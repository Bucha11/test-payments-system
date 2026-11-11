# NOTES

Things noticed while building that are outside the current milestone, open questions for
review, and environment caveats. Nothing here is built unless a milestone asks for it.

## Open questions for review (M1)

1. **`CREATED -> FAILED`.** Section 5.1 diagram has no such edge, but 8.1 step 2 fails a
   payment with `no_route` before it is claimed. Added the edge; alternative is to claim
   first and fail from PROCESSING. Chosen: the edge (no lease taken for a payment that
   never calls a provider).

2. **Late success on a non-SUCCEEDED attempt (S9).** 5.2 only allows
   `SUCCEEDED -> REFUNDED_DUPLICATE`. But the engine can only move past provider A when A's
   attempt is DECLINED_*, FAILED_NOT_CHARGED or VOIDED, all of which are final. So a late
   "succeeded" webhook for A would be REJECTED by the transition function, and the money
   would be left on the card. For S9 (and real life: a provider that mis-reports a
   decline, or a cancel that did not really block the charge) the late-success handler
   needs a way to record "provider says this reference is charged". Proposal for M4:
   keep the attempt graph as is, and let the late-success handler act on the *provider
   claim* rather than on the attempt status: call `cancel()`; on success mark the attempt
   `REFUNDED_DUPLICATE` via an explicit extra edge
   `{DECLINED_SOFT, FAILED_NOT_CHARGED, VOIDED} -> REFUNDED_DUPLICATE` (never through
   SUCCEEDED, so the partial unique index is never violated); on failure set
   `needs_review`. Needs a decision before M4.

3. **Transition outcome for stale events.** The spec says duplicates and out-of-order
   events are no-ops. The transition function distinguishes `DUPLICATE` (same state) from
   `REJECTED` (no such edge); webhook/polling treat both as no-ops, the engine treats both
   as a bug. See DESIGN.md.

## Environment caveats

* In the sandbox this was built in, Docker Hub returned 429 and container builds had no
  outbound network. The compose stack was verified with the same images pulled from
  `mirror.gcr.io` and the app image built with the sandbox proxy CA injected through a
  throw-away Dockerfile (not committed). On a normal machine `make up` needs nothing extra.
* redis-py ≥ 8 defaults `socket_timeout` to 5 s; taskiq-redis consumes with an unbounded
  BRPOP, so the worker crashed every 5 s. The broker sets `socket_timeout=None` explicitly
  (connect timeout stays 5 s).

## Braintree (M7)

Not started. Section 15 details are unverified by design; adapter will stay behind
`enabled: false` until checked against official docs.
