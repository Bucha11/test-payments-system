"""Adapter classification of transport outcomes, with fake transports."""

import asyncio

import httpx
import pytest

from cascade.domain.enums import CancelStatus, ChargeOutcome, LookupStatus
from cascade.domain.providers import ChargeRequest
from cascade.infrastructure.providers.mock import MockProviderAdapter

REQ = ChargeRequest("ref-1", 1000, "EUR", "tok_secret")


def adapter(handler) -> MockProviderAdapter:  # type: ignore[no-untyped-def]
    return MockProviderAdapter("m", "http://m", 0.2, transport=httpx.MockTransport(handler))


def raise_(exc: Exception):  # type: ignore[no-untyped-def]
    def h(req: httpx.Request) -> httpx.Response:
        raise exc

    return h


@pytest.mark.parametrize(
    ("handler", "outcome"),
    [
        (lambda r: httpx.Response(200, json={"status": "approved", "txn_id": "t"}), "APPROVED"),
        (lambda r: httpx.Response(402, json={"code": "do_not_honor"}), "SOFT_DECLINE"),
        (lambda r: httpx.Response(402, json={"code": "51"}), "HARD_DECLINE"),
        (lambda r: httpx.Response(402, json={"code": "???"}), "HARD_DECLINE"),
        (
            lambda r: httpx.Response(409, json={"error": "reference_cancelled"}),
            "FAILED_NOT_CHARGED",
        ),
        (lambda r: httpx.Response(422, json={}), "FAILED_NOT_CHARGED"),
        (lambda r: httpx.Response(500), "UNKNOWN"),
        (lambda r: httpx.Response(502), "UNKNOWN"),
        (lambda r: httpx.Response(503), "UNKNOWN"),
        (lambda r: httpx.Response(202, json={"status": "processing"}), "UNKNOWN"),
        (raise_(httpx.ConnectError("refused")), "FAILED_NOT_CHARGED"),
        (raise_(httpx.ConnectTimeout("t")), "FAILED_NOT_CHARGED"),
        (raise_(httpx.ReadTimeout("t")), "UNKNOWN"),
        (raise_(httpx.RemoteProtocolError("reset")), "UNKNOWN"),
        (raise_(httpx.ReadError("reset")), "UNKNOWN"),
    ],
)
async def test_charge_classification(handler, outcome: str) -> None:  # type: ignore[no-untyped-def]
    res = await adapter(handler).charge(REQ)
    assert res.outcome is ChargeOutcome(outcome)


async def test_outer_timeout_is_unknown() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    a = MockProviderAdapter("m", "http://m", 0.05, transport=httpx.MockTransport(slow))
    assert (await a.charge(REQ)).outcome is ChargeOutcome.UNKNOWN
    assert (await a.lookup("r")).status is LookupStatus.UNKNOWN
    assert (await a.cancel("r")).status is CancelStatus.UNKNOWN


@pytest.mark.parametrize(
    ("resp", "status"),
    [
        (httpx.Response(200, json={"status": "charged"}), LookupStatus.APPROVED),
        (httpx.Response(200, json={"status": "voided"}), LookupStatus.VOIDED),
        (httpx.Response(200, json={"status": "declined", "code": "54"}), LookupStatus.DECLINED),
        (httpx.Response(200, json={"status": "processing"}), LookupStatus.UNKNOWN),
        (httpx.Response(404), LookupStatus.NOT_FOUND),
        (httpx.Response(503), LookupStatus.UNKNOWN),
    ],
)
async def test_lookup_classification(resp: httpx.Response, status: LookupStatus) -> None:
    assert (await adapter(lambda r: resp).lookup("r")).status is status


async def test_token_sent_but_never_in_repr() -> None:
    seen = {}

    def h(r: httpx.Request) -> httpx.Response:
        seen["body"] = r.content
        return httpx.Response(200, json={"status": "approved"})

    await adapter(h).charge(REQ)
    assert b"tok_secret" in seen["body"]
    assert "tok_secret" not in repr(REQ)
