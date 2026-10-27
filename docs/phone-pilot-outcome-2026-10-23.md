# Fresh Phone harness pilot — stopped, incomplete

The one authorized pilot stopped at **2026-10-23T12:39:42.723Z**, after **104 assistant
steps** and **2713.814 charged seconds (45m13.8s)**. It reached the shared escalation
allowance, not the 400-step or 5400-second cap. The fourth escalation was a new
build-control hash review. Three review requests and one earlier infrastructure
failure had been counted together. No second pilot, automatic retry or developer
continuation was launched after this terminal result.

This is a harness stress-test result, independent of the original closed Swift
benchmark. Its historical scores, source, tables and partial round2 remain untouched.
GLM and the original Qwen/Mistral workers remain held.

## Application outcome

**No accepted journey and no pilot emulator runtime result.** The new source did
not produce a successfully compiled instrumentation APK. The last executed offline
build (job1170f45ac57e13d8c02037b409cf9819) failed with **eight Kotlin diagnostics**:
six unresolved `checkAccessibility` calls and two `performClick` receiver mismatches.
The last edit added explicit Compose test dependencies; its new Gradle hash was
fenced and never executed. The manager did not rescue or modify the app code.

App assembly completed for the preceding source during the isolated dependency
prefetch. The initial unmodified baseline passed all three required build tasks.
Neither establishes final-source acceptance. The four baseline JVM tests had passed
in readiness; the failed new-source combined run does not establish a fresh final
JVM result. No skipped, crashed, zero-discovered or unrun test is counted as passing.

Full exact source review found two partial Dial test methods, a Hilt runner and a
runtime denied-permissions annotation. The denied scenario has no substantive
marked test yet, and the remaining original Phone journeys are not implemented.
The runner replaces PhoneApp initialization, so production startup/provider
observers are not covered. The unchanged generated ExampleInstrumentedTest still
asserts the wrong package name. No successful accessibility or complete journey
result is evidenced. Teardown was improved after manager feedback but remains
unvalidated because the test source did not compile. Production app source was
not changed. There is no implementation commit: HEAD remains baseline
`19d7458debbe540c9cb7f0061805f7af766cdf5a`, with preserved uncommitted work.

Privileged install/deploy, current system-APK hash/flags/launch, CI, physical
ROM/device and SIM acceptance were not run for this failed implementation.
No merge or push occurred. Ticket7667684d1e4fc8c0531c6dc0a51a8c0f is in **review,
explicitly incomplete/not accepted**, with the existing worker assignment retained.

## Harness observations

- The daemon remains running on Ubuntu with its distinct paired worker identity.
  Exact coding PID348857 and its process group are gone; no coding process remains
  on either host. The Board claim was released at12:39:43.195Z. The Mac bridge then
  stopped after exact reservation/process/effect reconciliation; only its proven
  owned lock was removed. The silent emulator process was left untouched.
- The same engine session, selected checkout and consumed counters survived the
  explicit earlier infrastructure reconciliations. Four claim/release segments
  are recorded; these were not fresh implementation rounds. Earlier failures and
  intentional abort receipts remain intact.
- The live checkpoint ping was submitted12:19:32Z, acknowledged12:19:37Z and
  observably applied12:20:54Z. Ordinary later guidance has delivery receipts; those
  receipts alone do not establish semantic application.
- During the final healthy segment, renewal intervals were about300–303s without
  model reminders. Earlier claim renew-to-release gap was610.37s, slightly beyond
  the ten-minute target. Recorded maximum inbox polling gap was496.52s across
  infrastructure interruptions, despite a nominal30s interval. Full cadence is
  therefore a remaining audit item, not an unconditional pass.
- Actual installed-engine readiness probes proved RTK rewriting and command failure
  handling. The pilot mostly used native source tools and fixed harness commands;
  no successful RTK-prefixed task command was observed in its stored task history.
  Do not represent the preflight probe as comprehensive task-level RTK usage.
- True build exits and complete diagnostics survived both worker and manager
  prefetch paths. Build-control changes stayed fenced. Emulator rollback and image
  transport were independently verified in readiness; this pilot never reached its
  new implementation's emulator loop. Live model-offline task recovery remains
  unproven by this run.

## Measured usage and preservation

| Measure | Observed |
| --- | ---: |
| Assistant steps |104/400|
| Charged budget time |2713.814s/5400s|
| Proxy request IDs |104|
| Completed proxy requests |99|
| Disconnected requests |5|
| Requests with reported usage |99|
| Reported fresh input tokens |183959|
| Reported output tokens |18768|
| Reported cache-read tokens |12797226|
| Reported prompt tokens including cache |12981185|
| Sum of recorded request durations |622.102s|
| Configured self-hosted API billing |$0|
| Energy/hardware cost |Unmeasured|

Five disconnected requests lack usage, so recorded tokens are incomplete for those
requests. The sum of request durations is not task wall time, GPU compute time or
energy. Charged budget time conservatively includes infrastructure waiting. No
cost-per-passing-test ratio or numeric app-quality score is assigned to zero
accepted journeys.

A private SQLite backup, all tracked/untracked source (including Git baseline),
source manifest, model request JSONL and full command/job receipts are preserved
on Ubuntu at
`~/.local/state/strata-board-harness/phone-e2e-pilot-20261023/terminal-20261023T1249/`.
The complete source archive SHA256 is
`0213ac79631103db461194d97c6eebb480c7f23c08fa711925f80e6e3ab2c087`.
Sanitized metrics, source diff/manifest and final build receipts are in
`verification/phone-pilot-20261023/`. The original worktree remains unchanged.

## First post-pilot fix and remaining boundaries

Source now gives actual build-control review pauses and failure pauses independent
bounded allowances (three each; a fourth stops). Both retain authenticated manager
guidance, exact hash approval,660s waits, time/step caps and terminal cleanup.
Legacy budgets remain conservative. The stopped pilot is never resumed by an
upgrade. This change passed all **119 tests on macOS and Ubuntu3.10.12**, including
regressions for mixed reviews/failures, duplicate/restarted review requests, deadline
release, failure limits, legacy counters and terminal restart behavior.

This is **tested source only**. Ubuntu verification used a temporary directory,
removed afterward; the running daemon retains its original policy. No service
upgrade, budget reset, new worker, new ticket or new pilot followed the fix.

Remaining work is to audit real coordination latency during interruptions, constrain
read-heavy context growth with a meaningful early implementation build checkpoint,
and validate actual accessibility API discovery and the full run-inspect-fix loop.
These improvements must not weaken application assertions. A further live execution
requires deliberate human-authorized selection; this report grants no new dispatch.
