# Deterministic coordination component

`Coordinator` combines the local journal and selected-task Board adapter with
independent inbox, lease and canonical-text timers. It never calls inference,
discovers tasks, claims work, starts a process or restarts Strata. It is still a
separately tested component, not configured into the supervisor's CLI or service.

## Timers and bounded waiting

Default inbox interval is 30 seconds and renewal interval is 300 seconds, with
renewal also bounded by a 60-second lease safety margin. Board coordination
deadlines cannot exceed ten minutes. Empty polls and renewal need no model turn.
Inbox retry backoff is bounded at 300 seconds and runs independently of backend
availability. A nonempty page is persisted immediately and schedules a near-term
continuation. This drains addressed pages without treating polling as a claim.

The caller supplies the supervisor's actual backend state: available, expected
model outage or configuration/authentication failure. Configuration failures stay
distinct in saved control state. An idle model outage is normal; no task is
claimed or sticky pause created. For active/claimed work, a short model outage
can retain responsibility only within the configured grace period (300 seconds
by default). The original outage time survives supervisor restart.

Renewal requires the lease layer's actual execution verifier, a current owned
claim, fresh Board control and sufficiently recent progress. After an offline
grace deadline, stale progress or ten minutes without working inbox control, the
coordinator persists a quiesce intention before calling the engine adapter. It
stops renewing; a return of model availability alone cannot silently resume work.
A backwards clock or a progress timestamp implausibly in the future requires
reconciliation rather than extending the waiting window.

A lost pause reply leaves a durable request ID and an uncertain outcome. It is
not blindly submitted again after restart. The future engine adapter must query
and reconcile that exact request and expose acknowledgement/effect evidence.

Claim release requires explicit quiescence, verified cleanup and a durable coding
checkpoint, then the lease layer's fresh ownership/version check. An absent
process or expired lease alone cannot release responsibility or prove cleanup.
Release retains assignment and returns to `todo`; it is not a completed task or
review acceptance. An inactive worker without cleanup evidence is not renewed.

## Canonical message retrieval

Only explicitly configured selected-task threads are fetched. Other addressed
envelopes remain durable without being expanded into model context. The future
configuration/cutover path must bind those threads to the selected task and verify
trusted manager authorship before delivering guidance. Thread membership and body
integrity alone do not authorize instructions.

The Board's compact `text_fields.body` reference pins message ID, `/body`, SHA256,
character count and UTF-8 length. Each `get_text` request includes that hash and
uses Unicode code-point offsets with a 2048 encoded-byte budget. Saved prefixes
survive restart. Every chunk must preserve metadata and advance the exact offset;
the final body must match the complete counts and SHA256 before becoming `ready`.
Preview text is never substituted. Text bodies are capped at 1 MiB locally.

Body retrieval has a small per-tick request budget. Pending messages share it in
round-robin order; a long body cannot monopolize every tick. A malformed source
has per-message persisted backoff, so it cannot prevent a later valid ping from
being fetched. Renewal/control decisions happen before body reads. Retrieval does
not submit, acknowledge or apply the guidance to an engine.

## Worker observations are a required trust boundary

`WorkerState` and the lease's execution verifier must come from an adapter that
checks the actual owned process/session, in-flight operations and cleanup. Its
progress timestamp must describe useful work or a verified checkpoint, not merely
the daemon heartbeat. Token streaming alone does not establish useful progress;
repeated command/read/failure fingerprints still need the planned engine/tool
loop detector. Active execution without a selected lease requests quiescence.

The quiesce callback is currently an interface exercised with fixtures. No real
safe-boundary delivery, abort, live pause acknowledgement, process liveness,
cleanup verification or automatic resumption is established by these tests.
Trusted engine recovery and manager authorization must clear a stopped control
lifecycle deliberately; this component does not auto-resume after uncertainty.

## Verification

The tests exercise a **virtual** 30-minute active run with deadline assertions;
they do not claim 30 minutes of live Board/model operation. Other cases cover
offline grace across restart, continued polling during model outage, no renewal
of dead work, cleanup-gated release, stale Board/progress, uncertain pause replies,
clock reversal, active work without a lease, UTF-8/code-point chunk recovery,
hash mismatch, fair message retrieval and distinct backend configuration errors.
The HTTP Board tests also verify scoped `get_text` requests and source hashes.

All 44 tests pass on macOS and Ubuntu Python 3.10.12. The first Ubuntu run exposed
a separate startup race: a status reader could observe SQLite's new empty file
before bootstrap tables existed. Status now reports that empty state as
`initializing` while still rejecting an unexpected nonempty schema. A targeted
regression covers both cases. The first failure and final passing receipts are
retained under `verification/2026-10-23-coordinator*`.

No real worker has been paired, no coding process started, no service installed,
no physical device used and no original benchmark reopened. The next increment is
the coding-engine event adapter with actual request/steering/stop verification,
followed by daemon wiring and the remaining Phone pilot readiness checks.
