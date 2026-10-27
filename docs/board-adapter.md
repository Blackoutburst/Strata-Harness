# Selected-task Board adapter

This increment adds a standard-library HTTP client for the owned Board's stateless
JSON MCP endpoint, exact worker identity verification, and a durable selected-task
claim/renew/release lifecycle. It is tested against a loopback fake HTTP Board.
It is not paired to a real Board worker or wired into the running supervisor.

## Credentials and identity

`MCPHTTP` reads its bearer token from a named process environment variable at each
request. Source/configuration contains the variable name, not the credential.
Use HTTPS, or HTTP on loopback only. Reject credential-bearing/query URLs,
redirects, missing tokens, oversized responses and mismatched JSON-RPC reply IDs.
Requests have a configured network timeout of at most 15 seconds and no automatic
HTTP retries. Error strings report categories/status codes without response prose
or tokens. Authentication/redirect errors are configuration failures; unreachable
transport is a separate category from tool/RPC errors.

Registration initializes protocol version `2025-03-26`, then verifies the paired
owner, agent, device, session, identity binding key and profile version against
the expected worker. Inbox responses must retain that identity/profile pin. The
client follows the owned server's `stateless_http=True, json_response=True`
configuration observed in `agent-board/app.py`; generic stateful/SSE MCP servers
and automatic token refresh are not implemented. It does not import the Mac
manager connector or copy its credentials.

## Task boundary

The client receives an explicit selected-ticket allowlist from trusted
orchestration. It exposes no task discovery or claim-next operation. The lease
layer requires that the selected ticket is assigned to this exact worker and an
allowed owner; a missing worker assignment does not count as authorization.

`TaskLease.claim()` reads the current ticket, requires unclaimed `todo` work with
no known blocker, and submits that version. `renew()` reads the current version
and checks a supplied execution verifier before and after that read. The callback
must be implemented against actual worker/session ownership by the future runner;
a daemon heartbeat or saved PID is insufficient. Both renewal and release require
the exact owned live claim and profile pin. Expired or foreign claims are refused.
There is no expired-claim recovery API in this adapter.

Release retains the worker assignment and accepts only `todo`, `blocked` or
`review`, with a reason and a durable retry key. It cannot mark a task `done`,
reassign it, or substitute another session identity. A review status is a handoff,
not manager acceptance; the coordinator must separately verify commit/build/test
evidence before selecting that status.

## Mutation fencing and recovery

Before an external mutation, persist its operation, current version, exact
arguments and timestamp in SQLite. A conditional in-flight slot prevents another
mutation from starting while one is pending. Successful replies must have the
expected next version and claim/release shape, then produce a durable receipt.

A conflict, lost response or invalid reply leaves the intent pending and fences
further mutations. Reopening the database preserves that state. A fresh ticket
read does not clear the fence, and no automatic retry is issued. Claim/renew
operations are not assumed to have an idempotency API; a JSON-RPC request ID is
only response correlation. Release's retry key remains saved after a lost reply.

`resolve_uncertain(evidence)` is a trusted coordinator recovery hook, not a model
tool. Its caller must inspect the actual process/session, worktree and any prior
external effects, then supply concrete evidence. The hook rereads the current
ticket, refuses a foreign or expired claim, and records reconciliation before
clearing the local fence. It never clears a remote claim, claims work or runs a
command. Evidence is not automatically verified by this persistence layer.

All calls must be serialized on one coordinator thread owning the SQLite
connection. This is not a distributed execution lock. A changed claimant, expired
claim or ambiguous prior side effect still requires the separately authorized
recovery path; lease expiry alone is not proof of cleanup.

## Verification and remaining work

Eleven new tests use real loopback HTTP requests for initialization/registration,
current-version claim/renew/release, inbox/profile checking, explicit assignment,
scope rejection, dead/expired execution, death during a ticket read, concurrent
version change, lost claim/release replies, restart recovery, authentication,
redirects, reply-ID mismatch and refusal of completion/reassignment operations.
The original 18 supervisor/journal tests run alongside them.

The first test run exposed a fixture bug: the disconnect injector compared two
unset values and dropped initialization replies. The injector now requires an
explicit operation; the transport/identity checks were retained.

Still required before the Phone pilot: real worker pairing, canonical relevant
body/discussion retrieval, bounded inbox/renewal scheduling and outage policy,
engine events and safe-boundary steering, trusted recovery evidence, actual
process verification, reliable build execution and the scoped emulator adapter.
No live claim, coding process, service installation, Strata configuration change
or original benchmark retry occurred in this increment.
