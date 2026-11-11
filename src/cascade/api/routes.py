import hashlib
import hmac
import json
import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from cascade.api.schemas import PaymentIn, PaymentOut
from cascade.application.payments import CreatePayment, IdempotencyConflict, ProviderEvent
from cascade.domain.enums import PaymentStatus
from cascade.domain.money import Money
from cascade.domain.taxonomy import map_raw_code
from cascade.domain.transitions import PAYMENT_TERMINAL
from cascade.infrastructure.container import Services
from cascade.infrastructure.db.stats import compute_stats
from cascade.infrastructure.providers.mock import MOCK_CODES

log = logging.getLogger(__name__)
router = APIRouter()


def _svc(request: Request) -> Services:
    s: Services = request.app.state.services
    return s


def _respond(out: PaymentOut) -> JSONResponse:
    # 201 when terminal; 202 while still in flight (PENDING_RECONCILIATION, or a replay
    # of a request that is still being processed).
    code = 201 if PaymentStatus(out.status) in PAYMENT_TERMINAL else 202
    return JSONResponse(out.model_dump(mode="json"), status_code=code)


@router.post("/payments")
async def create_payment(
    body: PaymentIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    if not idempotency_key or len(idempotency_key) > 255:
        raise HTTPException(400, "Idempotency-Key header is required (max 255 chars)")
    cmd = CreatePayment(
        idempotency_key=idempotency_key,
        money=Money(body.amount_minor, body.currency),
        pm_token=body.payment_method.token,
        pm_bin=body.payment_method.bin,
        pm_country=body.payment_method.country,
    )
    try:
        p, attempts = await _svc(request).payments.create(cmd)
    except IdempotencyConflict:
        raise HTTPException(422, "Idempotency-Key reused with a different request body") from None
    return _respond(PaymentOut.of(p, attempts))


@router.get("/payments/{payment_id}")
async def get_payment(payment_id: UUID, request: Request) -> JSONResponse:
    try:
        p, attempts = await _svc(request).payments.get(payment_id)
    except KeyError:
        raise HTTPException(404, "payment not found") from None
    return _respond(PaymentOut.of(p, attempts))


@router.post("/webhooks/{provider}")
async def webhook(provider: str, request: Request) -> dict[str, str]:
    svc = _svc(request)
    if provider not in svc.adapters:
        raise HTTPException(404, "unknown provider")
    raw = await request.body()
    expected = hmac.new(svc.settings.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("X-Signature", "")):
        raise HTTPException(401, "bad signature")
    try:
        payload: dict[str, Any] = json.loads(raw)
        event_id = str(payload["event_id"])
        reference = str(payload["reference"])
        kind = str(payload["type"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "malformed event") from None

    if not await svc.repo.claim_webhook(provider, event_id, payload):
        return {"status": "duplicate"}
    code = payload.get("code")
    ev = ProviderEvent(
        provider=provider,
        reference=reference,
        succeeded=kind == "charge.succeeded",
        reason=None if kind == "charge.succeeded" else map_raw_code(provider, MOCK_CODES, code),
        raw_code=code,
        provider_txn_id=payload.get("txn_id"),
    )
    # If applying fails (or the process dies here), the event stays unprocessed and the
    # provider's redelivery applies it.
    result = await svc.events.apply(ev)
    await svc.repo.mark_webhook_processed(provider, event_id)
    log.info(
        "webhook applied", extra={"provider": provider, "event_id": event_id, "result": result}
    )
    return {"status": result}


@router.get("/stats")
async def stats(
    request: Request, window_min: Annotated[int, Query(ge=1, le=7 * 24 * 60)] = 60
) -> dict[str, Any]:
    return await compute_stats(_svc(request).sessionmaker, window_min)


@router.get("/providers")
async def providers(request: Request) -> list[dict[str, Any]]:
    svc = _svc(request)
    out = []
    for name, pc in svc.config.providers.items():
        snap = await svc.breaker.snapshot(name)
        out.append(
            {
                "name": name,
                "type": pc.type,
                "enabled": pc.enabled and name in svc.adapters,
                "breaker_state": snap.state.value,
                "consecutive_failures": snap.consecutive_failures,
                "seconds_until_half_open": (
                    round(snap.seconds_until_half_open, 1)
                    if snap.seconds_until_half_open is not None
                    else None
                ),
            }
        )
    return out
