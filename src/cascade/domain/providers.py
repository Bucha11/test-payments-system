"""Provider adapter contract (spec section 7).

Adapters translate a provider's API into these types. Rules:
* every call has an explicit timeout; adapters never retry (retry policy = engine);
* FAILED_NOT_CHARGED only when it is certain the request was not processed;
  a read timeout, a reset after send or an ambiguous 5xx is UNKNOWN;
* never log pm_token or credentials.
"""

from dataclasses import dataclass, field
from typing import Protocol

from cascade.domain.enums import CancelStatus, ChargeOutcome, LookupStatus
from cascade.domain.taxonomy import DeclineClass, NormalizedReason, classify


@dataclass(frozen=True, slots=True)
class ChargeRequest:
    reference: str  # = attempt id; the provider-side idempotency key
    amount_minor: int
    currency: str
    pm_token: str = field(repr=False)  # never printed


@dataclass(frozen=True, slots=True)
class ChargeResult:
    outcome: ChargeOutcome
    reason: NormalizedReason | None = None
    provider_txn_id: str | None = None
    raw_code: str | None = None
    raw_message: str | None = None

    def __post_init__(self) -> None:
        declined = self.outcome in (ChargeOutcome.SOFT_DECLINE, ChargeOutcome.HARD_DECLINE)
        if declined:
            if self.reason is None:
                raise ValueError("a decline needs a normalized reason")
            expected = (
                ChargeOutcome.SOFT_DECLINE
                if classify(self.reason) is DeclineClass.SOFT
                else ChargeOutcome.HARD_DECLINE
            )
            if self.outcome is not expected:
                raise ValueError(f"{self.reason} is {classify(self.reason)}, not {self.outcome}")

    @classmethod
    def declined(
        cls,
        reason: NormalizedReason,
        raw_code: str | None = None,
        raw_message: str | None = None,
        provider_txn_id: str | None = None,
    ) -> "ChargeResult":
        """Build a decline; soft/hard is derived from the taxonomy, never chosen by adapters."""
        outcome = (
            ChargeOutcome.SOFT_DECLINE
            if classify(reason) is DeclineClass.SOFT
            else ChargeOutcome.HARD_DECLINE
        )
        return cls(outcome, reason, provider_txn_id, raw_code, raw_message)


@dataclass(frozen=True, slots=True)
class LookupResult:
    status: LookupStatus
    reason: NormalizedReason | None = None  # set when DECLINED
    provider_txn_id: str | None = None
    raw_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is LookupStatus.DECLINED and self.reason is None:
            raise ValueError("DECLINED lookup needs a normalized reason")


@dataclass(frozen=True, slots=True)
class CancelResult:
    status: CancelStatus


class ProviderAdapter(Protocol):
    name: str
    # True if cancel(ref) guarantees `ref` can never be charged later (tombstone).
    cancel_blocks_late_charge: bool

    async def charge(self, req: ChargeRequest) -> ChargeResult: ...

    async def lookup(self, reference: str) -> LookupResult: ...

    async def cancel(self, reference: str) -> CancelResult: ...
