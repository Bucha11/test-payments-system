"""Adapter for the mock provider (src/mock_provider).

Classification of transport outcomes (section 7):
* connection refused / connect timeout: the request never reached the provider
  -> FAILED_NOT_CHARGED;
* 409 (reference cancelled / voided) and 4xx validation errors: provably not charged
  -> FAILED_NOT_CHARGED;
* read timeout, reset after send, any 5xx, 202 "processing" -> UNKNOWN.

The timeout is enforced twice: httpx's own timeouts (real network) and an outer
asyncio.timeout, which also covers transports that ignore httpx timeouts (ASGI in tests).
No retries here; retry policy lives in the engine.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from cascade.domain.enums import CancelStatus, ChargeOutcome, LookupStatus
from cascade.domain.providers import CancelResult, ChargeRequest, ChargeResult, LookupResult
from cascade.domain.taxonomy import NormalizedReason, map_raw_code

log = logging.getLogger(__name__)

R = NormalizedReason

# Raw mock decline codes -> normalized reasons. The mock uses descriptive codes; a few
# ISO 8583-style numeric codes are accepted as well. Anything else -> UNMAPPED (hard).
MOCK_CODES: Mapping[str, NormalizedReason] = {
    **{r.value: r for r in R if r is not R.UNMAPPED},
    "05": R.DO_NOT_HONOR,
    "51": R.INSUFFICIENT_FUNDS,
    "54": R.EXPIRED_CARD,
    "14": R.INVALID_CARD,
    "41": R.LOST_OR_STOLEN,
    "43": R.LOST_OR_STOLEN,
    "91": R.ISSUER_UNAVAILABLE,
}


class MockProviderAdapter:
    def __init__(
        self,
        name: str,
        base_url: str,
        timeout_s: float,
        *,
        cancel_blocks_late_charge: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.cancel_blocks_late_charge = cancel_blocks_late_charge
        self.timeout_s = timeout_s
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(timeout_s), transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, url: str, json: Any = None) -> httpx.Response:
        async with asyncio.timeout(self.timeout_s):
            return await self._client.request(method, url, json=json)

    async def charge(self, req: ChargeRequest) -> ChargeResult:
        payload = {
            "reference": req.reference,
            "amount_minor": req.amount_minor,
            "currency": req.currency,
            "token": req.pm_token,
        }
        try:
            resp = await self._send("POST", "/charges", payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return ChargeResult(ChargeOutcome.FAILED_NOT_CHARGED, raw_message=type(e).__name__)
        except (TimeoutError, httpx.HTTPError) as e:
            return ChargeResult(ChargeOutcome.UNKNOWN, raw_message=type(e).__name__ or "timeout")
        body = _json(resp)
        code = str(resp.status_code)
        if resp.status_code == 200 and body.get("status") == "approved":
            return ChargeResult(ChargeOutcome.APPROVED, provider_txn_id=body.get("txn_id"))
        if resp.status_code == 402:
            raw = body.get("code")
            reason = map_raw_code(self.name, MOCK_CODES, raw)
            return ChargeResult.declined(reason, raw_code=raw, raw_message="declined")
        if resp.status_code in (400, 409, 422):
            return ChargeResult(
                ChargeOutcome.FAILED_NOT_CHARGED,
                raw_code=code,
                raw_message=str(body.get("error") or body.get("status")),
            )
        return ChargeResult(ChargeOutcome.UNKNOWN, raw_code=code, raw_message=f"http {code}")

    async def lookup(self, reference: str) -> LookupResult:
        try:
            resp = await self._send("GET", f"/charges/by-reference/{reference}")
        except (TimeoutError, httpx.HTTPError):
            return LookupResult(LookupStatus.UNKNOWN)
        if resp.status_code == 404:
            return LookupResult(LookupStatus.NOT_FOUND)
        if resp.status_code != 200:
            return LookupResult(LookupStatus.UNKNOWN)
        body = _json(resp)
        status, txn = body.get("status"), body.get("txn_id")
        if status == "charged":
            return LookupResult(LookupStatus.APPROVED, provider_txn_id=txn)
        if status == "voided":
            return LookupResult(LookupStatus.VOIDED, provider_txn_id=txn)
        if status == "declined":
            raw = body.get("code")
            reason = map_raw_code(self.name, MOCK_CODES, raw)
            return LookupResult(LookupStatus.DECLINED, reason=reason, raw_code=raw)
        return LookupResult(LookupStatus.UNKNOWN)  # processing / anything unexpected

    async def cancel(self, reference: str) -> CancelResult:
        try:
            resp = await self._send("POST", f"/charges/by-reference/{reference}/cancel")
        except (TimeoutError, httpx.HTTPError):
            return CancelResult(CancelStatus.UNKNOWN)
        if resp.status_code == 200 and _json(resp).get("status") == "cancelled":
            return CancelResult(CancelStatus.CANCELLED)
        if resp.status_code == 409:
            return CancelResult(CancelStatus.NOT_CANCELLABLE)
        return CancelResult(CancelStatus.UNKNOWN)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
