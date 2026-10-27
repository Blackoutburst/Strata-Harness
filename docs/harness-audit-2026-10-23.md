# Harness audit and first adapter decision

2026-10-23. This is a post-benchmark experiment. It does not change the frozen
Phone comparison or accept the unfinished Swift implementation.

## Evidence that determines the first changes

The original Swift run ended under the human's stop rule: round 2 attempt 3
reached 400 steps, its processes exited, and its dead claim was released. Only
round 1 received a completed review. All partial source, command output and token
evidence were preserved. The last partial source still fails instrumented-test
compilation: 15 diagnostics in five files. App assembly and four existing JVM
tests pass independently. No final instrumented runtime or privileged deployment
was accepted.

Across four developer turns, Swift consumed 144 min 17 s of active developer-run
wall time, 1,307,441 fresh input tokens, 306,472 output tokens and 177,825,049
accumulated cache-read tokens. Round 2 accounts for 114 min 53 s. Zero provider
error events were recorded; 26 tool-state errors were recorded. These counters do
not include all command failures: five Bash outputs contain both `BUILD FAILED`
and `EXIT=0`, caused by a pipeline masking Gradle's status. RTK was active: 649 of
746 Bash calls began explicitly with RTK. Original Qwen also used RTK, so this
comparison does not isolate its effect.

Claim renewals and addressed-inbox checks repeatedly missed cadence despite
manager reminders. This is a coordination failure to remove from the model's
responsibilities. Reduced compiler diagnostics show some useful fixing, but three
400-step attempts without a committed second-round handoff demonstrate that step
count alone does not ensure convergence.

The full interrupted workflow did not demonstrate faster completion than regular
Qwen's historical 62 min 52 s run. Configured API billing is $0; electricity and
hardware were not measured. Neither cache counters nor token totals establish
energy consumption. The final Swift quality score remains unfinalized because
round 2 was incomplete.

Evidence lives in the existing manager outputs: `phone-model-comparison-with-swift.md`
and `swift-benchmark-early-stop/{developer-metrics.json,final-build.log,result.json}`.
These are external experiment receipts, not dependencies to import into this project.

## Concrete engine comparison

| Candidate | Documented control surface | Evidence here | Remaining gate |
|---|---|---|---|
| OpenCode server | Headless HTTP, OpenAPI, session state, async prompts, abort and event stream | Strata provider and RTK exercised in the original CLI runs | Verify the installed API, durable session recovery and ping application boundaries; prove real command status |
| Pi RPC | Long-lived JSONL subprocess, request correlation and streamed lifecycle events | Primary documentation inspected; no local pilot | Verify Strata tool calls, RTK integration, session persistence and the exact installed protocol |
| Existing detached CLI runner | Bounded process with JSONL output and final summary | Repeated step-limit exits, missed Board cadence and no live steering contract | Retain as the historical baseline; it is insufficient as the new coordinator interface |

