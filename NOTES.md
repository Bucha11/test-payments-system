# NOTES

Decisions taken where the spec was silent or self-contradictory, things deliberately left
out, and environment caveats.

## Decisions (taken after M1 review: "go on")

1. **`CREATED -> FAILED` (`no_route`).** 5.1's diagram has no such edge, but 8.1 step 2
   fails a payment before it is claimed. The edge exists.
2. **Late success on a settled attempt (S9).** 5.2 only had `SUCCEEDED -> REFUNDED_DUPLICATE`,
   but the engine only moves past an attempt once it is DECLINED_*, FAILED_NOT_CHARGED or
   VOIDED, so a late "charged" for it could not be recorded and the money would stay on the
   card. Added `{UNKNOWN, DECLINED_SOFT, DECLINED_HARD, FAILED_NOT_CHARGED, VOIDED} ->
   REFUNDED_DUPLICATE` (never via SUCCEEDED, so the one-success index holds).
3. **Fencing token `payments.lease_owner`** (migration 0002). The spec's claim query alone
   lets a worker whose lease expired keep writing after the sweep took over. Every engine
   write checks and renews the token in the same transaction.
4. **`LookupStatus.VOIDED`** in addition to section 7's four values (see DESIGN.md).
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

## Residual risks (by design, flagged not hidden)

* A provider that reports "declined" and later charges anyway, while the cascade already
  succeeded elsewhere: the webhook path cancels the late charge; if that cancel fails the
  payment is flagged `needs_review`. Without a webhook nothing notices it; a periodic
  provider-side settlement report reconciliation would (out of scope).
* `unknown_max_age_s` exceeded with lookup still failing: `FAILED` + `needs_review`; if the
  provider did charge, it is a manual case by definition.

## Milestone status

* M1–M5: done; scenarios S1–S12 and the property test are green.
* M6.1 `/stats`, `/providers`: done, S13 green; the demo prints both.
* M6.2 weighted routing: done, S14 green (unit test).
* M6.3 GitHub Actions: ruff, mypy (domain + application, strict), `make test`.
* M7 Braintree: **not done** — no sandbox credentials in `.env`, and section 15 details are
  explicitly unverified. `braintree` stays in `routing.yaml` with `enabled: false`; no adapter
  is built for it, so it never enters a route.

## Environment caveats (the sandbox this was built in)

* Docker Hub returned 429 and container builds had no direct network. The stack was run
  with the same images from `mirror.gcr.io` and the app image built with the sandbox
  proxy CA injected through a throw-away Dockerfile (not committed). `docs/demo-output.txt`
  is from that real run. On a normal machine `make up` needs nothing extra.
* redis-py >= 8 defaults `socket_timeout` to 5 s; taskiq-redis consumes with an unbounded
  BRPOP, so the worker died every 5 s. The broker sets `socket_timeout=None` explicitly.
