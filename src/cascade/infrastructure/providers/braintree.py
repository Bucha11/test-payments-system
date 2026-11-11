"""Adapter for Braintree (sandbox or production) via the official SDK.

The SDK is synchronous (requests), so every call runs in a worker thread under an outer
asyncio timeout; the SDK's own HTTP timeout is set to the same value. A thread that
outlives the asyncio timeout is abandoned, and the outcome is UNKNOWN.

Mapping onto the adapter contract (domain/providers.py):

* charge = `transaction.sale` with `submit_for_settlement`; the attempt id goes into
  `order_id`, which is how lookup and cancel find the transaction later. Braintree has no
  idempotency key, so an attempt is never re-sent: the engine resolves an UNKNOWN attempt
  through lookup instead.
* lookup = transaction search by `order_id`.
* cancel = `void` while the transaction is not settled yet, `refund` once it is. Neither
  stops a request that is still in flight, so `cancel_blocks_late_charge` is False: an
  unknown outcome that lookup cannot resolve parks the payment for reconciliation instead
  of moving on to the next provider.

Transport classification (see domain/providers.py): only a connect timeout and errors
returned before processing (authentication, authorization, rate limit, validation) are
FAILED_NOT_CHARGED.
A read timeout, a dropped connection, 5xx and anything unexpected are UNKNOWN.

Braintree does not send webhooks for card sales, so this provider is reconciled by lookup
only.
"""

import asyncio
import logging
from collections.abc import Callable, Collection, Mapping
from decimal import Decimal
from typing import Any, TypeVar

import braintree
from braintree.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    TooManyRequestsError,
    UpgradeRequiredError,
)
from braintree.exceptions.http.timeout_error import ConnectTimeoutError

from cascade.domain.enums import CancelStatus, ChargeOutcome, LookupStatus
from cascade.domain.providers import CancelResult, ChargeRequest, ChargeResult, LookupResult
from cascade.domain.taxonomy import NormalizedReason, map_raw_code

log = logging.getLogger(__name__)

R = NormalizedReason
T = TypeVar("T")

# Processor response codes (https://developer.paypal.com/braintree/docs/reference/general/
# processor-responses/authorization-responses). The question is ours, not Braintree's
# `processor_response_type`: "could another provider approve this?". So 2001 insufficient
# funds is hard here even though Braintree calls it soft (= "retry later at Braintree").
# Codes not listed become UNMAPPED (hard) and are logged.
BRAINTREE_CODES: Mapping[str, NormalizedReason] = {
    "2000": R.DO_NOT_HONOR,
    "2001": R.INSUFFICIENT_FUNDS,
    "2002": R.INSUFFICIENT_FUNDS,  # limit exceeded
    "2003": R.INSUFFICIENT_FUNDS,  # cardholder's activity limit exceeded
    "2004": R.EXPIRED_CARD,
    "2005": R.INVALID_CARD,
    "2006": R.INVALID_CARD,  # invalid expiration date
    "2007": R.CLOSED_ACCOUNT,  # no account
    "2008": R.INVALID_CARD,
    "2009": R.INVALID_CARD,  # no such issuer
    "2010": R.INVALID_CVV,
    "2012": R.LOST_OR_STOLEN,
    "2013": R.LOST_OR_STOLEN,
    "2015": R.GENERIC_DECLINE,  # transaction not allowed (at this acquirer)
    "2016": R.GENERIC_DECLINE,  # duplicate transaction (Braintree's duplicate checking)
    "2038": R.GENERIC_DECLINE,  # processor declined
    "2044": R.DO_NOT_HONOR,  # declined - call issuer
    "2046": R.GENERIC_DECLINE,
    "2047": R.PICKUP_CARD,
    "2051": R.UNSUPPORTED_CARD_OR_CURRENCY,  # card type not enabled
    "2059": R.AUTHENTICATION_FAILED,  # address verification failed
    "2060": R.INVALID_CVV,  # address verification and CVV failed
    "2091": R.UNSUPPORTED_CARD_OR_CURRENCY,  # currency not supported by merchant account
    "2099": R.AUTHENTICATION_FAILED,  # cardholder authentication required (3-D Secure)
    "3000": R.PROVIDER_UNAVAILABLE,  # processor network unavailable, try again
    # Gateway rejections (`gateway_rejected:<reason>`): Braintree's own risk rules.
    "gateway_rejected:fraud": R.PROVIDER_RISK_REJECT,
    "gateway_rejected:risk_threshold": R.PROVIDER_RISK_REJECT,
    "gateway_rejected:duplicate": R.GENERIC_DECLINE,
    "gateway_rejected:cvv": R.INVALID_CVV,
    "gateway_rejected:avs": R.AUTHENTICATION_FAILED,
    "gateway_rejected:avs_and_cvv": R.INVALID_CVV,
    "gateway_rejected:three_d_secure": R.AUTHENTICATION_FAILED,
    "gateway_rejected:token_issuance": R.INVALID_CARD,
}

