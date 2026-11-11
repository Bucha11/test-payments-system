from enum import StrEnum


class PaymentStatus(StrEnum):
    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AttemptStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    DECLINED_SOFT = "DECLINED_SOFT"
    DECLINED_HARD = "DECLINED_HARD"
    FAILED_NOT_CHARGED = "FAILED_NOT_CHARGED"
    UNKNOWN = "UNKNOWN"
    VOIDED = "VOIDED"
    REFUNDED_DUPLICATE = "REFUNDED_DUPLICATE"


class ChargeOutcome(StrEnum):
    APPROVED = "APPROVED"
    SOFT_DECLINE = "SOFT_DECLINE"
    HARD_DECLINE = "HARD_DECLINE"
    FAILED_NOT_CHARGED = "FAILED_NOT_CHARGED"
    UNKNOWN = "UNKNOWN"


class LookupStatus(StrEnum):
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    # Extension of section 7: the reference existed and was voided (e.g. our own earlier
    # cancel went through but its response was lost). Holds no money, cannot be charged.
    VOIDED = "VOIDED"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


class CancelStatus(StrEnum):
    CANCELLED = "CANCELLED"
    NOT_CANCELLABLE = "NOT_CANCELLABLE"
    UNKNOWN = "UNKNOWN"


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
