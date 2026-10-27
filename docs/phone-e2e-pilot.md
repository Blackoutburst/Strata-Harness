# First live harness pilot: Phone E2E

The human requested on 2026-10-23: “Once done make it redo the phone E2E test,
this is a good crash test to debug the harness.” This queues the Phone task as
the first live end-to-end pilot after the harness's readiness checks. It does not
reopen the original Swift benchmark or authorize unrelated Board work.

## Readiness before dispatch

- Complete and test the Board and coding-engine adapters: own paired worker
  identity, selected-task scope, current-version claim/renew/release, durable inbox,
  safe-boundary manager pings, urgent stop and actionable blocker escalation.
- Pass fake-service restart, replay, uncertain-outcome and model-outage tests.
  Prove the actual installed engine's relevant API behavior and RTK activity.
- Retain the real command exit status and full logs, require an early compile
  checkpoint and stop repeated unchanged failures before the step budget runs out.
- Reconcile previous process/session/worktree effects and verify one coding worker
  across hosts. A local flock alone is insufficient. Keep the manager and worker
  identities distinct; pairing credentials stay outside this source tree.
- Verify the bounded emulator-validation path. If orchestration runs on Ubuntu
  while the permitted emulator remains on the Mac, use an explicitly scoped
  validation adapter on its owning host. Do not grant unrestricted remote shell or
  substitute another emulator/device to make the pilot run.

## Fresh task and source boundary

Create one separately identified manager-selected Phone E2E pilot ticket/session
and a fresh isolated worktree from the original baseline
`19d7458debbe540c9cb7f0061805f7af766cdf5a`. Use the original task specification and
acceptance requirements. Do not reuse the old partial Swift implementation or
another model's implementation. The old ticket, worktree, logs, comparisons and
historical scores remain intact.

Swift implements the task through the new harness, using the served Strata model
and RTK. The manager develops the harness, reviews the resulting app diff and
independently checks acceptance. The manager must not implement the app tests on
Swift's behalf. Keep minimal production changes and meaningful accessibility and
journey assertions. Do not weaken assertions, count skips as passes or hide
crashed/unrun cases.

## Bounded run, inspect and fix

Start with one controlled pilot execution, capped at 90 minutes of active worker
time and a recorded step budget. Capture model/session/request settings, tool and
RTK events, source/checkpoints and real command receipts. A step-limit or repeated
failure is a pilot result to diagnose; it must not trigger an unbounded automatic
retry chain or revive original benchmark round 2/3.

The worker may use the separately implemented, serial-restricted validation path
to build, run the permitted emulator tests, inspect failure logs and fix its own
implementation before handoff. Use JDK17 and the owning host's SDK. Rebuild
`assembleDebug`, `assembleDebugAndroidTest` and `testDebugUnitTest`; inspect supplied
scripts before running them. Keep raw exit codes independent of RTK/output tails.

Use only the already authorized silent isolated caregiver35 API35 emulator,
`emulator-5556`, ports 5556/5557. Verify its actual owned process, unlocked state,
CE storage after install and resolvable target activity. Every ADB command must
specify that serial. Connected runs remain capped at 25 minutes, each test at
300 seconds. Preserve the original AVD, exclude physical devices, real calls,
real accounts and unrelated data. Restore exact settings, roles and permission
flags; clean only proven owned records/components/allocations. Do not run a UI
dump during live instrumentation.

Use build/XML/log evidence first. Screenshot diagnosis is available only after a
known image has been verified through the engine request and the actual served
model. An advertised vision capability alone does not establish this path.

## Harness stress observations

| Probe | Evidence required |
|---|---|
| Task intake and a long build | One selected claim; timely own-identity renewals/inbox checks without model reminders |
| Manager ping during work | Durable receipt, safe-boundary delivery, acknowledgement and observable subsequent effect with latency |
| Persistent build/test failure | Correct failing status, full evidence, bounded attempts and one useful manager question |
| Controlled supervisor restart | Checkpoint and guidance retained; current worker/claim/effects reconciled without duplicate execution |
| Inference unavailable | Board control remains available, backoff and bounded claim grace work, no automatic model restart |
| Final review | Exact commit/clean source state, truthful passed/failed/skipped/crashed/unrun counts, claim released and worker stopped |

Run destructive interruption scenarios against fake services first. During the
live pilot, interrupt only owned harness components at a checkpoint with known
in-flight effects. Do not stop or reconfigure the personal-PC Strata server to
simulate an outage; use the adapter's controlled availability test. Avoid an
interruption that abandons test-created records or restarts an uncertain command.

## Outcome and next decision

Report the Phone implementation and harness reliability separately. Record total
developer and validation time, accepted meaningful journeys, manager review effort,
fresh/output/cache tokens, tool failures, renew/inbox deadlines, ping latency and
recovery behavior. Energy remains unmeasured unless real telemetry is available.

Independently review the full diff and required checks. Privileged deployment,
emulator runtime, CI and physical ROM/SIM acceptance remain separate claims; verify
the deploy script and active system APK where supported or report the gap. Failed
code stays isolated. No production merge/push follows merely from a pilot run.

Use the result to fix the harness in bounded increments and deliberately select
any follow-up pilot. Keep GLM held through this first authorized Phone harness
pilot; its three recovery tasks remain queued and preserved. No broader backlog
dispatch is authorized.