# Transaction statuses (https://developer.paypal.com/braintree/docs/reference/response/
# transaction#status).
_HOLDS_MONEY = frozenset(
    {
        "authorizing",
        "authorized",
        "submitted_for_settlement",
        "settlement_pending",
        "settling",
        "settled",
        "settlement_confirmed",
    }
)
_RELEASED = frozenset({"voided", "authorization_expired", "settlement_declined"})
_DECLINED = frozenset({"processor_declined", "gateway_rejected", "failed"})
_VOIDABLE = frozenset({"authorized", "submitted_for_settlement", "settlement_pending"})
_REFUNDABLE = frozenset({"settling", "settled", "settlement_confirmed"})

# Rejected before any processing: the request provably did not create a transaction.
_NOT_PROCESSED_ERRORS = (
    ConnectTimeoutError,
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    UpgradeRequiredError,
    TooManyRequestsError,
)

# Validation error codes about the payment method itself (nonce/token unusable).
_INVALID_PAYMENT_METHOD_CODES = frozenset({"91565", "91564", "91517", "91518", "91567"})

# Currencies with a minor unit other than 2 (ISO 4217). Everything else has 2.
_MINOR_UNITS = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "BHD": 3, "KWD": 3, "OMR": 3}

# Sandbox test nonces, usable many times: https://developer.paypal.com/braintree/docs/
# reference/general/testing/python#nonces-representing-cards
SANDBOX_DEFAULT_NONCE = "fake-valid-nonce"


def to_decimal_amount(amount_minor: int, currency: str) -> str:
    exp = _MINOR_UNITS.get(currency, 2)
    return str(Decimal(amount_minor).scaleb(-exp).quantize(Decimal(1).scaleb(-exp)))


