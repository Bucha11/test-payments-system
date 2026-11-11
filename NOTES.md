# NOTES

Design decisions that are not obvious from the code, residual risks, and environment
caveats.

## Decisions

1. **`CREATED -> FAILED` (`no_route`).** A payment whose route is empty (every provider
   disabled or its breaker open) fails before it is ever claimed, so the edge exists.
2. **Late success on a settled attempt (S9).** `SUCCEEDED -> REFUNDED_DUPLICATE` alone is
   not enough: the engine only moves past an attempt once it is DECLINED_*,
   FAILED_NOT_CHARGED or VOIDED, so a late "charged" for such an attempt could not be
   recorded and the money would stay on the card. Hence `{UNKNOWN, DECLINED_SOFT,
   DECLINED_HARD, FAILED_NOT_CHARGED, VOIDED} -> REFUNDED_DUPLICATE` (never via SUCCEEDED,
   so the one-success index holds).
3. **Fencing token `payments.lease_owner`** (migration 0002). A lease-expiry claim alone
   lets a worker whose lease expired keep writing after the sweep took over. Every engine
   write checks and renews the token in the same transaction.
4. **`LookupStatus.VOIDED`** in addition to APPROVED / DECLINED / NOT_FOUND / UNKNOWN
   (see DESIGN.md).
5. **Deadline uses the engine's clock** (`deadline_at` computed in the application, not by
   `now()` in SQL), so deadline checks and deadline creation use one time source.
6. **Reconciliation backoff and next check.** No delayed-task infrastructure: a parked
   payment stores its next-check time in `lease_until`; the worker's sweep (every
   `sweep_interval_s`) picks up due ones. The first check is enqueued immediately.
7. **Sweep runs inside the worker** (a loop started on worker startup) rather than a
   separate `taskiq scheduler` container: cron granularity is one minute, too slow for
   S7, and concurrent sweepers are harmless because claims are atomic. A `sweep` task is
   registered too, for manual kicks.
8. **Half-open probe slot is taken at route-build time.** If the probed provider is not
   first in the route and the payment succeeds earlier, the probe is unused until its key
   expires (= cooldown). Acceptable; noted.
9. **Mock decline codes** are descriptive (`do_not_honor`, `insufficient_funds`, ...) plus a
   few ISO-8583 numerics; the adapter maps them, unknown codes become `unmapped` (hard).
   `soft_decline` / `hard_decline` without a code default to `do_not_honor` /
   `insufficient_funds`.
10. **Mock admin extras:** `GET /_admin/webhooks` (outbox, used to deliver webhooks in tests)
    and `GET /_admin/calls` (charge calls, used to assert "B never called").
11. **Idempotent replay status code:** a replay returns the current state: 201 if terminal,
    202 if still in flight.
12. **Property test** configures adapters truthfully (`cancel_blocks_late_charge` equals the
    mock flag). A provider that lies about cancel is only recoverable through a webhook;
    that case is S9.

13. **A success webhook is a hint, verified by lookup.** Found by the property test: A
    charged, the response was lost, lookup was down, our cancel voided the charge (A does
    not guarantee no later charge, so the payment was parked) — then A's
    `charge.succeeded`, emitted *before* the void, arrived and was trusted: SUCCEEDED with
    zero charges. Now the webhook handler asks `lookup()` for the current state and applies
    that; if lookup is unavailable it defers to reconciliation. For late success it ignores
    the event when lookup says nothing is charged, and cancels when lookup is APPROVED or
    unavailable (cancel is idempotent, so cancelling "to be safe" cannot hurt). Decline
    webhooks are applied directly (a decline cannot be undone by our cancel). Regression test:
    `test_stale_success_webhook_after_our_void_is_not_trusted`.

14. **Braintree sandbox as the real provider.** The route `USD + country US` is
    `[braintree, mock_a]`. Braintree has no idempotency key, so an attempt is sent once and
    an unknown outcome is resolved by searching `order_id` (= attempt id). `cancel` voids
    before settlement and refunds after; it cannot stop a request still in flight, so
    `cancel_blocks_late_charge` is false and an unresolvable unknown outcome parks the
    payment. Braintree sends no webhooks for card sales: lookup is the only reconciliation
    path. The sandbox merchant account settles USD only (`currencies: [USD]`); another
    currency is a soft decline without a network call. Any non-`fake-`/`bt_` token is
    charged as a sandbox test card, so one payment can cascade between mocks and Braintree.
    Our taxonomy decides soft/hard, not Braintree's `processor_response_type` (it calls
    2001 insufficient funds "soft" meaning "retry later here").
15. **Webhook dedupe on processed events** (migration 0003). Receiving and applying were
    two steps; a crash between them left a row that turned every redelivery into a
    "duplicate". Now only rows with `processed_at` are duplicates; applying is idempotent.
16. **An APPROVED response that cannot be recorded is undone.** If the attempt was moved
    meanwhile (a decline webhook raced the response), the engine cancels the charge before
    the loop may try the next provider; if the cancel fails, the payment fails with
    `unrecorded_charge` + `needs_review` and no further provider is called.
17. **Lease vs. provider timeout is validated.** One attempt can run
    `timeout_s x (lookup_retries + 2)` without a write (charge, lookups, cancel); the config
    is rejected unless that is below `lease_s`, otherwise a sweeper would take the payment
    over mid-step.
18. **Mock webhook event ids are unique across restarts.** They used to restart at
    `evt_1`, and the API dedupes on event id forever, so after a mock restart new events
    were dropped as duplicates (seen as S9b failing on a second `make demo`).

## Residual risks (by design, flagged not hidden)

* A provider that reports "declined" and later charges anyway, while the cascade already
  succeeded elsewhere: the webhook path cancels the late charge; if that cancel fails the
  payment is flagged `needs_review`. Without a webhook (Braintree sends none for sales)
  nothing notices it; a periodic settlement-report reconciliation would (out of scope).
* `unknown_max_age_s` exceeded with lookup still failing: `FAILED` + `needs_review`; if the
  provider did charge, it is a manual case by definition.

## Environment caveats

* redis-py >= 8 defaults `socket_timeout` to 5 s; taskiq-redis consumes with an unbounded
  BRPOP, so the worker died every 5 s. The broker sets `socket_timeout=None` explicitly.
* Host port 5432 must be free for `make up` / `make test` (a local PostgreSQL will clash).
