"""Cascade engine (sections 8.1-8.3).

Shape of the algorithm, in one place:

    process(payment):
        build route (once, CREATED only); empty -> FAILED no_route
        claim (lease + fencing token); not claimed -> someone else is on it
        loop:
            re-read payment and attempts from the database
            look at the LAST attempt:
                SUCCEEDED            -> done (payment was settled in the same transaction)
                DECLINED_HARD        -> FAILED, stop
                STARTED              -> crashed mid-call: mark UNKNOWN, loop
                UNKNOWN              -> resolve (8.2) or park in PENDING_RECONCILIATION
                money-free otherwise -> fall through and try the next provider
            budget / route / max_attempts left?  no -> FAILED with the last reason
            write-ahead attempt STARTED (committed) -> charge() -> record breaker -> persist

The loop always decides from what is persisted, never from what it "just did". So a
webhook, a crashed predecessor or a lost write all lead to the same decision path.
No database transaction is open while a provider is called.
"""

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum, auto
from uuid import UUID, uuid4

from cascade.application.config import RoutingConfig
from cascade.application.ports import (
    AttemptUpdate,
    CircuitBreaker,
    Clock,
    LostLease,
    PaymentRepository,
    TaskQueue,
)
from cascade.application.routing import expand, select_rule
from cascade.domain.entities import Attempt, Payment
from cascade.domain.enums import AttemptStatus, CancelStatus, ChargeOutcome, PaymentStatus
from cascade.domain.outcomes import attempt_status_for_charge, attempt_status_for_lookup
from cascade.domain.providers import ChargeRequest, ChargeResult, ProviderAdapter
from cascade.domain.taxonomy import NormalizedReason
from cascade.domain.transitions import ATTEMPT_MONEY_FREE, PAYMENT_TERMINAL

log = logging.getLogger(__name__)

A = AttemptStatus
P = PaymentStatus

UNAVAILABLE = NormalizedReason.PROVIDER_UNAVAILABLE.value


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Step(Enum):
    STOP = auto()
    CONTINUE = auto()


