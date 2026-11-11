"""Mock payment provider (section 10). One codebase, run as two containers.

State is in memory. Behaviour of the next charges is scripted through /_admin/script;
`GET /_admin/ledger` is the ground truth for tests: what was actually charged.

The clock is injectable so tests can fast-forward `late_charge:<s>` deterministically.
Due late charges are settled lazily on every request (and by a background loop when run
as a real server, which also delivers webhooks).
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt

STEP_RE = re.compile(
    r"^(approve|error_500|timeout|timeout_charged"
    r"|soft_decline(:[a-z0-9_]+)?|hard_decline(:[a-z0-9_]+)?|late_charge:\d+(\.\d+)?)$"
)
DEFAULT_SOFT = "do_not_honor"
DEFAULT_HARD = "insufficient_funds"

RecordState = Literal["charged", "voided", "declined", "pending"]


@dataclass
class Record:
    reference: str
    amount_minor: int
    currency: str
    state: RecordState
    txn_id: str
    code: str | None = None
    due_at: float | None = None  # pending late charge


@dataclass
class Flags:
    lookup_down: bool = False
    cancel_down: bool = False
    cancel_blocks_late_charge: bool = True
    webhooks: Literal["off", "once", "duplicate"] = "off"


@dataclass
class MockState:
    name: str
    clock: Callable[[], float]
    secret: str
    hang_s: float = 3600.0
    records: dict[str, Record] = field(default_factory=dict)
    tombstones: set[str] = field(default_factory=set)
    script: deque[str] = field(default_factory=deque)
    flags: Flags = field(default_factory=Flags)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _event_seq: int = 0
    _txn_seq: int = 0

    def reset(self) -> None:
        self.records.clear()
        self.tombstones.clear()
        self.script.clear()
        self.flags = Flags()
        self.outbox.clear()
        self.calls.clear()

    def next_txn(self) -> str:
        self._txn_seq += 1
        return f"{self.name}_txn_{self._txn_seq}"

    def emit(self, rec: Record) -> None:
        if self.flags.webhooks == "off":
            return
        self._event_seq += 1
        body = {
            "event_id": f"{self.name}_evt_{self._event_seq}",
            "type": "charge.succeeded" if rec.state == "charged" else "charge.declined",
            "reference": rec.reference,
            "txn_id": rec.txn_id,
            "code": rec.code,
        }
        raw = json.dumps(body, sort_keys=True)
        sig = hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        event = {"body": raw, "signature": sig, "delivered": False}
        self.outbox.append(event)
        if self.flags.webhooks == "duplicate":
            self.outbox.append(dict(event))

    def settle_due(self) -> None:
        now = self.clock()
        for rec in self.records.values():
            if rec.state == "pending" and rec.due_at is not None and rec.due_at <= now:
                rec.state, rec.due_at = "charged", None
                self.emit(rec)


class ChargeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str
    amount_minor: StrictInt
    currency: str
    token: str


class ScriptIn(BaseModel):
    steps: list[str]


class FlagsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lookup_down: bool | None = None
    cancel_down: bool | None = None
    cancel_blocks_late_charge: bool | None = None
    webhooks: Literal["off", "once", "duplicate"] | None = None


def _charge_response(rec: Record) -> JSONResponse:
    if rec.state == "charged":
        return JSONResponse({"status": "approved", "txn_id": rec.txn_id})
    if rec.state == "declined":
        return JSONResponse({"status": "declined", "code": rec.code}, status_code=402)
    if rec.state == "voided":
        return JSONResponse({"status": "voided", "txn_id": rec.txn_id}, status_code=409)
    return JSONResponse({"status": "processing"}, status_code=202)


def create_app(
    name: str,
    *,
    clock: Callable[[], float] = time.time,
    secret: str = "dev-webhook-secret",
    webhook_url: str | None = None,
    hang_s: float = 3600.0,
) -> FastAPI:
    state = MockState(name=name, clock=clock, secret=secret, hang_s=hang_s)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_background(state, webhook_url))
        yield
        task.cancel()

    app = FastAPI(title=f"Mock provider {name}", lifespan=lifespan)
    app.state.mock = state

    async def hang() -> JSONResponse:
        await asyncio.sleep(state.hang_s)
        return JSONResponse({"status": "gateway_timeout"}, status_code=504)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "provider": name}

    @app.post("/charges")
    async def charge(body: ChargeIn) -> JSONResponse:
        state.settle_due()
        ref = body.reference
        if ref in state.tombstones:
            return JSONResponse({"error": "reference_cancelled"}, status_code=409)
        if ref in state.records:  # idempotent by reference
            return _charge_response(state.records[ref])
        step = state.script.popleft() if state.script else "approve"
        state.calls.append({"reference": ref, "step": step})
        kind, _, arg = step.partition(":")

        def record(st: RecordState, code: str | None = None, due: float | None = None) -> Record:
            rec = Record(ref, body.amount_minor, body.currency, st, state.next_txn(), code, due)
            state.records[ref] = rec
            return rec

        if kind == "approve":
            rec = record("charged")
            state.emit(rec)
            return _charge_response(rec)
        if kind in ("soft_decline", "hard_decline"):
            rec = record(
                "declined", arg or (DEFAULT_SOFT if kind == "soft_decline" else DEFAULT_HARD)
            )
            state.emit(rec)
            return _charge_response(rec)
        if kind == "error_500":
            return JSONResponse({"error": "internal"}, status_code=500)
        if kind == "timeout":
            return await hang()
        if kind == "timeout_charged":
            state.emit(record("charged"))
            return await hang()
        if kind == "late_charge":
            record("pending", due=state.clock() + float(arg))
            return await hang()
        raise HTTPException(400, f"bad step {step}")

    @app.get("/charges/by-reference/{reference}")
    async def lookup(reference: str) -> JSONResponse:
        state.settle_due()
        if state.flags.lookup_down:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        rec = state.records.get(reference)
        if rec is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        status = "processing" if rec.state == "pending" else rec.state
        return JSONResponse({"status": status, "txn_id": rec.txn_id, "code": rec.code})

    @app.post("/charges/by-reference/{reference}/cancel")
    async def cancel(reference: str) -> JSONResponse:
        state.settle_due()
        if state.flags.cancel_down:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        rec = state.records.get(reference)
        blocks = state.flags.cancel_blocks_late_charge
        if rec is None:
            if blocks:
                state.tombstones.add(reference)  # a later charge with it is rejected
            return JSONResponse({"status": "cancelled"})
        if rec.state == "charged":
            rec.state = "voided"
        elif rec.state == "pending" and blocks:
            rec.state, rec.due_at = "voided", None
        # pending + not blocking: reports success, but the late charge still happens.
        return JSONResponse({"status": "cancelled"})

    @app.post("/_admin/script")
    async def set_script(body: ScriptIn) -> dict[str, Any]:
        bad = [s for s in body.steps if not STEP_RE.match(s)]
        if bad:
            raise HTTPException(422, f"bad steps: {bad}")
        state.script.extend(body.steps)
        return {"queued": list(state.script)}

    @app.post("/_admin/flags")
    async def set_flags(body: FlagsIn) -> dict[str, Any]:
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(state.flags, k, v)
        return asdict(state.flags)

    @app.get("/_admin/ledger")
    async def ledger() -> dict[str, Any]:
        state.settle_due()
        return {
            ref: {
                "state": r.state,
                "amount_minor": r.amount_minor,
                "currency": r.currency,
                "txn_id": r.txn_id,
            }
            for ref, r in state.records.items()
            if r.state in ("charged", "voided")
        }

    @app.get("/_admin/webhooks")
    async def webhooks() -> list[dict[str, Any]]:
        state.settle_due()
        return state.outbox

    @app.get("/_admin/calls")
    async def calls() -> list[dict[str, Any]]:
        return state.calls

    @app.post("/_admin/reset")
    async def reset() -> dict[str, str]:
        state.reset()
        return {"status": "reset"}

    return app


async def _background(state: MockState, webhook_url: str | None) -> None:
    """Real-server mode: settle late charges on time and deliver webhooks."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            await asyncio.sleep(0.2)
            state.settle_due()
            if not webhook_url:
                continue
            for ev in state.outbox:
                if ev["delivered"]:
                    continue
                try:
                    resp = await client.post(
                        webhook_url,
                        content=ev["body"],
                        headers={
                            "X-Signature": ev["signature"],
                            "Content-Type": "application/json",
                        },
                    )
                    ev["delivered"] = resp.status_code < 500
                except httpx.HTTPError:
                    break  # retry on the next tick


app = create_app(
    os.environ.get("MOCK_PROVIDER_NAME", "mock"),
    secret=os.environ.get("WEBHOOK_SECRET", "dev-webhook-secret"),
    webhook_url=os.environ.get("WEBHOOK_URL") or None,
)
