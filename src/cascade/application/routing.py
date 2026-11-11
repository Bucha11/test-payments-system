"""Route selection (section 9) and weighted expansion (8.7). Pure functions."""

import random
from collections.abc import Mapping, Sequence
from uuid import UUID

from cascade.application.config import Match, RouteEntry, RouteRule, RoutingConfig


def _matches(
    m: Match, amount_minor: int, currency: str, country: str | None, bin_: str | None
) -> bool:
    if m.currency is not None and currency not in m.currency:
        return False
    if m.country is not None and (country is None or country not in m.country):
        return False
    if m.amount_minor_gte is not None and amount_minor < m.amount_minor_gte:
        return False
    if m.amount_minor_lt is not None and amount_minor >= m.amount_minor_lt:
        return False
    if m.bin_prefix is not None and (bin_ is None or not bin_.startswith(tuple(m.bin_prefix))):
        return False
    return True


def select_rule(
    cfg: RoutingConfig,
    *,
    amount_minor: int,
    currency: str,
    country: str | None,
    bin_: str | None,
) -> RouteRule:
    """First matching rule wins; the last rule is always the default."""
    for rule in cfg.routes:
        if rule.default or (
            rule.match is not None and _matches(rule.match, amount_minor, currency, country, bin_)
        ):
            return rule
    raise AssertionError("config validation guarantees a default rule")


def weighted_order(weights: Mapping[str, int], rng: random.Random) -> list[str]:
    """Weighted random sampling without replacement.

    The first pick is proportional to weight; the rest stay as ordered fallbacks.
    Iteration follows the mapping's insertion order, so the result is a pure function
    of (weights, rng state).
    """
    remaining = dict(weights)
    out: list[str] = []
    while remaining:
        r = rng.random() * sum(remaining.values())
        acc = 0.0
        pick = next(iter(remaining))
        for name, w in remaining.items():
            acc += w
            if r < acc:
                pick = name
                break
        out.append(pick)
        del remaining[pick]
    return out


def expand(entries: Sequence[RouteEntry], payment_id: UUID) -> list[str]:
    """Expand split groups with an RNG seeded by the payment id; drop repeats."""
    rng = random.Random(payment_id.int)
    out: list[str] = []
    for e in entries:
        names = [e] if isinstance(e, str) else weighted_order(e.split, rng)
        out.extend(n for n in names if n not in out)
    return out