class CascadeEngine:
    def __init__(
        self,
        *,
        config: RoutingConfig,
        repo: PaymentRepository,
        adapters: Mapping[str, ProviderAdapter],
        breaker: CircuitBreaker,
        queue: TaskQueue,
        clock: Clock = utcnow,
    ) -> None:
        self.cfg = config
        self.c = config.cascade
        self.repo = repo
        self.adapters = adapters
        self.breaker = breaker
        self.queue = queue
        self.clock = clock

    # ------------------------------------------------------------------ entry points

    async def process(self, payment_id: UUID) -> None:
        """Drive a payment as far as it can go now. Safe to call concurrently and repeatedly."""
        p = await self.repo.get(payment_id)
        if p is None or p.status in PAYMENT_TERMINAL:
            return
        if p.status is P.CREATED and not p.route:
            if not await self._snapshot_route(p):
                return
        token = uuid4()
        prev = await self.repo.claim(payment_id, token, self.c.lease_s)
        if prev is None:
            return  # someone else holds the lease (or the payment moved on)
        try:
            if prev is P.PENDING_RECONCILIATION:
                await self._reconcile(payment_id, token)
            else:
                await self._drive(payment_id, token)
        except LostLease:
            log.info("lease lost, stopping", extra={"payment_id": str(payment_id)})

    async def sweep(self, limit: int = 100) -> int:
        """Recovery: expired leases (crashed workers), due reconciliations, stuck CREATED."""
        ids = await self.repo.due_for_recovery(created_grace_s=self.c.lease_s, limit=limit)
        for pid in ids:
            try:
                await self.process(pid)
            except Exception:
                log.exception("sweep: processing failed", extra={"payment_id": str(pid)})
        return len(ids)

    # ------------------------------------------------------------------ route

    async def build_route(self, p: Payment) -> list[str]:
        rule = select_rule(
            self.cfg,
            amount_minor=p.amount_minor,
            currency=p.currency,
            country=p.pm_country,
            bin_=p.pm_bin,
        )
        route: list[str] = []
        for name in expand(rule.providers, p.id):
            pc = self.cfg.providers[name]
            if not pc.enabled or name not in self.adapters:
                continue
            if await self.breaker.allow(name):
                route.append(name)
        return route

    async def _snapshot_route(self, p: Payment) -> bool:
        route = await self.build_route(p)
        if not route:
            await self.repo.finish_payment(p.id, P.CREATED, P.FAILED, None, reason="no_route")
            return False
        await self.repo.set_route(p.id, route)  # no-op if a concurrent caller won
        return True

    # ------------------------------------------------------------------ main loop (8.1)

    async def _drive(self, pid: UUID, token: UUID) -> None:
        for _ in range(4 * self.c.max_attempts + 8):
            p = await self.repo.get(pid)
            assert p is not None
            if p.status is not P.PROCESSING:
                return  # settled by someone else (webhook)
            attempts = await self.repo.attempts(pid)
            last = attempts[-1] if attempts else None

            if last is not None:
                if last.status is A.SUCCEEDED:
                    return
                if last.status is A.DECLINED_HARD:
                    await self._fail(pid, token, last.reason or "hard_decline")
                    return
                if last.status is A.STARTED:
                    # Our own write was lost, or a previous worker crashed mid-call.
                    await self.repo.update_attempt(
                        last.id, A.STARTED, A.UNKNOWN, AttemptUpdate(), token
                    )
                    continue
                if last.status is A.UNKNOWN:
                    if await self._resolve_unknown(p, last, token) is _Step.STOP:
                        return
                    continue
                assert last.status in ATTEMPT_MONEY_FREE

            n = len(attempts)
            if self.clock() >= p.deadline_at:
                await self._fail(pid, token, (last.reason if last else None) or "deadline_exceeded")
                return
            if n >= len(p.route) or n >= self.c.max_attempts:
                await self._fail(pid, token, (last.reason if last else None) or "route_exhausted")
                return

            provider = p.route[n]
            attempt = await self.repo.start_attempt(pid, token, n + 1, provider)  # write-ahead
            result = await self._charge(provider, attempt, p)
            await self.breaker.record(provider, result.outcome)
            await self._persist_charge(attempt, result, token)
        raise RuntimeError(f"cascade loop did not converge for payment {pid}")

    async def _charge(self, provider: str, attempt: Attempt, p: Payment) -> ChargeResult:
        adapter = self.adapters.get(provider)
        if adapter is None:  # removed from config after the route was snapshotted
            return ChargeResult(ChargeOutcome.FAILED_NOT_CHARGED, raw_message="no adapter")
        req = ChargeRequest(str(attempt.id), p.amount_minor, p.currency, p.pm_token)
        return await adapter.charge(req)

    async def _persist_charge(self, attempt: Attempt, r: ChargeResult, token: UUID) -> None:
        data = AttemptUpdate(
            reason=r.reason.value if r.reason else None,
            raw_code=r.raw_code,
            raw_message=r.raw_message,
            provider_txn_id=r.provider_txn_id,
        )
        to = attempt_status_for_charge(r.outcome)
        if to is A.SUCCEEDED:
            await self.repo.succeed(attempt.payment_id, attempt.id, A.STARTED, data, token)
        else:
            if to is A.FAILED_NOT_CHARGED and data.reason is None:
                data = AttemptUpdate(UNAVAILABLE, r.raw_code, r.raw_message, r.provider_txn_id)
            await self.repo.update_attempt(attempt.id, A.STARTED, to, data, token)
        # If the conditional write did not apply, the next loop iteration sees why.

    async def _fail(self, pid: UUID, token: UUID, reason: str) -> None:
        await self.repo.finish_payment(pid, P.PROCESSING, P.FAILED, token, reason=reason)

    # ------------------------------------------------------------------ unknown outcome (8.2)

    async def _apply_lookup(self, adapter: ProviderAdapter, a: Attempt, token: UUID) -> bool:
        """One lookup. True if it resolved the attempt (and the result is persisted)."""
        lr = await adapter.lookup(str(a.id))
        to = attempt_status_for_lookup(lr.status, lr.reason)
        if to is None:
            return False
        data = AttemptUpdate(
            reason=lr.reason.value if lr.reason else (UNAVAILABLE if to is A.VOIDED else None),
            raw_code=lr.raw_code,
            provider_txn_id=lr.provider_txn_id,
        )
        if to is A.SUCCEEDED:
            await self.repo.succeed(a.payment_id, a.id, A.UNKNOWN, data, token)
        else:
            await self.repo.update_attempt(a.id, A.UNKNOWN, to, data, token)
        return True

    async def _resolve_unknown(self, p: Payment, a: Attempt, token: UUID) -> _Step:
        """Move past an UNKNOWN attempt only once it provably holds no money and never will.

        lookup (with retries) -> cancel (only if cancel blocks late charges) -> otherwise
        park the payment in PENDING_RECONCILIATION and do NOT try the next provider.
        """
        adapter = self.adapters.get(a.provider)
        if adapter is not None:
            for i in range(self.c.lookup_retries):
                if await self._apply_lookup(adapter, a, token):
                    return _Step.CONTINUE
                if i + 1 < self.c.lookup_retries:
                    await asyncio.sleep(self.c.lookup_backoff_s * 2**i)
            cr = await adapter.cancel(str(a.id))
            if cr.status is CancelStatus.CANCELLED and adapter.cancel_blocks_late_charge:
                await self.repo.update_attempt(
                    a.id, A.UNKNOWN, A.VOIDED, AttemptUpdate(reason=UNAVAILABLE), token
                )
                return _Step.CONTINUE
        log.warning(
            "unknown outcome not resolvable now, parking for reconciliation",
            extra={"payment_id": str(p.id), "attempt_id": str(a.id), "provider": a.provider},
        )
        await self.repo.finish_payment(
            p.id, P.PROCESSING, P.PENDING_RECONCILIATION, token, next_check_s=0
        )
        await self._enqueue(p.id)
        return _Step.STOP

    async def _enqueue(self, pid: UUID) -> None:
        try:
            await self.queue.enqueue_reconcile(pid)
        except Exception:  # the periodic sweep picks it up anyway
            log.exception("enqueue failed; sweep will pick the payment up")

    # ------------------------------------------------------------------ reconciliation (8.3)

    async def _reconcile(self, pid: UUID, token: UUID) -> None:
        attempts = await self.repo.attempts(pid)
        last = attempts[-1] if attempts else None
        if last is None or last.status not in (A.UNKNOWN, A.STARTED):
            # Resolved meanwhile (e.g. by a webhook): continue the normal cascade.
            await self._drive(pid, token)
            return
        if last.status is A.STARTED:
            await self.repo.update_attempt(last.id, A.STARTED, A.UNKNOWN, AttemptUpdate(), token)
            last = await self.repo.get_attempt(last.id)
            assert last is not None
        adapter = self.adapters.get(last.provider)
        if adapter is not None and await self._apply_lookup(adapter, last, token):
            # APPROVED -> payment already SUCCEEDED; DECLINED/VOIDED -> the loop resumes the
            # cascade if the deadline allows, otherwise fails with the last reason.
            await self._drive(pid, token)
            return

        age = (self.clock() - last.started_at).total_seconds()
        if age >= self.c.unknown_max_age_s:
            if adapter is not None:
                cr = await adapter.cancel(str(last.id))
                if cr.status is CancelStatus.CANCELLED and adapter.cancel_blocks_late_charge:
                    await self.repo.update_attempt(
                        last.id, A.UNKNOWN, A.VOIDED, AttemptUpdate(reason=UNAVAILABLE), token
                    )
            log.error(
                "unknown outcome exceeded max age; failing for manual review",
                extra={"payment_id": str(pid), "attempt_id": str(last.id)},
            )
            await self.repo.finish_payment(
                pid, P.PROCESSING, P.FAILED, token, reason="unknown_outcome", needs_review=True
            )
            return

        backoff = min(self.c.reconcile_backoff_max_s, max(self.c.reconcile_backoff_min_s, age / 2))
        await self.repo.finish_payment(
            pid, P.PROCESSING, P.PENDING_RECONCILIATION, token, next_check_s=backoff
        )
