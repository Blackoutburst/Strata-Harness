# Selected OpenCode session component

`strata_board_harness/engine.py` provides a bounded v1 HTTP client and durable
guidance/abort receipts for one **pre-created** selected session. It is separate
from the launcher and daemon. It neither claims tasks nor starts workers.

## Binding and authorization

The trusted dispatcher supplies a selected task, engine session, canonical existing
workspace, agent, provider and model. SQLite preserves this binding and rejects
changes on restart. Credentials are read through environment-variable names;
bindings and errors do not contain their values. HTTPS or loopback HTTP is
required, redirects are rejected, request timeouts are at most 15 seconds and
responses are capped at 1 MiB. Oversized evidence fails rather than being silently
truncated.

The component requires trusted process-verification and canonical-author/task
authorization callbacks. Before mutation it checks process ownership, pinned
engine version, session and workspace, then checks the process hook again. Board
text cannot select another binding. Fake callbacks do not establish live process
ownership or cross-host exclusivity; the future launcher must supply those facts.

## Durable guidance and stop

The journal first retains hash-checked canonical guidance. After authorization,
`deliver` saves a stable request ID and `prepared` intention before HTTP submission.
The guidance envelope retains the source body and includes an explicit
`GUIDANCE_ACK <request_id>` receipt instruction.

HTTP 204 records `submitted`. If its reply is lost, the saved intention survives
restart and the POST is never repeated automatically. Reconciliation reads the
exact request ID and checks its user role, session and canonical envelope. A
missing or changed message does not authorize a retry: a delayed original request
may still take effect.

`acknowledge` requires an assistant-role message with the submitted prompt as its
parent and the exact acknowledgement line. It records `acknowledged`, never
`applied`. Application requires independent evidence. Durable abort intentions
also have stable IDs; repeated/restarted calls do not issue another abort. Even
an accepted abort reports `quiescent=false` and `cleanup_verified=false` until the
process/tool observers establish those facts.

`command_results` exposes actual tool exit codes, output and truncation metadata.
A completed tool with exit 37 fails; missing/boolean exits never pass. Exit zero
is only shell-process evidence. The future direct command gateway must preserve
the intended build's full log/status independently of a model's tail pipeline or
summary. A successful inner Gradle command cannot be inferred from a pipeline's
zero exit code.

## Verification on 2026-10-23

All **57 tests** pass on macOS and Ubuntu Python 3.10.12. New loopback fake-engine
tests cover lost admission/abort replies and SQLite restart, wrong author/role/
parent/workspace/version, process death during verification, immutable bindings,
credential/redirect/body limits and nonzero exits. The first test run exposed a
fixture bug parsing a bodyless abort POST; its failure receipt is retained and
the fixture was corrected without weakening production checks.

The actual installed OpenCode **1.19.3** probe on macOS passed RTK rewriting,
tool-boundary steering, acknowledgement, owned-tool interruption and preservation
of an exit-37 failure. It then exercised `SelectedSession` through the inspector's
authenticated transport against that real API: separate submitted/acknowledged
receipts were stored and reconciled while `applied` stayed false. `EngineHTTP`
itself was tested against fake HTTP; using it with a managed live engine remains
an integration gate.

A bounded startup trace completed session creation in **6.01 seconds**, before
any model request. It observed dependency installation and outbound HTTPS sockets.
Two subsequent full control probes passed. Earlier cold-start timeouts remain
preserved; their exact cause is **unconfirmed**, so they are not labelled fixed.
Dependency preparation/session readiness must occur outside active coding time
and claims. There is no automatic retry chain.

Receipts are under `verification/2026-10-23-`:

- `engine-client-local.json` and `engine-client-ubuntu.json`: test output/hashes.
- `engine-client-first-failure.json`: original fake-server parsing failure.
- `engine-client-installed.json`: actual engine plus fake-model controls.
- `engine-bootstrap-trace.json`: bounded no-inference startup observations.
- `engine-client.json`: aggregate boundaries and results.

Ubuntu verification used a temporary directory that was removed. No installed
Ubuntu engine/RTK/Strata behavior, real Strata request or real Board developer
operation was tested. No service was installed, worker paired or pilot dispatched.

## Remaining integration

Wire the coordinator, Board client, journal and engine through one daemon
lifecycle. Implement durable session creation/readiness, uncertain-creation
recovery, bounded history/event reading, coding checkpoints, actual process/tool
observations and offline/pause/stop/restart behavior. This component currently
reads individual messages by ID; it is not a complete streaming event pump.

Keep the Board timer independent of inference and claims out of failed startup.
Complete the direct command gateway, early-build/repeated-failure policy, useful
blocker escalation, authenticated manager controls, own worker pairing,
cross-host single-worker reconciliation and restricted validation adapter. Verify
actual image requests before screenshot diagnosis. The fresh Phone pilot remains
queued and the original benchmark remains closed. GLM stays held.
