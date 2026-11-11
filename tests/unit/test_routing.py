import random
import uuid
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cascade.application.config import RoutingConfig
from cascade.application.routing import expand, select_rule, weighted_order
from cascade.infrastructure.config_loader import load_routing_config

CFG_PATH = Path(__file__).resolve().parents[2] / "config" / "routing.yaml"
CFG = load_routing_config(CFG_PATH)


def route(amount: int = 1000, currency: str = "USD", country: str | None = None) -> list[str]:
    rule = select_rule(CFG, amount_minor=amount, currency=currency, country=country, bin_=None)
    return expand(rule.providers, uuid.uuid4())


def test_rules_first_match_wins() -> None:
    assert route(currency="EUR", country="DE") == ["mock_b", "mock_a"]
    assert route(currency="EUR", country="FR") == ["mock_b", "mock_a"]
    assert route(currency="EUR", country=None) == ["mock_a", "mock_b"]
    assert route(amount=50_000) == ["mock_a"]
    assert route(amount=50_000, currency="EUR", country="DE") == ["mock_b", "mock_a"]
    assert route(amount=49_999) == ["mock_a", "mock_b"]


def test_s14_weighted_split_distribution() -> None:
    rule = select_rule(CFG, amount_minor=1000, currency="GBP", country=None, bin_=None)
    firsts: Counter[str] = Counter()
    for i in range(1000):
        pid = uuid.UUID(int=random.Random(i).getrandbits(128))
        order = expand(rule.providers, pid)
        assert sorted(order) == ["mock_a", "mock_b"]  # both always present (fallback)
        assert order == expand(rule.providers, pid)  # deterministic per payment id
        firsts[order[0]] += 1
    share = firsts["mock_a"] / 1000
    assert 0.65 <= share <= 0.75, share


def test_weighted_order_is_a_permutation() -> None:
    rng = random.Random(1)
    w = {"a": 5, "b": 1, "c": 1}
    for _ in range(200):
        assert sorted(weighted_order(w, rng)) == ["a", "b", "c"]


def test_expand_dedupes() -> None:
    from cascade.application.config import Split

    assert expand([Split(split={"a": 1, "b": 1}), "a", "c"], uuid.uuid4())[2:] == ["c"]


def _cfg(**over: object) -> dict:
    data = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "config" / "routing.yaml").read_text()
    )
    data.update(over)
    return data


@pytest.mark.parametrize(
    "bad",
    [
        {"routes": [{"default": True, "providers": ["nope"]}]},
        {"routes": [{"match": {"currency": ["EUR"]}, "providers": ["mock_a"]}]},  # no default
        {
            "routes": [
                {"default": True, "providers": ["mock_a"]},
                {"match": {"currency": ["EUR"]}, "providers": ["mock_a"]},
            ]
        },  # default not last
        {"routes": [{"match": {"currency": ["EUR"]}, "default": True, "providers": ["mock_a"]}]},
        {"routes": [{"default": True, "providers": []}]},
        {"routes": [{"default": True, "providers": [{"split": {"mock_a": 0}}]}]},
        {"cascade": {"max_attempts": 0}},
        {"cascade": {"unknown_key": 1}},
        {
            "providers": {
                "mock_a": {"type": "mock", "timeout_s": 3, "failure_threshold": 3, "cooldown_s": 30}
            }
        },  # mock without base_url
        {"cascade": {"lease_s": 10}},  # braintree 5 s x (3 lookups + charge + cancel) >= lease
    ],
)
def test_invalid_config_fails_fast(bad: dict) -> None:
    with pytest.raises(ValidationError):
        RoutingConfig.model_validate(_cfg(**bad))


def test_us_usd_routes_to_braintree_first() -> None:
    assert route(currency="USD", country="US") == ["braintree", "mock_a"]
    assert route(currency="USD", country="US", amount=200_000) == ["braintree", "mock_a"]
    assert route(currency="USD", country=None) == ["mock_a", "mock_b"]
