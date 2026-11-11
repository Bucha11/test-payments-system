"""Demo against the running stack (`make up`, then `make demo`).

For each scenario: script the mocks through /_admin, create the payment over HTTP,
print the final status, the attempts table and the ledger check (OK / VIOLATION).
Ends with /stats and /providers. Exit code 1 on any ledger violation.

Run order differs from the numbering on purpose: the breaker counts consecutive
UNKNOWN outcomes across payments, so scenarios with timeouts on A are interleaved with
healthy ones, and S4 (which deliberately opens A's breaker) runs last.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

API = os.environ.get("API_URL", "http://localhost:8000")
MOCKS = {
    "mock_a": os.environ.get("MOCK_A_URL", "http://localhost:8101"),
    "mock_b": os.environ.get("MOCK_B_URL", "http://localhost:8102"),
}
TERMINAL = {"SUCCEEDED", "FAILED"}


def _braintree() -> Any:
    """Braintree adapter for the ledger check (credentials from .env), or None."""
    try:
        from cascade.infrastructure.providers.braintree import BraintreeAdapter
        from cascade.infrastructure.settings import Settings
    except ImportError:
        return None
    st = Settings()
    if not st.braintree_configured:
        return None
    return BraintreeAdapter.create(
        "braintree",
        merchant_id=st.braintree_merchant_id,
        public_key=st.braintree_public_key,
        private_key=st.braintree_private_key,
        environment=st.braintree_environment,
        timeout_s=15,
    )


BRAINTREE = _braintree()

http = httpx.Client(timeout=30)
violations: list[str] = []


# --------------------------------------------------------------------------- helpers


def mock(name: str, path: str, body: Any = None) -> Any:
    url = MOCKS[name] + path
    r = http.post(url, json=body) if body is not None or path.endswith("reset") else http.get(url)
    r.raise_for_status()
    return r.json()


def setup(
    a: list[str] | None = None,
    b: list[str] | None = None,
    a_flags: dict[str, Any] | None = None,
    b_flags: dict[str, Any] | None = None,
) -> None:
    for name, steps, flags in (("mock_a", a, a_flags), ("mock_b", b, b_flags)):
        mock(name, "/_admin/reset")
        if flags:
            mock(name, "/_admin/flags", flags)
        if steps:
            mock(name, "/_admin/script", {"steps": steps})


def pay(key: str | None = None, **pm: Any) -> httpx.Response:
    body = {
        "amount_minor": pm.pop("amount_minor", 1999),
        "currency": pm.pop("currency", "USD"),
        "payment_method": {"token": "tok_demo_visa", **pm},
    }
    return http.post(
        f"{API}/payments", json=body, headers={"Idempotency-Key": key or f"demo-{uuid.uuid4()}"}
    )


def get(pid: str) -> dict[str, Any]:
    data: dict[str, Any] = http.get(f"{API}/payments/{pid}").json()
    return data


def wait_until(pid: str, pred: Callable[[dict[str, Any]], bool], timeout_s: float = 45) -> dict:
    end = time.monotonic() + timeout_s
    p = get(pid)
    while not pred(p) and time.monotonic() < end:
        time.sleep(0.5)
        p = get(pid)
    return p


def ledger_charges(p: dict[str, Any]) -> int:
    refs = {a["id"] for a in p["attempts"]}
    n = 0
    for name in MOCKS:
        for ref, e in mock(name, "/_admin/ledger").items():
            if ref in refs and e["state"] == "charged":
                n += 1
    for a in p["attempts"]:
        if a["provider"] == "braintree" and BRAINTREE is not None:
            if asyncio.run(BRAINTREE.lookup(a["id"])).status.value == "APPROVED":
                n += 1
    return n


def report(title: str, p: dict[str, Any], http_status: int | None = None) -> None:
    print(f"\n=== {title}")
    hs = f" (HTTP {http_status})" if http_status else ""
    extra = f", reason={p['failure_reason']}" if p.get("failure_reason") else ""
    review = ", needs_review" if p.get("needs_review") else ""
    print(f"payment {p['id']}: {p['status']}{hs}{extra}{review}; route={p['route']}")
    print(f"  {'seq':>3}  {'provider':<9} {'status':<19} {'reason':<22} {'duration':>9}")
    for a in p["attempts"]:
        dur = f"{a['duration_ms']} ms" if a["duration_ms"] is not None else "-"
        print(
            f"  {a['seq']:>3}  {a['provider']:<9} {a['status']:<19} "
            f"{(a['reason'] or '-'):<22} {dur:>9}"
        )
    charged = ledger_charges(p)
    expected = 1 if p["status"] == "SUCCEEDED" else 0
    ok = charged == expected
    print(f"  ledger: {charged} charge(s), expected {expected} -> {'OK' if ok else 'VIOLATION'}")
    if not ok:
        violations.append(title)


def run(title: str, **kw: Any) -> dict[str, Any]:
    r = pay(**kw)
    p = r.json()
    if p["status"] not in TERMINAL:
        p = wait_until(p["id"], lambda x: x["status"] in TERMINAL)
    report(title, p, r.status_code)
    return p


# --------------------------------------------------------------------------- scenarios


def s1() -> None:
    setup()
    run("S1  A approves")


def s2() -> None:
    setup(a=["soft_decline:do_not_honor"])
    run("S2  A soft decline -> B approves")


def s3() -> None:
    setup(a=["hard_decline:insufficient_funds"])
    run("S3  A hard decline -> stop, B never called")
    print(f"  mock_b charge calls: {len(mock('mock_b', '/_admin/calls'))}")


def s5() -> None:
    setup(a=["timeout_charged"])
    run("S5  A times out but charged -> lookup finds it, B never called")
    print(f"  mock_b charge calls: {len(mock('mock_b', '/_admin/calls'))}")


def s6() -> None:
    setup(a=["late_charge:5"])
    p = run("S6  A hangs, would charge after 5 s -> cancelled (tombstone), B approves")
    time.sleep(6)
    a_charged = [
        e
        for ref, e in mock("mock_a", "/_admin/ledger").items()
        if ref == p["attempts"][0]["id"] and e["state"] == "charged"
    ]
    print(f"  after 6 s: mock_a charged entries for this payment = {len(a_charged)}")
    report("S6  (re-check after 6 s)", get(p["id"]))


def s7() -> None:
    setup(a=["timeout_charged"], a_flags={"lookup_down": True, "cancel_down": True})
    r = pay()
    p = r.json()
    print("\n=== S7  A unknown + lookup/cancel down -> parked, B never called")
    print(
        f"payment {p['id']}: {p['status']} (HTTP {r.status_code}); "
        f"mock_b calls: {len(mock('mock_b', '/_admin/calls'))}"
    )
    mock("mock_a", "/_admin/flags", {"lookup_down": False, "cancel_down": False})
    print("  outage over (flags cleared); waiting for the worker to reconcile...")
    p = wait_until(p["id"], lambda x: x["status"] in TERMINAL)
    report("S7  after reconciliation by the worker", p)


def s8() -> None:
    setup()
    from concurrent.futures import ThreadPoolExecutor

    key = f"demo-s8-{uuid.uuid4()}"
    with ThreadPoolExecutor(10) as ex:
        rs = list(ex.map(lambda _: pay(key), range(10)))
    ids = {r.json()["id"] for r in rs}
    codes = sorted(r.status_code for r in rs)
    print("\n=== S8  10 concurrent POSTs with one Idempotency-Key")
    print(f"  distinct payment ids: {len(ids)}; HTTP codes: {codes}")
    print(f"  provider charge calls: {sum(len(mock(n, '/_admin/calls')) for n in MOCKS)}")
    p = wait_until(ids.pop(), lambda x: x["status"] in TERMINAL)
    report("S8  resulting payment", p)


def s9() -> None:
    setup(a_flags={"webhooks": "duplicate"})
    p = run("S9a A approves, webhook delivered twice (duplicate is a no-op)")
    time.sleep(1.5)
    delivered = sum(1 for e in mock("mock_a", "/_admin/webhooks") if e["delivered"])
    print(f"  webhooks delivered: {delivered}; status still {get(p['id'])['status']}")

    setup(
        a=["late_charge:6"],
        a_flags={"webhooks": "duplicate", "cancel_blocks_late_charge": False},
    )
    p = run("S9b A's cancel does NOT block the late charge -> B approves meanwhile")
    print("  waiting for A's late charge + its webhook...")
    p = wait_until(
        p["id"], lambda x: x["attempts"][0]["status"] == "REFUNDED_DUPLICATE", timeout_s=20
    )
    report("S9b after late-success webhook: A cancelled -> REFUNDED_DUPLICATE", p)


def s10() -> None:
    setup(a=["soft_decline:do_not_honor"], b=["soft_decline:generic_decline"])
    run("S10 A and B soft decline -> FAILED, route exhausted")


def bt() -> None:
    providers = {p["name"]: p for p in http.get(f"{API}/providers").json()}
    if not providers.get("braintree", {}).get("enabled"):
        print("\n=== BT  skipped: Braintree sandbox credentials are not set in .env")
        return
    # Route for USD + country US: [braintree, mock_a]. Sandbox test amounts pick the outcome.
    setup()
    run("BT1 US card: Braintree sandbox approves", country="US", amount_minor=1999)
    setup()
    run(
        "BT2 Braintree soft decline (2000 Do Not Honor) -> mock_a approves",
        country="US",
        amount_minor=200_000,
    )
    setup()
    run(
        "BT3 Braintree hard decline (2001 Insufficient Funds) -> stop, mock_a never called",
        country="US",
        amount_minor=200_100,
    )
    print(f"  mock_a charge calls: {len(mock('mock_a', '/_admin/calls'))}")


def s4() -> None:
    setup(a=["error_500", "error_500", "error_500"])
    for i in range(3):
        run(f"S4.{i + 1} A returns 500 (unknown -> lookup NOT_FOUND -> cancel) -> B")
    print("\n  /providers after 3 consecutive failures on A:")
    print(json.dumps(http.get(f"{API}/providers").json(), indent=2))
    run("S4.4 A's breaker is OPEN -> route is B only")
    print("  waiting 31 s for A's cooldown...")
    time.sleep(31)
    run("S4.5 after cooldown: one half-open probe goes to A, succeeds, breaker closes")


def main() -> int:
    http.get(f"{API}/health").raise_for_status()
    for fn in (s1, s5, s2, s6, s3, s7, s8, s9, s10, bt, s4):
        fn()
    print("\n=== GET /stats?window_min=60")
    print(json.dumps(http.get(f"{API}/stats", params={"window_min": 60}).json(), indent=2))
    print("\n=== GET /providers")
    print(json.dumps(http.get(f"{API}/providers").json(), indent=2))
    print(f"\nledger violations: {len(violations)} {violations or ''}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
