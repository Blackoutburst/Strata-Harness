# Durable coordination journal

The journal is the local persistence layer for future Board and engine adapters.
It uses the supervisor's private SQLite database. It makes no network or model
requests, does not claim tasks and does not start a coding process. The running
supervisor does not yet call it. Current tests supply fake protocol interfaces.

## Inbox acknowledgement boundary

Bind a `Journal` to the separately paired worker's exact owner, agent, device and
session IDs. A database belonging to a different reader is rejected. A returned
Board session must match all four IDs before the page can be saved.

`poll_once(read_page)` passes the saved cursor to the reader, then saves every
message envelope and the returned cursor in one transaction. The next poll may
present that cursor, acknowledging the already durable page. If a process exits
before commit, the previous cursor remains available for replay. If it exits
after commit, messages remain pending even after the Board acknowledgement.
Empty pages advance only the reader cursor; pending guidance and checkpoints
remain unchanged, without inference.

Message IDs deduplicate replay. A repeated ID with changed metadata stops cursor
advancement and requires source reconciliation. This deliberately fails closed;
the future adapter must handle legitimate remote edits or mutable metadata
explicitly rather than silently replacing pending guidance. A stale concurrent
poll cannot overwrite the reader cursor or keep its newly inserted messages.

Previews are not sent to an engine. The adapter must fetch the complete canonical
body, following pagination with the expected source hash, before `save_body`.
That method verifies the UTF-8 SHA256 and retains the original source. A later
body change requires reconciliation. Hash checking protects integrity; it does
not authenticate the author or authorize the message's instructions.

## Delivery evidence and uncertain outcomes

| Stage | What has been recorded | What it does not establish |
|---|---|---|
| `received` | Addressed envelope persisted | Full canonical source or authorized guidance |
| `ready` | Canonical body matches its expected hash | Authorization or a safe execution boundary |
| `prepared` | Stable engine session and request ID saved before a possible send | Whether the engine received anything |
| `submitted` | An adapter supplied evidence of engine acceptance | Model acknowledgement or application |
| `acknowledged` | An adapter supplied a distinct acknowledgement receipt | Observable application |
| `applied` | An adapter supplied separate effect evidence | Independent manager acceptance of the task |

Only `applied` messages leave the pending view. Receipts retain timestamps and
structured evidence, enforce stage ordering and deduplicate exact retries.
Conflicting receipt evidence fails closed. `prepared` is an outbox intention,
not permission to automatically send. If a crash follows external acceptance but
precedes a receipt, the future adapter must query/reconcile the same request ID.
It must not blindly resubmit or invent a new request ID. If the engine cannot
reconcile that outcome, stop for explicit reconciliation. This module alone
provides no exactly-once external execution guarantee.

The journal does not verify supplied evidence against a real engine. Author
trust, selected-task scope, safe-boundary delivery, urgent-stop semantics and
actual observable effects must be implemented and tested in the coordinator and
engine adapters. Recording a fixture receipt proves persistence, not live steering.

## Checkpoints and recovery

An opaque per-task checkpoint retains its engine session, payload and timestamp.
A different engine session cannot replace it through the ordinary save method.
The checkpoint does not establish a live process, a clean worktree or a current
Board claim. The future restart path must inspect those facts before resuming.

Keep the supervisor's OS lock around the state directory. The journal is not a
distributed worker lock, task lease manager or authorization store. Credentials
remain outside its source and receipts. Apply the existing bounded model-offline
grace and ownership-reconciliation design before any live worker cutover.

## Verified in this increment

Ten new tests cover page/cursor atomicity, duplicate replay, stale polls, reader
identity mismatch, canonical-body verification, uncertain external acceptance,
receipt ordering and checkpoint/session preservation. Two tests force actual
child-process exits: one inside the page transaction, another immediately after
commit. These are process-crash tests, not simulated power-loss guarantees.

The eight supervisor tests remain part of the same suite. No live Board adapter,
claim renewal, coding-engine submission, stop/steering guarantee, service
deployment or model/image compatibility is claimed by this increment.