OpenCode documents local server authentication, a `/doc` schema, health/version,
session status, asynchronous prompt submission, abort and SSE events. An accepted
request is not evidence that guidance was applied. Installed-version behavior must
be checked before adopting the adapter.
[OpenCode server documentation](https://opencode.ai/docs/server/).

Pi's RPC protocol supports a Python parent controlling a child through JSONL.
Responses require request-ID correlation while events continue asynchronously;
session configuration must preserve durable state. Its lifecycle distinguishes
an immediate turn ending from the agent settling after automatic work.
[Pi RPC documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md).

Pi documents steering after the current turn's tools and before the next model
call. Queued acceptance is distinct from completion. Abort can leave queued
messages runnable, so a stop implementation must clear the queue as well as abort.
Image transport is documented, but that does not prove Swift's served vision path.
[Pi RPC commands](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-commands.md).

**First candidate: an OpenCode HTTP adapter behind an engine-neutral coordinator.**
This minimizes provider and RTK changes while we test the coordination fix. It is
a provisional implementation choice, not a claim of better quality or throughput.
Pi remains the comparison candidate if its verified control behavior is a better
fit. Do not install either as a live autonomous service before the adapter tests.

## Board contract observed in the owned server source

Read-only inspection of `agent-board/app.py`, `coordination.py` and `board.py`
established these constraints for a separately paired worker:

- Compact `read_inbox` presents a cursor acknowledging the preceding delivery.
  Commit the received page and next cursor together before presenting it again.
  Cursorless compact retries can replay unacknowledged messages. Persist and
  deduplicate message IDs; previews require canonical bodies, with pagination.
  Inbox polling announces presence but does not claim or execute work.
- `claim_ticket` and `renew_ticket` require the current ticket version. Renewal
  increments that version and refuses expired/replaced claims. Serialize all
  mutations for the active ticket. On conflict, reread and reconcile; never
  blindly replay a stale update.
- `release_ticket` preserves task history and can retain assignment. It does not
  clear another session's claim or an expired one. Dead-claim recovery is a
  separate operation with narrower identity/version/runtime requirements.
- A manager question must not be mislabeled as a human approval request. The
  worker should post one concise addressed blocker message with evidence and a
  stable idempotency key, then enter a bounded wait. Its own claim lifecycle must
  accurately reflect that wait and any stopped coding process.

No live Board adapter or new worker identity has been paired. The Mac manager's
session and credentials must not become the Ubuntu developer's identity.

## First implementation increments

1. **Durable coordination journal with fake services.** Store received pages,
   pending canonical guidance, message receipts, mutation retry keys and task
   versions in SQLite. Keep `received`, `submitted to engine`, `acknowledged` and
   `applied` distinct. Empty polling and renewal invoke no LLM. The first tests
   must cover process crashes between page receipt, persistence and cursor reuse.
2. **Own-identity Board adapter.** Use an explicit manager-selected task allowlist,
   verified paired identity and serialized versioned operations. Renew comfortably
   before expiry, independent of model/tool duration. If the model is offline,
   keep control processing alive; stop taking new work, checkpoint and release
   responsibility after a bounded grace period. A lease is never proof that a
   coding process exited or that a previous side effect was cleaned up.
3. **Engine adapter and observable steering.** Pin the tested version and inspect
   its schema in an isolated configuration. Queue only relevant canonical deltas.
   A ping test must observe the next safe boundary, one delivery acknowledgement
   and a subsequent effect. A stop test must prove no queued continuation starts.
   Reconnection must inspect the actual process, session, worktree and ticket
   before resuming. Cross-host worker exclusivity needs a verified cutover or
   shared execution guard; the current local flock is insufficient across hosts.
4. **Reliable build feedback and bounded self-test.** Capture the actual child
   return code independently of output compression; preserve raw logs. Require a
   compile checkpoint after a small edit batch. Repeated unchanged failure
   fingerprints or read-only loops trigger a checkpoint and actionable escalation
   before the step budget is exhausted. A final handoff requires an exact commit,
   clean source state, verified receipts and honest gaps, not CLI exit 0 alone.
   Add only the owned silent emulator loop after these controls work. Verify a
   known image through engine, request payload and served model before relying
   on screenshot diagnosis. Manager acceptance remains independent.

## Bounded pilot gates

| Experiment | Required observation |
|---|---|
| Empty Board polls and routine renewal | Zero inference calls; versions and durable receipts stay consistent |
| Reply lost after Board mutation | Reconcile exact retry key/current ticket; no duplicate effect or overwrite |
| Crash after inbox page saved | Restart retains pending guidance and deduplicates replay before cursor acknowledgement |
| Manager ping during a tool | Delivered at a verified safe boundary; acknowledgement and application recorded separately |
| Manager stop with queued work | Queue cannot restart generation; tool/process ownership and partial effects preserved |
| Synthetic persistent compiler failure | One concise manager escalation with fingerprint, attempted fixes and evidence |
| Model offline while Board remains online | Supervisor and steering remain responsive; bounded claim grace; checkpoint retained |
| Restart with another claimant or live worker | No duplicate process/claim; explicit reconciliation required |

Run the pilot against local fake Board/engine services first, then a bounded
human-authorized task with its own identity. Vary one factor at a time and compare
accepted journeys, review effort, total developer plus validation time, token
categories, tool errors and steering latency. Preserve the historical benchmark.

The existing eight supervisor tests passed on macOS and Ubuntu; the systemd unit
passed static verification. The service is not installed. This audit adds a design
and acceptance plan only: no live adapter, agent switch, model request, Board task
dispatch, Strata setting change or Git initialization was performed.
