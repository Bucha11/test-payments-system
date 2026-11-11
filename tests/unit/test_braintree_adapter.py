"""Braintree adapter: classification of SDK results and errors, with a fake gateway.

Shapes mirror what the sandbox actually returns (checked against it): a declined sale is
`is_success=False` with a transaction in `processor_declined` / `failed` /
`gateway_rejected`; a validation error has no transaction, only `errors.deep_errors`.
"""

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from braintree.exceptions import AuthenticationError, ServerError, TooManyRequestsError
from braintree.exceptions.http.timeout_error import ConnectTimeoutError, ReadTimeoutError

from cascade.domain.enums import CancelStatus, ChargeOutcome, LookupStatus
from cascade.domain.providers import ChargeRequest
from cascade.domain.taxonomy import NormalizedReason
from cascade.infrastructure.providers.braintree import BraintreeAdapter, to_decimal_amount

REQ = ChargeRequest("ref-1", 1999, "USD", "tok_demo_visa")


def txn(status: str, **kw: Any) -> SimpleNamespace:
    base = {
        "id": "bt1",
        "type": "sale",
        "status": status,
        "processor_response_code": "1000",
        "gateway_rejection_reason": None,
        "refund_ids": [],
    }
    return SimpleNamespace(**{**base, **kw})


def ok(t: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(is_success=True, transaction=t, message=None)


def failed(t: SimpleNamespace | None = None, codes: tuple[str, ...] = ()) -> SimpleNamespace:
    errors = SimpleNamespace(deep_errors=[SimpleNamespace(code=c) for c in codes])
    return SimpleNamespace(is_success=False, transaction=t, message="msg", errors=errors)


@dataclass
class FakeTransactions:
    sale_result: Any = None
    sale_error: Exception | None = None
    sale_delay_s: float = 0
    found: list[SimpleNamespace] = field(default_factory=list)
    search_error: Exception | None = None
    void_ok: bool = True
    refund_ok: bool = True
    sales: list[dict[str, Any]] = field(default_factory=list)
    voided: list[str] = field(default_factory=list)
    refunded: list[str] = field(default_factory=list)

    def sale(self, params: dict[str, Any]) -> Any:
        self.sales.append(params)
        time.sleep(self.sale_delay_s)
        if self.sale_error:
            raise self.sale_error
        return self.sale_result

    def search(self, _query: Any) -> SimpleNamespace:
        if self.search_error:
            raise self.search_error
        return SimpleNamespace(items=list(self.found))

    def find(self, txn_id: str) -> SimpleNamespace:
        return next(t for t in self.found if t.id == txn_id)

    def void(self, txn_id: str) -> SimpleNamespace:
        if self.void_ok:
            self.voided.append(txn_id)
            self.find(txn_id).status = "voided"
        return SimpleNamespace(is_success=self.void_ok)

    def refund(self, txn_id: str) -> SimpleNamespace:
        if self.refund_ok:
            self.refunded.append(txn_id)
            self.find(txn_id).refund_ids = ["r1"]
        return SimpleNamespace(is_success=self.refund_ok)


def adapter(tx: FakeTransactions, **kw: Any) -> BraintreeAdapter:
    gw: Any = SimpleNamespace(transaction=tx)
    return BraintreeAdapter("braintree", gw, 0.5, sandbox=True, currencies=["USD"], **kw)


# ------------------------------------------------------------------------- charge


@pytest.mark.parametrize(
    ("result", "outcome", "reason"),
    [
        (ok(txn("submitted_for_settlement")), ChargeOutcome.APPROVED, None),
        (
            failed(txn("processor_declined", processor_response_code="2000")),
            ChargeOutcome.SOFT_DECLINE,
            NormalizedReason.DO_NOT_HONOR,
        ),
        (
            failed(txn("processor_declined", processor_response_code="2001")),
            ChargeOutcome.HARD_DECLINE,
            NormalizedReason.INSUFFICIENT_FUNDS,
        ),
        (  # sandbox amount 3000.00: status "failed", nothing processed
            failed(txn("failed", processor_response_code="3000")),
            ChargeOutcome.SOFT_DECLINE,
            NormalizedReason.PROVIDER_UNAVAILABLE,
        ),
        (
            failed(txn("gateway_rejected", gateway_rejection_reason="fraud")),
            ChargeOutcome.SOFT_DECLINE,
            NormalizedReason.PROVIDER_RISK_REJECT,
        ),
        (  # unknown processor code: fail safe
            failed(txn("processor_declined", processor_response_code="2999")),
            ChargeOutcome.HARD_DECLINE,
            NormalizedReason.UNMAPPED,
        ),
        (  # unusable nonce / token
            failed(codes=("91565",)),
            ChargeOutcome.HARD_DECLINE,
            NormalizedReason.INVALID_CARD,
        ),
        (failed(codes=("81503",)), ChargeOutcome.FAILED_NOT_CHARGED, None),
        (ok(txn("voided")), ChargeOutcome.UNKNOWN, None),  # unexpected state for a sale
    ],
)
async def test_charge_result_classification(
    result: Any, outcome: ChargeOutcome, reason: NormalizedReason | None
) -> None:
    r = await adapter(FakeTransactions(sale_result=result)).charge(REQ)
    assert r.outcome is outcome
    assert r.reason is reason


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ConnectTimeoutError(), ChargeOutcome.FAILED_NOT_CHARGED),  # never reached Braintree
        (AuthenticationError(), ChargeOutcome.FAILED_NOT_CHARGED),
        (TooManyRequestsError(), ChargeOutcome.FAILED_NOT_CHARGED),
        (ReadTimeoutError(), ChargeOutcome.UNKNOWN),  # sent, answer lost
        (ServerError(), ChargeOutcome.UNKNOWN),
        (ConnectionResetError(), ChargeOutcome.UNKNOWN),
    ],
)
async def test_charge_errors(error: Exception, outcome: ChargeOutcome) -> None:
    r = await adapter(FakeTransactions(sale_error=error)).charge(REQ)
    assert r.outcome is outcome


