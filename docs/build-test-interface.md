# Selected Phone worker interface

Board intake, claims, renewals and inbox polling belong to the daemon. The model
receives the selected canonical brief and relevant authenticated guidance. It
never receives the Board credential and must not discover another task.

Use native `read`, `glob`, `grep`, `write` and `edit` inside the selected source
workspace. Bash has a narrow command grammar; RTK handles supported `ls` and
`git status --short`. The worker does not receive raw SSH, ADB, a shell interpreter
for arbitrary commands, or access to another repository.

The fixed commands are:

| Command | Result |
| --- | --- |
| `harness-tool build` | JDK17 `assembleDebug assembleDebugAndroidTest testDebugUnitTest`, full raw log receipt and real exit code |
| `harness-tool test` | Normal suite on the selected silent API35 emulator; excludes the denied-state annotation below |
| `harness-tool test denied` | Denied-state suite with role setup and external revocation before instrumentation starts |
| `harness-tool test class:tech.primal.attentive_os.phone.e2e.ClassName` | A normal-fixture diagnostic subset; does not establish a complete suite pass |
| `harness-tool status` | Compact owned job inventory; it does not start another operation |
| `harness-tool logs job-ID:0` | First full-log page; follow `next_offset` with the same job ID |
| `harness-tool screenshot` | Saved PNG from the latest owned failure, captured before VM rollback; inspect the returned real path with `read` |
| `harness-tool checkpoint` | Current commit, clean/dirty state, canonical working-source hash and effect-cleanup receipt |
| `harness-tool commit test:_Add_Phone_E2E_journeys` | A local commit; underscores become spaces. No push or hook execution. |

A command reporting `completed` can still have a failing exit code. Failed,
skipped, crashed and unrun tests are separate counts. Zero discovered/passed tests
cannot establish a runtime pass. Every build and APK is tied to its actual source
hash; a source edit after a build requires another build before testing or review.
The source hash is stable across a commit that changes no file content.

Build controls such as Gradle scripts and the wrapper require a manager-approved
hash after changes. The manager may approve a minimal dependency addition and
prepare required public artifacts; the model must not replace wrappers, disable
checks or blanket-ignore failures. The build namespace has no host home, Board or
SSH credentials, physical devices, or outbound network. A separately recorded
trusted-baseline prefetch warms its owned caches. JVM/Android builds must work
offline afterward. New source dependencies may need an actionable manager request.

## Denied permissions

Mark genuine denied-state tests with the runtime annotation
`tech.primal.attentive_os.phone.e2e.RequiresDeniedPermissions`. The worker defines
that annotation in its own test source; the harness supplies a filter contract,
not application-test implementation. Use the `denied` run for these tests.

The adapter assigns this Phone APK the Dialer role, then externally revokes
`CALL_PHONE`, `READ_CALL_LOG` and `ANSWER_PHONE_CALLS`, verifies the denied state,
and only then starts instrumentation. A denied-state fixture must not reassign the
role or use a blanket grant rule, since the role can re-grant denied permissions.
Revoking a runtime permission from inside instrumentation can kill the host.
`MANAGE_OWN_CALLS` is a normal permission on API35 and is not a runtime-revocation
case. Keep meaningful denied-state assertions instead of replacing them with
skips or a self-fulfilling mock.

Normal and denied runs have separate source/artifact/scenario receipts. A class
subset does not replace the full normal and denied suites. Accessibility checks,
owned test data and fake-call assertions remain application requirements. The VM
snapshot rollback restores exact settings/role/permission fingerprints and owned
emulator state; it does not excuse unsafe broad deletion in test fixtures.

## Control and handoff

The model acknowledges the exact `GUIDANCE_ACK` identifier supplied with manager
guidance. Acknowledgement is distinct from application. The special authenticated
`HARNESS_EXPECT_CHECKPOINT` probe has an observable receipt when the requested
owned checkpoint command actually completes. Other guidance needs its own
independent semantic verification.

A genuine blocker includes the precise failure, attempts and evidence. The daemon
posts one addressed manager escalation, pauses the current session, persists its
checkpoint and waits at most 660 seconds. Guidance can resume that same authorized
session after cleanup. Expiry, repeated blockers, a time/step cap, or an urgent
manager stop terminates the attempt; no automatic new run follows. Long owned
build/test jobs carry a verified thread/deadline observation so a legitimate
bounded connected run is not mistaken for an idle model loop.

The post-pilot source separates exact build-control review requests from failures:
each has three permitted pauses, with a fourth request in either category stopping
the execution. Deduplication, authenticated guidance, 660-second waits and build
hash approval still apply. This policy passed fake-service tests on both hosts but
is not installed in the daemon. The first pilot's stop remains terminal, and its
old shared-budget receipts are preserved.

Finish with a clean local commit and `HARNESS_REVIEW <full-40-character-commit>`.
The controller checks the exact commit and successful current-source build before
review handoff. Independent manager review still decides whether the journeys,
accessibility, safety, runtime results and deployment evidence are acceptable.
CI, privileged deployment and physical ROM/SIM acceptance remain separate claims.
