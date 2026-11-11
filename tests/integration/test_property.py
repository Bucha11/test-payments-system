"""Property test (section 13): random provider behaviour never breaks the money invariants.

For random scripts for A and B (any step from section 10) and random flag combinations,
a payment is driven to a terminal state: the API call, then reconciliation rounds driven
by hand (outages end, the fake clock jumps forward so late charges land, webhooks are
delivered). Then:
  * ledger invariant: charged entries == 1 if SUCCEEDED else 0;
  * so a payment is never SUCCEEDED with zero charges;
  * a HARD decline is never followed by another attempt.

Adapters are configured truthfully about `cancel_blocks_late_charge` (= the mock flag).
The lying-provider case is covered by S9 (it needs a webhook to be recoverable).
"""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cascade.domain.taxonomy import DeclineClass, NormalizedReason, classify
from tests.integration.conftest import HarnessFactory
from tests.integration.harness import Harness, make_config

pytestmark = pytest.mark.integration

SOFT = sorted(r.value for r in NormalizedReason if classify(r) is DeclineClass.SOFT)
HARD = sorted(
    r.value for r in NormalizedReason if classify(r) is DeclineClass.HARD and r.value != "unmapped"
)

step = st.one_of(
    st.just("approve"),
    st.sampled_from(SOFT).map(lambda c: f"soft_decline:{c}"),
    st.sampled_from(HARD).map(lambda c: f"hard_decline:{c}"),
    st.just("error_500"),
    st.just("timeout"),
    st.just("timeout_charged"),
    st.integers(1, 10).map(lambda s: f"late_charge:{s}"),
)
script = st.lists(step, max_size=3)
flags = st.fixed_dictionaries(
    {
        "lookup_down": st.booleans(),
        "cancel_down": st.booleans(),
        "cancel_blocks_late_charge": st.booleans(),
        "webhooks": st.sampled_from(["off", "once", "duplicate"]),
    }
)

TERMINAL = {"SUCCEEDED", "FAILED"}


@pytest.fixture
async def penv(make_env: HarnessFactory) -> Harness:
    return await make_env(make_config(timeout_s=0.1))


async def _run_example(env: Harness, scripts: dict, flag_sets: dict) -> None:
    await env.reset_mocks()
    await env.svc.redis.flushdb()
    for name in ("mock_a", "mock_b"):
        await env.flags(name, **flag_sets[name])
        if scripts[name]:
            await env.script(name, *scripts[name])
        env.svc.adapters[name].cancel_blocks_late_charge = flag_sets[name][  # type: ignore[misc]
            "cancel_blocks_late_charge"
        ]

    r = await env.pay()
    assert r.status_code in (201, 202), r.text
    pid = r.json()["id"]

    # Drive reconciliation: the outage ends, time passes, webhooks arrive, the sweep runs.
    for name in ("mock_a", "mock_b"):
        await env.flags(name, lookup_down=False, cancel_down=False)
    for _ in range(8):
        if (await env.get(pid))["status"] in TERMINAL:
            break
        env.clock.advance(300)
        for name in ("mock_a", "mock_b"):
            await env.deliver_webhooks(name)
        await env.svc.engine.sweep()

    # Anything that lands late (late charges, their webhooks) is handled too.
    env.clock.advance(1000)
    for name in ("mock_a", "mock_b"):
        await env.ledger(name)  # settles due late charges
        await env.deliver_webhooks(name)

    p = await env.assert_ledger_invariant(pid)
    assert p["status"] in TERMINAL, p
    statuses = [a["status"] for a in p["attempts"]]
    if "DECLINED_HARD" in statuses:
        assert statuses.index("DECLINED_HARD") == len(statuses) - 1, statuses
        assert p["status"] == "FAILED"


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(sa=script, sb=script, fa=flags, fb=flags)
async def test_random_provider_behaviour_keeps_invariants(
    penv: Harness, sa: list[str], sb: list[str], fa: dict, fb: dict
) -> None:
    await _run_example(penv, {"mock_a": sa, "mock_b": sb}, {"mock_a": fa, "mock_b": fb})
