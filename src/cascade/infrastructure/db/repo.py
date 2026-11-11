"""PostgreSQL implementation of PaymentRepository.

Each public method is one short transaction. Status changes are conditional UPDATEs
(`WHERE status = :from`), validated first by the domain transition functions, so two
writers racing on one row cannot both apply a move. Engine writes are additionally
fenced by the claim token (`lease_owner`).
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cascade.application.ports import AttemptUpdate, LostLease
from cascade.domain.entities import Attempt, NewPayment, Payment
from cascade.domain.enums import AttemptStatus, PaymentStatus
from cascade.domain.transitions import transition_attempt, transition_payment
from cascade.infrastructure.db.models import AttemptRow, PaymentRow, WebhookEventRow

P = PaymentStatus
A = AttemptStatus


def _payment(r: PaymentRow) -> Payment:
    return Payment(
        id=r.id,
        idempotency_key=r.idempotency_key,
        request_hash=r.request_hash,
        amount_minor=r.amount_minor,
        currency=r.currency,
        pm_token=r.pm_token,
        pm_bin=r.pm_bin,
        pm_country=r.pm_country,
        status=PaymentStatus(r.status),
        route=tuple(r.route),
        deadline_at=r.deadline_at,
        lease_until=r.lease_until,
        needs_review=r.needs_review,
        failure_reason=r.failure_reason,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _attempt(r: AttemptRow) -> Attempt:
    return Attempt(
        id=r.id,
        payment_id=r.payment_id,
        seq=r.seq,
        provider=r.provider,
        status=AttemptStatus(r.status),
        provider_txn_id=r.provider_txn_id,
        raw_code=r.raw_code,
        raw_message=r.raw_message,
        reason=r.reason,
        started_at=r.started_at,
        finished_at=r.finished_at,
    )


_CLAIM = text(
    """
    WITH prev AS (SELECT id, status FROM payments WHERE id = :id FOR UPDATE)
    UPDATE payments p
       SET status = 'PROCESSING', lease_owner = :token,
           lease_until = now() + make_interval(secs => :lease), updated_at = now()
      FROM prev
     WHERE p.id = prev.id
       AND (prev.status = 'CREATED'
            OR (prev.status IN ('PROCESSING', 'PENDING_RECONCILIATION')
                AND (p.lease_until IS NULL OR p.lease_until < now())))
    RETURNING prev.status::text
    """
)

_DUE = text(
    """
    SELECT id FROM payments
     WHERE (status = 'PROCESSING' AND lease_until < now())
        OR (status = 'PENDING_RECONCILIATION' AND (lease_until IS NULL OR lease_until < now()))
        OR (status = 'CREATED' AND created_at < now() - make_interval(secs => :grace))
     ORDER BY updated_at
     LIMIT :limit
    """
)


class _Abort(Exception):
    """Roll back the current transaction (raised inside `sm.begin()`)."""


class SqlPaymentRepository:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], lease_s: float) -> None:
        self.sm = sessionmaker
        self.lease_s = lease_s

    async def _fence(self, s: AsyncSession, payment_id: UUID, token: UUID | None) -> None:
        """Check (and renew) our claim inside the caller's transaction; row lock serializes."""
        if token is None:
            return
        res = await s.execute(
            update(PaymentRow)
            .where(
                PaymentRow.id == payment_id,
                PaymentRow.lease_owner == token,
                PaymentRow.status == P.PROCESSING,
            )
            .values(lease_until=func.now() + timedelta(seconds=self.lease_s), updated_at=func.now())
            .returning(PaymentRow.id)
        )
        if res.first() is None:
            raise LostLease(str(payment_id))

    # ---------------------------------------------------------------- reads

    async def get(self, payment_id: UUID) -> Payment | None:
        async with self.sm() as s:
            r = await s.get(PaymentRow, payment_id)
            return _payment(r) if r else None

    async def attempts(self, payment_id: UUID) -> list[Attempt]:
        async with self.sm() as s:
            rows = await s.scalars(
                select(AttemptRow)
                .where(AttemptRow.payment_id == payment_id)
                .order_by(AttemptRow.seq)
            )
            return [_attempt(r) for r in rows]

    async def get_attempt(self, attempt_id: UUID) -> Attempt | None:
        async with self.sm() as s:
            r = await s.get(AttemptRow, attempt_id)
            return _attempt(r) if r else None

    # ---------------------------------------------------------------- payments

    async def insert_payment(self, new: NewPayment) -> tuple[Payment, bool]:
        async with self.sm.begin() as s:
            stmt = (
                pg_insert(PaymentRow)
                .values(
                    id=new.id,
                    idempotency_key=new.idempotency_key,
                    request_hash=new.request_hash,
                    amount_minor=new.amount_minor,
                    currency=new.currency,
                    pm_token=new.pm_token,
                    pm_bin=new.pm_bin,
                    pm_country=new.pm_country,
                    status=P.CREATED,
                    deadline_at=new.deadline_at,
                )
                .on_conflict_do_nothing(index_elements=[PaymentRow.idempotency_key])
                .returning(PaymentRow.id)
            )
            created = (await s.execute(stmt)).first() is not None
        async with self.sm() as s:
            row = await s.scalar(
                select(PaymentRow).where(PaymentRow.idempotency_key == new.idempotency_key)
            )
            assert row is not None
            return _payment(row), created

    async def set_route(self, payment_id: UUID, route: Sequence[str]) -> bool:
        async with self.sm.begin() as s:
            res = await s.execute(
                update(PaymentRow)
                .where(
                    PaymentRow.id == payment_id,
                    PaymentRow.status == P.CREATED,
                    PaymentRow.route == text("'[]'::jsonb"),
                )
                .values(route=list(route), updated_at=func.now())
                .returning(PaymentRow.id)
            )
            return res.first() is not None

    async def claim(self, payment_id: UUID, token: UUID, lease_s: float) -> PaymentStatus | None:
        async with self.sm.begin() as s:
            row = (
                await s.execute(_CLAIM, {"id": payment_id, "token": token, "lease": lease_s})
            ).first()
            return PaymentStatus(row[0]) if row else None

    async def finish_payment(
        self,
        payment_id: UUID,
        frm: PaymentStatus,
        to: PaymentStatus,
        token: UUID | None,
        *,
        reason: str | None = None,
        needs_review: bool = False,
        next_check_s: float = 0,
    ) -> bool:
        transition_payment(frm, to).require()
        values: dict[str, Any] = {
            "status": to,
            "lease_owner": None,
            "updated_at": func.now(),
            "needs_review": PaymentRow.needs_review | needs_review,
            "failure_reason": reason if to is P.FAILED else None,
        }
        # PENDING_RECONCILIATION reuses lease_until as "not before" for the next check.
        values["lease_until"] = (
            func.now() + timedelta(seconds=next_check_s) if to is P.PENDING_RECONCILIATION else None
        )
        async with self.sm.begin() as s:
            conds = [PaymentRow.id == payment_id, PaymentRow.status == frm]
            if token is not None:
                conds.append(PaymentRow.lease_owner == token)
            res = await s.execute(
                update(PaymentRow).where(*conds).values(**values).returning(PaymentRow.id)
            )
            ok = res.first() is not None
            if not ok and token is not None:
                raise LostLease(str(payment_id))
            return ok

    async def flag_needs_review(self, payment_id: UUID) -> None:
        async with self.sm.begin() as s:
            await s.execute(
                update(PaymentRow)
                .where(PaymentRow.id == payment_id)
                .values(needs_review=True, updated_at=func.now())
            )

    async def due_for_recovery(self, created_grace_s: float, limit: int) -> list[UUID]:
        async with self.sm() as s:
            rows = await s.execute(_DUE, {"grace": created_grace_s, "limit": limit})
            return [r[0] for r in rows]

    # ---------------------------------------------------------------- attempts

    async def start_attempt(
        self, payment_id: UUID, token: UUID, seq: int, provider: str
    ) -> Attempt:
        """Write-ahead: the attempt row is committed BEFORE the provider is called."""
        async with self.sm.begin() as s:
            await self._fence(s, payment_id, token)
            row = (
                await s.scalars(
                    pg_insert(AttemptRow)
                    .values(
                        id=uuid4(),
                        payment_id=payment_id,
                        seq=seq,
                        provider=provider,
                        status=A.STARTED,
                    )
                    .returning(AttemptRow)
                )
            ).one()
            return _attempt(row)

    async def update_attempt(
        self,
        attempt_id: UUID,
        frm: AttemptStatus,
        to: AttemptStatus,
        data: AttemptUpdate,
        token: UUID | None,
    ) -> bool:
        transition_attempt(frm, to).require()
        async with self.sm.begin() as s:
            if token is not None:
                pid = await s.scalar(
                    select(AttemptRow.payment_id).where(AttemptRow.id == attempt_id)
                )
                assert pid is not None
                await self._fence(s, pid, token)
            res = await s.execute(
                update(AttemptRow)
                .where(AttemptRow.id == attempt_id, AttemptRow.status == frm)
                .values(**self._attempt_values(to, data))
                .returning(AttemptRow.id)
            )
            return res.first() is not None

    async def succeed(
        self,
        payment_id: UUID,
        attempt_id: UUID,
        frm: AttemptStatus,
        data: AttemptUpdate,
        token: UUID | None,
    ) -> bool:
        """Attempt -> SUCCEEDED and payment -> SUCCEEDED atomically, or nothing."""
        transition_attempt(frm, A.SUCCEEDED).require()
        try:
            async with self.sm.begin() as s:
                await self._fence(s, payment_id, token)
                res = await s.execute(
                    update(AttemptRow)
                    .where(AttemptRow.id == attempt_id, AttemptRow.status == frm)
                    .values(**self._attempt_values(A.SUCCEEDED, data))
                    .returning(AttemptRow.id)
                )
                if res.first() is None:
                    raise _Abort
                conds = [
                    PaymentRow.id == payment_id,
                    PaymentRow.status.in_([P.PROCESSING, P.PENDING_RECONCILIATION]),
                ]
                if token is not None:
                    conds.append(PaymentRow.lease_owner == token)
                res = await s.execute(
                    update(PaymentRow)
                    .where(*conds)
                    .values(
                        status=P.SUCCEEDED,
                        lease_owner=None,
                        lease_until=None,
                        failure_reason=None,
                        updated_at=func.now(),
                    )
                    .returning(PaymentRow.id)
                )
                if res.first() is None:
                    raise _Abort
                return True
        except _Abort:
            return False
        except IntegrityError:  # uq_attempts_one_success: another attempt already won
            return False

    @staticmethod
    def _attempt_values(to: AttemptStatus, d: AttemptUpdate) -> dict[str, Any]:
        v: dict[str, Any] = {
            "status": to,
            "finished_at": func.coalesce(AttemptRow.finished_at, func.now()),
        }
        if d.reason is not None:
            v["reason"] = d.reason
        if d.raw_code is not None:
            v["raw_code"] = d.raw_code
        if d.raw_message is not None:
            v["raw_message"] = d.raw_message
        if d.provider_txn_id is not None:
            v["provider_txn_id"] = d.provider_txn_id
        return v

    # ---------------------------------------------------------------- webhooks

    async def claim_webhook(self, provider: str, event_id: str, payload: dict[str, Any]) -> bool:
        """True if the event still has to be applied; False if it already was (duplicate).

        A row without `processed_at` means an earlier delivery was received but not applied
        (it failed, or the process died in between): the redelivery applies it again.
        Applying is idempotent (conditional transitions), so a concurrent redelivery that
        also gets True is harmless.
        """
        async with self.sm.begin() as s:
            await s.execute(
                pg_insert(WebhookEventRow)
                .values(provider=provider, event_id=event_id, payload=payload)
                .on_conflict_do_nothing(constraint="uq_webhook_events")
            )
            processed = await s.scalar(
                select(WebhookEventRow.processed_at).where(
                    WebhookEventRow.provider == provider, WebhookEventRow.event_id == event_id
                )
            )
            return processed is None

    async def mark_webhook_processed(self, provider: str, event_id: str) -> None:
        async with self.sm.begin() as s:
            await s.execute(
                update(WebhookEventRow)
                .where(WebhookEventRow.provider == provider, WebhookEventRow.event_id == event_id)
                .values(processed_at=func.now())
            )