class BraintreeAdapter:
    def __init__(
        self,
        name: str,
        gateway: braintree.BraintreeGateway,
        timeout_s: float,
        *,
        currencies: Collection[str] | None = None,
        merchant_account_id: str | None = None,
        sandbox: bool = False,
        cancel_blocks_late_charge: bool = False,
    ) -> None:
        self.name = name
        self.gateway = gateway
        self.timeout_s = timeout_s
        self.currencies = frozenset(currencies) if currencies else None
        self.merchant_account_id = merchant_account_id
        self.sandbox = sandbox
        self.cancel_blocks_late_charge = cancel_blocks_late_charge

    @classmethod
    def create(
        cls,
        name: str,
        *,
        merchant_id: str,
        public_key: str,
        private_key: str,
        environment: str,
        timeout_s: float,
        **kw: Any,
    ) -> "BraintreeAdapter":
        env = braintree.Environment.parse_environment(environment)
        gw = braintree.BraintreeGateway(
            braintree.Configuration(
                environment=env,
                merchant_id=merchant_id,
                public_key=public_key,
                private_key=private_key,
                timeout=timeout_s,
            )
        )
        return cls(name, gw, timeout_s, sandbox=env == braintree.Environment.Sandbox, **kw)

    async def _call(self, fn: Callable[..., T], *args: Any) -> T:
        async with asyncio.timeout(self.timeout_s):
            return await asyncio.to_thread(fn, *args)

    # ------------------------------------------------------------------ payment method

    def _payment_method(self, pm_token: str) -> dict[str, str]:
        """The service's opaque token -> a Braintree payment method.

        `bt_<token>` is a vaulted Braintree payment method token; a `fake-...` value is a
        sandbox nonce. In the sandbox any other token (e.g. the mocks' `tok_...`) is charged
        as a valid test card, so one payment can cascade between the mocks and Braintree.
        In production a network token or a token vault would map one customer card to each
        provider's own token; that is out of scope here.
        """
        if pm_token.startswith("bt_"):
            return {"payment_method_token": pm_token.removeprefix("bt_")}
        if pm_token.startswith("fake-") or not self.sandbox:
            return {"payment_method_nonce": pm_token}
        return {"payment_method_nonce": SANDBOX_DEFAULT_NONCE}

    # ------------------------------------------------------------------ charge

    async def charge(self, req: ChargeRequest) -> ChargeResult:
        if self.currencies is not None and req.currency not in self.currencies:
            # Nothing is sent: the merchant account cannot settle this currency.
            return ChargeResult.declined(
                R.UNSUPPORTED_CARD_OR_CURRENCY, raw_message=f"{req.currency} not enabled"
            )
        params: dict[str, Any] = {
            "amount": to_decimal_amount(req.amount_minor, req.currency),
            "order_id": req.reference,
            "options": {"submit_for_settlement": True},
            **self._payment_method(req.pm_token),
        }
        if self.merchant_account_id:
            params["merchant_account_id"] = self.merchant_account_id
        try:
            result = await self._call(self.gateway.transaction.sale, params)
        except _NOT_PROCESSED_ERRORS as e:
            reason = R.RATE_LIMITED if isinstance(e, TooManyRequestsError) else None
            return ChargeResult(
                ChargeOutcome.FAILED_NOT_CHARGED, reason=reason, raw_message=type(e).__name__
            )
        except Exception as e:  # read timeout, reset, 5xx, our own asyncio timeout, ...
            log.warning(
                "braintree charge outcome unknown",
                extra={"provider": self.name, "reference": req.reference, "error": repr(e)},
            )
            return ChargeResult(ChargeOutcome.UNKNOWN, raw_message=type(e).__name__ or "timeout")
        return self._charge_result(result)

    def _charge_result(self, result: Any) -> ChargeResult:
        t = result.transaction
        if result.is_success and t is not None and t.status in _HOLDS_MONEY:
            return ChargeResult(ChargeOutcome.APPROVED, provider_txn_id=t.id)
        if t is not None:
            if t.status in _DECLINED:
                raw = _decline_code(t)
                reason = map_raw_code(self.name, BRAINTREE_CODES, raw)
                return ChargeResult.declined(
                    reason, raw_code=raw, raw_message=result.message, provider_txn_id=t.id
                )
            # A transaction exists, but in a state we did not expect from a sale.
            return ChargeResult(ChargeOutcome.UNKNOWN, raw_code=str(t.status), provider_txn_id=t.id)
        # No transaction: validation errors, nothing was processed.
        codes = [str(e.code) for e in result.errors.deep_errors]
        raw = codes[0] if codes else None
        if _INVALID_PAYMENT_METHOD_CODES.intersection(codes):
            return ChargeResult.declined(R.INVALID_CARD, raw_code=raw, raw_message=result.message)
        return ChargeResult(
            ChargeOutcome.FAILED_NOT_CHARGED, raw_code=raw, raw_message=result.message
        )

    # ------------------------------------------------------------------ lookup / cancel

    def _find(self, reference: str) -> list[Any]:
        res = self.gateway.transaction.search(braintree.TransactionSearch.order_id == reference)
        return [t for t in res.items if t.type == "sale"]

    async def lookup(self, reference: str) -> LookupResult:
        try:
            txns = await self._call(self._find, reference)
        except Exception:
            log.warning("braintree lookup failed", extra={"provider": self.name}, exc_info=True)
            return LookupResult(LookupStatus.UNKNOWN)
        return self._lookup_result(txns)

    def _lookup_result(self, txns: list[Any]) -> LookupResult:
        if not txns:
            return LookupResult(LookupStatus.NOT_FOUND)
        # One sale per reference is expected; if there were more, any money wins.
        for t in txns:
            if t.status in _HOLDS_MONEY and not t.refund_ids:
                return LookupResult(LookupStatus.APPROVED, provider_txn_id=t.id)
        for t in txns:
            if t.status in _HOLDS_MONEY or t.status in _RELEASED:  # refunded or voided
                return LookupResult(LookupStatus.VOIDED, provider_txn_id=t.id)
        t = txns[0]
        if t.status in _DECLINED:
            raw = _decline_code(t)
            return LookupResult(
                LookupStatus.DECLINED,
                reason=map_raw_code(self.name, BRAINTREE_CODES, raw),
                provider_txn_id=t.id,
                raw_code=raw,
            )
        return LookupResult(LookupStatus.UNKNOWN)

    async def cancel(self, reference: str) -> CancelResult:
        """Make the reference hold no money: void before settlement, refund after."""
        try:
            return await self._call(self._cancel_sync, reference)
        except Exception:
            log.warning("braintree cancel failed", extra={"provider": self.name}, exc_info=True)
            return CancelResult(CancelStatus.UNKNOWN)

    def _cancel_sync(self, reference: str) -> CancelResult:
        txns = self._find(reference)
        if not txns:
            # No tombstone exists at Braintree: a request still in flight could land later.
            return CancelResult(CancelStatus.UNKNOWN)
        status = CancelStatus.CANCELLED
        for t in txns:
            if t.status in _RELEASED or t.status in _DECLINED or t.refund_ids:
                continue
            if t.status in _VOIDABLE and self.gateway.transaction.void(t.id).is_success:
                continue
            fresh = self.gateway.transaction.find(t.id)  # may have settled meanwhile
            if fresh.status in _REFUNDABLE and not fresh.refund_ids:
                if self.gateway.transaction.refund(t.id).is_success:
                    continue
            if fresh.status in _RELEASED or fresh.refund_ids:
                continue
            status = CancelStatus.NOT_CANCELLABLE
        return CancelResult(status)


def _decline_code(t: Any) -> str | None:
    if t.status == "gateway_rejected":
        return f"gateway_rejected:{t.gateway_rejection_reason}"
    return str(t.processor_response_code) if t.processor_response_code else None