async def test_charge_outer_timeout_is_unknown() -> None:
    tx = FakeTransactions(sale_result=ok(txn("submitted_for_settlement")), sale_delay_s=1.0)
    r = await adapter(tx).charge(REQ)
    assert r.outcome is ChargeOutcome.UNKNOWN


async def test_charge_sends_reference_amount_and_sandbox_nonce() -> None:
    tx = FakeTransactions(sale_result=ok(txn("submitted_for_settlement")))
    await adapter(tx, merchant_account_id="acc").charge(REQ)
    sent = tx.sales[0]
    assert sent["order_id"] == "ref-1"
    assert sent["amount"] == "19.99"
    assert sent["payment_method_nonce"] == "fake-valid-nonce"  # mock token, sandbox
    assert sent["merchant_account_id"] == "acc"
    assert sent["options"] == {"submit_for_settlement": True}


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (
            "fake-processor-declined-visa-nonce",
            {"payment_method_nonce": "fake-processor-declined-visa-nonce"},
        ),
        ("bt_abc", {"payment_method_token": "abc"}),
    ],
)
async def test_payment_method_mapping(token: str, expected: dict[str, str]) -> None:
    tx = FakeTransactions(sale_result=ok(txn("submitted_for_settlement")))
    await adapter(tx).charge(ChargeRequest("r", 100, "USD", token))
    assert expected.items() <= tx.sales[0].items()


async def test_unsupported_currency_declines_without_a_call() -> None:
    tx = FakeTransactions()
    r = await adapter(tx).charge(ChargeRequest("r", 100, "EUR", "t"))
    assert r.outcome is ChargeOutcome.SOFT_DECLINE
    assert r.reason is NormalizedReason.UNSUPPORTED_CARD_OR_CURRENCY
    assert tx.sales == []


def test_decimal_amount() -> None:
    assert to_decimal_amount(1999, "USD") == "19.99"
    assert to_decimal_amount(5, "USD") == "0.05"
    assert to_decimal_amount(1500, "JPY") == "1500"
    assert to_decimal_amount(1500, "KWD") == "1.500"


# ------------------------------------------------------------------------- lookup


@pytest.mark.parametrize(
    ("found", "status"),
    [
        ([], LookupStatus.NOT_FOUND),
        ([txn("submitted_for_settlement")], LookupStatus.APPROVED),
        ([txn("settled")], LookupStatus.APPROVED),
        ([txn("settled", refund_ids=["r1"])], LookupStatus.VOIDED),  # our cancel refunded it
        ([txn("voided")], LookupStatus.VOIDED),
        ([txn("processor_declined", processor_response_code="2000")], LookupStatus.DECLINED),
        ([txn("weird_new_status")], LookupStatus.UNKNOWN),
        ([txn("voided"), txn("settled", id="bt2")], LookupStatus.APPROVED),  # money wins
        ([txn("settled", type="credit")], LookupStatus.NOT_FOUND),  # a refund is not the sale
    ],
)
async def test_lookup(found: list[SimpleNamespace], status: LookupStatus) -> None:
    r = await adapter(FakeTransactions(found=found)).lookup("ref-1")
    assert r.status is status


async def test_lookup_error_is_unknown() -> None:
    r = await adapter(FakeTransactions(search_error=ServerError())).lookup("ref-1")
    assert r.status is LookupStatus.UNKNOWN


# ------------------------------------------------------------------------- cancel


async def test_cancel_voids_before_settlement() -> None:
    tx = FakeTransactions(found=[txn("submitted_for_settlement")])
    assert (await adapter(tx).cancel("ref-1")).status is CancelStatus.CANCELLED
    assert tx.voided == ["bt1"]
    assert tx.refunded == []


async def test_cancel_refunds_after_settlement() -> None:
    tx = FakeTransactions(found=[txn("settled")])
    assert (await adapter(tx).cancel("ref-1")).status is CancelStatus.CANCELLED
    assert tx.refunded == ["bt1"]


async def test_cancel_is_idempotent() -> None:
    tx = FakeTransactions(found=[txn("settled", refund_ids=["r1"]), txn("voided", id="bt2")])
    assert (await adapter(tx).cancel("ref-1")).status is CancelStatus.CANCELLED
    assert tx.voided == tx.refunded == []


async def test_cancel_of_a_declined_sale_holds_no_money() -> None:
    tx = FakeTransactions(found=[txn("processor_declined")])
    assert (await adapter(tx).cancel("ref-1")).status is CancelStatus.CANCELLED


async def test_cancel_not_found_is_unknown() -> None:
    # No tombstone at Braintree: a request in flight could still create the sale.
    assert (await adapter(FakeTransactions()).cancel("ref-1")).status is CancelStatus.UNKNOWN


async def test_cancel_that_fails_is_not_cancellable() -> None:
    tx = FakeTransactions(found=[txn("settled")], refund_ok=False)
    assert (await adapter(tx).cancel("ref-1")).status is CancelStatus.NOT_CANCELLABLE


def test_cancel_does_not_block_late_charges() -> None:
    assert adapter(FakeTransactions()).cancel_blocks_late_charge is False
