"""Use cases: create a payment idempotently (8.1 step 1), apply provider events (8.3, 8.4)."""

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from cascade.application.engine import CascadeEngine
from cascade.application.ports import AttemptUpdate, PaymentRepository, TaskQueue
from cascade.domain.entities import Attempt, NewPayment, Payment
from cascade.domain.enums import AttemptStatus, CancelStatus, LookupStatus, PaymentStatus
from cascade.domain.money import Money
from cascade.domain.outcomes import attempt_status_for_decline, attempt_status_for_lookup
from cascade.domain.providers import LookupResult, ProviderAdapter
from cascade.domain.taxonomy import NormalizedReason
from cascade.domain.transitions import ATTEMPT_UNRESOLVED, transition_attempt

log = logging.getLogger(__name__)

A = AttemptStatus
P = PaymentStatus


class IdempotencyConflict(Exception):
    """Same Idempotency-Key, different request body."""


@dataclass(frozen=True, slots=True)
class CreatePayment:
    idempotency_key: str
    money: Money
    pm_token: str
    pm_bin: str | None
    pm_country: str | None

    def request_hash(self) -> str:
        body = {
            "amount_minor": self.money.amount_minor,
            "currency": self.money.currency,
            "payment_method": {
                "token": self.pm_token,
                "bin": self.pm_bin,
                "country": self.pm_country,
            },
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class PaymentService:
    def __init__(self, repo: PaymentRepository, engine: CascadeEngine) -> None:
        self.repo = repo
        self.engine = engine

    async def create(self, cmd: CreatePayment) -> tuple[Payment, list[Attempt]]:
        new = NewPayment(
            id=uuid4(),
            idempotency_key=cmd.idempotency_key,
            request_hash=cmd.request_hash(),
            amount_minor=cmd.money.amount_minor,
            currency=cmd.money.currency,
            pm_token=cmd.pm_token,
            pm_bin=cmd.pm_bin,
            pm_country=cmd.pm_country,
            deadline_at=self.engine.clock() + timedelta(seconds=self.engine.c.budget_s),
        )
        payment, created = await self.repo.insert_payment(new)
        if not created:
            if payment.request_hash != new.request_hash:
                raise IdempotencyConflict(cmd.idempotency_key)
            return payment, await self.repo.attempts(payment.id)  # replay: no new processing
        await self.engine.process(payment.id)
        return await self.get(payment.id)

    async def get(self, payment_id: UUID) -> tuple[Payment, list[Attempt]]:
        p = await self.repo.get(payment_id)
        if p is None:
            raise KeyError(payment_id)
        return p, await self.repo.attempts(payment_id)


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """Normalized provider notification (from a webhook)."""

    provider: str
    reference: str  # = attempt id
    succeeded: bool
    reason: NormalizedReason | None = None  # for declines
    raw_code: str | None = None
    provider_txn_id: str | None = None


class ProviderEvents:
    """Applies provider-reported outcomes through the same transitions as polling."""

    def __init__(
        self,
        repo: PaymentRepository,
        adapters: Mapping[str, ProviderAdapter],
        queue: TaskQueue,
    ) -> None:
        self.repo = repo
        self.adapters = adapters
        self.queue = queue

    async def apply(self, ev: ProviderEvent) -> str:
        try:
            attempt_id = UUID(ev.reference)
        except ValueError:
            return "ignored_unknown_reference"
        a = await self.repo.get_attempt(attempt_id)
        if a is None or a.provider != ev.provider:
            return "ignored_unknown_reference"
        p = await self.repo.get(a.payment_id)
        assert p is not None
        if ev.succeeded:
            return await self._on_success(p, a, ev)
        return await self._on_decline(p, a, ev)

    async def _on_success(self, p: Payment, a: Attempt, ev: ProviderEvent) -> str:
        """A success webhook is a hint, not the truth: it can be stale (sent before our own
        cancel voided the charge). The provider's current state comes from lookup()."""
        if a.status in (A.SUCCEEDED, A.REFUNDED_DUPLICATE):
            return "duplicate"
        adapter = self.adapters.get(a.provider)
        lr = await adapter.lookup(str(a.id)) if adapter else LookupResult(LookupStatus.UNKNOWN)

        if a.status in ATTEMPT_UNRESOLVED and p.status in (P.PROCESSING, P.PENDING_RECONCILIATION):
            to = attempt_status_for_lookup(lr.status, lr.reason)
            if to is None:  # cannot verify now: reconciliation will
                if p.status is P.PENDING_RECONCILIATION:
                    await self.queue.enqueue_reconcile(p.id)
                return "deferred"
            data = AttemptUpdate(
                reason=lr.reason.value if lr.reason else None,
                raw_code=lr.raw_code,
                provider_txn_id=lr.provider_txn_id or ev.provider_txn_id,
            )
            if to is A.SUCCEEDED:
                applied = await self.repo.succeed(p.id, a.id, a.status, data, token=None)
            else:
                applied = await self.repo.update_attempt(a.id, a.status, to, data, token=None)
                if applied and p.status is P.PENDING_RECONCILIATION:
                    await self.queue.enqueue_reconcile(p.id)  # resume or fail the cascade
            if applied:
                return "applied" if to is A.SUCCEEDED else "verified_not_charged"
            a = await self.repo.get_attempt(a.id) or a  # raced: decide on fresh state
            if a.status in (A.SUCCEEDED, A.REFUNDED_DUPLICATE):
                return "duplicate"
            p = await self.repo.get(p.id) or p

        if lr.status in (LookupStatus.VOIDED, LookupStatus.DECLINED, LookupStatus.NOT_FOUND):
            return "ignored_stale"  # provider says nothing is charged now
        return await self.handle_late_success(p, a)  # APPROVED, or UNKNOWN: cancel to be safe

    async def handle_late_success(self, p: Payment, a: Attempt) -> str:
        """Provider charged an attempt we already settled otherwise (8.3). Undo that charge.

        Net charges stay at one: cancel the late one and mark it REFUNDED_DUPLICATE, or, if
        the cancel does not go through, flag the payment for manual review.
        """
        if not transition_attempt(a.status, A.REFUNDED_DUPLICATE).applied:
            return "ignored_stale"
        log.warning(
            "late success on settled attempt; cancelling it",
            extra={"payment_id": str(p.id), "attempt_id": str(a.id), "provider": a.provider},
        )
        adapter = self.adapters.get(a.provider)
        cancelled = (
            adapter is not None
            and (await adapter.cancel(str(a.id))).status is CancelStatus.CANCELLED
        )
        if cancelled and await self.repo.update_attempt(
            a.id, a.status, A.REFUNDED_DUPLICATE, AttemptUpdate(), token=None
        ):
            return "late_success_refunded"
        await self.repo.flag_needs_review(p.id)
        log.error(
            "late success could not be cancelled; needs review",
            extra={"payment_id": str(p.id), "attempt_id": str(a.id)},
        )
        return "late_success_needs_review"

    async def _on_decline(self, p: Payment, a: Attempt, ev: ProviderEvent) -> str:
        to = attempt_status_for_decline(ev.reason or NormalizedReason.UNMAPPED)
        t = transition_attempt(a.status, to)
        if not t.applied:
            return "duplicate" if a.status == to else "ignored_stale"
        data = AttemptUpdate(
            reason=(ev.reason or NormalizedReason.UNMAPPED).value, raw_code=ev.raw_code
        )
        if not await self.repo.update_attempt(a.id, a.status, to, data, token=None):
            return "ignored_stale"
        if p.status is P.PENDING_RECONCILIATION:
            await self.queue.enqueue_reconcile(p.id)  # resume (or fail) the cascade
        return "applied"
