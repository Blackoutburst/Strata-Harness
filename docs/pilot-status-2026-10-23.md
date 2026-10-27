# Phone harness pilot — current observation, 2026-10-23

The original model benchmark remains closed. This fresh pilot uses ticket
7667684d1e4fc8c0531c6dc0a51a8c0f, thread60193ddf392ed524c706dad266e2a703,
worker e1842df60e6de4ae0025464e7930f869 and engine
ses_e976e64ffffe4RYMXLu4fuhQGj. Its isolated checkout starts at
19d7458debbe540c9cb7f0061805f7af766cdf5a. No prior model implementation was reused.

At 12:20:54 UTC, the first deployed-service build had passed assembleDebug,
assembleDebugAndroidTest and testDebugUnitTest, in about70seconds. The scoped
checkpoint also completed with unchanged clean baseline source and verified owned
emulator cleanup. Manager message5521fe51efcfd513f710e4eb3bae6698 has distinct
submitted, acknowledged and applied receipts; application followed the build.
App implementation has started afterward; this is not final Phone acceptance.

The live crash test exposed infrastructure gaps beyond the original fake-service
checks. Every failed receipt and database/session was retained:

- Checkpoint/screenshot handlers incorrectly counted their own job thread as
  incomplete effect cleanup. They now check downstream effects; exception logs
  remain retrievable while uncertain cleanup stays fenced.
- A multiline HARNESS_BLOCKER marker was missed. Assistant-only whitespace-aware
  parsing now handles it, with a regression test.
- Restart discovery could read an old listening URL from the appended log before
  the current process listened. Readiness now starts at the current launch's byte
  offset, with actual same-session reattachment/no-generation verification.
- The user unit omitted AF_NETLINK, needed for bwrap loopback setup. Two real
  failing builds triggered an addressed blocker and a clean pause. A clean offline
  baseline build passed under matching transient-unit restrictions after adding
  the family; the deployed owned unit was then upgraded while the worker stopped.
  The build still runs in its isolated network/filesystem namespace.
- OpenCode1.19.3 message pagination uses an opaque X-Next-Cursor header. Using a
  message ID caused an HTTP error and safe claim release. The adapter now uses the
  bounded header, keeping the original origin and selected session. An actual
  25-user-message noReply probe verified pagination without model generation,
  plus restart/session/step preservation. Earlier short probes had not crossed
  this boundary. See the pinned upstream [handler](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts)
  and [pagination tests](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/test/session/messages-pagination.test.ts).
- Early-build attempt state now survives controller restart. An attempted or failed
  build remains distinct from a successful current-source build. Exact build-control
  approval can resolve an old request without turning its failed receipt into a pass.

All115 tests pass on macOS and Ubuntu. The real-engine synthetic probe passed RTK,
safe-boundary steering/ACK, exit37, path/command rejection, pagination and cleanup.
The original frozen comparison scores are unchanged.

Four manager-directed infrastructure reconciliations retained the SAME ticket,
checkout, engine session and consumed counters; no fresh implementation round or
initial prompt was submitted. All changes occurred after owned process cleanup.
The budget clock conservatively includes infrastructure waiting; its charged
seconds are not a measurement of model inference time. Raw proxy usage/timing and
validation durations are retained separately. No automatic cap-driven retry or new
pilot is allowed. Further terminal faults remain paused for diagnosis.

The Ubuntu user service is enabled with linger, and the same-chat ten-minute
manager monitor is ACTIVE. Board renewals and inbox polling are deterministic;
they do not require model reminders. Strata may be stopped by the human without
restarting the supervisor. Mac Board/emulator and private tunnel dependencies still
apply. Only caregiver35/emulator-5556 is authorized. Independent app diff/runtime,
privileged deployment, CI and physical ROM/SIM acceptance remain outstanding.

At12:25Z the own claim renewed and inbox polling continued. Manager observed55 steps with unchanged source and sent scoped first-slice guidance. The next observation showed Swift writing its Hilt runner, denied-state annotation and first Dial fixture. Those new app files have not yet been independently compiled/reviewed.

At12:29Z Swift had reached91 steps and added its first Hilt runner, denied-state annotation and Dial fixture. The build-control fence posted an addressed request and paused the same session. The manager reviewed the exact two Gradle diffs and approved their hashes for execution only. A real isolated dependency-prefetch build failed at app/build.gradle.kts:33 (`instrumentationRunnerArguments` unresolved, exit1 after12s). The full failure receipt was retained and actionable guidance delivered through the Board. Swift removed that unsupported configuration; the new app/build.gradle.kts hash was separately reviewed and approved. New Phone tests remain unaccepted and unrun. This exposed a harness ergonomics gap: ordinary build-control review consumes the same small blocker allowance as repeated failures. No live budget or permissions were changed to hide that limitation.

At12:37Z the revised Gradle configuration completed and public dependencies resolved in the owned isolated manager prefetch. App assembly completed, while test-APK compilation failed with one reported error at DialJourneyTest.kt:48 (`enableAccessibilityChecks` unresolved). Exit1/full raw log retained in verification/manager-reviewed-prefetch-20261023T1235.*. Manager returned the exact diagnostic with instructions to retain meaningful accessibility checks. No new runtime pass is claimed. The same pilot/counters continue; the monitor remains active.

Terminal update: the pilot stopped at12:39:42.723Z after104 steps/2713.814 charged seconds when a third build-control review became the fourth shared escalation. No runtime result. The full outcome, preservation, cadence gaps and tested-only119-test post-pilot policy are recorded in [phone-pilot-outcome-2026-10-23.md](phone-pilot-outcome-2026-10-23.md). Do not resume or deploy it implicitly.
