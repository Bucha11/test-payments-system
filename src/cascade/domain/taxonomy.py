"""Decline taxonomy.

The single question behind the table: "could a different provider/acquirer plausibly
approve this?" Soft = yes, cascade to the next provider. Hard = no, stop.

Adapters own the mapping raw provider code -> NormalizedReason (via `map_raw_code`).
The class is derived from the reason here, in exactly one table.
"""

import logging
from collections.abc import Mapping
from enum import StrEnum

log = logging.getLogger(__name__)


class DeclineClass(StrEnum):
    SOFT = "soft"
    HARD = "hard"


class NormalizedReason(StrEnum):
    # soft: another provider could plausibly approve
    GENERIC_DECLINE = "generic_decline"
    DO_NOT_HONOR = "do_not_honor"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    ISSUER_UNAVAILABLE = "issuer_unavailable"
    PROVIDER_RISK_REJECT = "provider_risk_reject"
    UNSUPPORTED_CARD_OR_CURRENCY = "unsupported_card_or_currency"
    RATE_LIMITED = "rate_limited"
    # hard: the card/customer is the problem, nobody will approve it
    INSUFFICIENT_FUNDS = "insufficient_funds"
    EXPIRED_CARD = "expired_card"
    INVALID_CARD = "invalid_card"
    INVALID_CVV = "invalid_cvv"
    LOST_OR_STOLEN = "lost_or_stolen"
    PICKUP_CARD = "pickup_card"
    CLOSED_ACCOUNT = "closed_account"
    AUTHENTICATION_FAILED = "authentication_failed"
    # a raw code the adapter does not know: fail safe
    UNMAPPED = "unmapped"


_S, _H = DeclineClass.SOFT, DeclineClass.HARD

DECLINE_CLASS: Mapping[NormalizedReason, DeclineClass] = {
    NormalizedReason.GENERIC_DECLINE: _S,
    NormalizedReason.DO_NOT_HONOR: _S,
    NormalizedReason.PROVIDER_UNAVAILABLE: _S,
    NormalizedReason.ISSUER_UNAVAILABLE: _S,
    NormalizedReason.PROVIDER_RISK_REJECT: _S,
    NormalizedReason.UNSUPPORTED_CARD_OR_CURRENCY: _S,
    NormalizedReason.RATE_LIMITED: _S,
    NormalizedReason.INSUFFICIENT_FUNDS: _H,
    NormalizedReason.EXPIRED_CARD: _H,
    NormalizedReason.INVALID_CARD: _H,
    NormalizedReason.INVALID_CVV: _H,
    NormalizedReason.LOST_OR_STOLEN: _H,
    NormalizedReason.PICKUP_CARD: _H,
    NormalizedReason.CLOSED_ACCOUNT: _H,
    NormalizedReason.AUTHENTICATION_FAILED: _H,
    NormalizedReason.UNMAPPED: _H,
}


def classify(reason: NormalizedReason) -> DeclineClass:
    return DECLINE_CLASS[reason]


def is_cascadable(reason: NormalizedReason) -> bool:
    return classify(reason) is DeclineClass.SOFT


def map_raw_code(
    provider: str, table: Mapping[str, NormalizedReason], raw_code: str | None
) -> NormalizedReason:
    """Adapter helper: raw provider code -> normalized reason.

    Unknown codes become UNMAPPED (hard): retrying an unknown decline on another
    provider can violate card-scheme retry rules, so we fail safe and log it so the
    mapping table gets extended.
    """
    if raw_code is not None and raw_code in table:
        return table[raw_code]
    log.warning(
        "unmapped decline code, treating as hard",
        extra={"provider": provider, "raw_code": raw_code},
    )
    return NormalizedReason.UNMAPPED
