# Installed coding-engine control preflight

2026-10-23: OpenCode 1.19.3 was inspected and exercised on macOS using temporary
HOME/XDG directories, a loopback fake model, copied RTK plugin and owned test tools.
No Strata request, real Board developer session, Phone source change or device
operation occurred. This is an adapter preflight, **not a wired autonomous worker**.

## Observed capabilities

| Check | Evidence | Outcome |
| --- | --- | --- |
| Authenticated HTTP/OpenAPI discovery | `verification/2026-10-23-opencode-api.json` | Unauthorized health request returned 401; authenticated health reported 1.19.3. |
| v1 prompt during a running tool | `verification/2026-10-23-opencode-control-v1.json` | Ping reached the next fake coding request after the owned tool completed. No second coding request occurred during the held tool. Actual assistant history contains `HARNESS_ACK`. |
| v1 RTK | Same receipt | Model requested `ls`; completed tool history records `rtk ls`, exit 0 and compressed RTK output. `RTK_OPENCODE_AGENT=harness-fixture`; copied plugin unchanged. |
| v1 urgent abort | Same receipt | Owned sleeping tool process exited within the five-second observation deadline; pending guidance caused no continuation during the following 0.5-second observation. |
| v2 steering/interrupt | `verification/2026-10-23-opencode-control-events.json` | Durable history shows ping admission during the tool, tool completion, ping promotion and subsequent step; interrupt stopped the owned tool. |
| v2 RTK | Same receipt | Actual command remained `ls`, so this route failed the RTK gate. Do not assume plugin support from its configuration alone. |
| v2 session wait | `verification/2026-10-23-opencode-control-diagnostic.json` | HTTP 503: installed endpoint reports that session wait is not available yet. OpenAPI presence is insufficient. |
| Fresh session bootstrap | Timeout receipts below | Later isolated v1 session creation repeatedly timed out before any fake-model request. Readiness remains blocked. |
| Nonzero command status | `verification/2026-10-23-engine-client-installed.json` | Later strengthened run preserved exit 37 and full owned failure text even when tool status was `completed`. |

The v1 control path is the first candidate because its successful installed run
retained RTK. That receipt does not prove reliable cold startup, production model
behavior, semantic application of arbitrary guidance, distributed exclusivity or
permanent cancellation of queued work. A delivered HTTP prompt is not an
acknowledgement. An acknowledgement is not proof that requested work was applied.

The v1 endpoints exercised are `POST /session`,
`POST /session/{id}/prompt_async`, `GET /session/{id}/message` and
`POST /session/{id}/abort`. The v2 alternatives are `/api/session`, its
`/{id}/prompt`, `/{id}/history` and `/{id}/interrupt` routes. Admission/history
shapes differ; an adapter must pin and validate its chosen version.

## Cold-start investigation and preserved failures

**Later update:** a bounded startup trace completed in 6.01 seconds while observing
dependency downloads. Subsequent full v1 control runs passed, including the
exit-37 gate and the new [selected-session component](engine-client.md). Earlier
timeouts remain preserved below; their exact cause is unconfirmed. Startup is
intermittent, not labelled fixed, and readiness must precede coding/claims.

The last successful v1 probe started at 08:49:43 UTC. Subsequent runs reached
authenticated health and OpenAPI discovery but timed out creating the first
session. Their fake-model request arrays are empty. DEBUG logs end during
instance bootstrap after loading the temporary global configuration. The exact
cause is unresolved; this is an infrastructure readiness failure, not an app
quality result or a Swift model loop.

The initial 15-second timeout was retained, followed by a 30-second diagnostic
deadline. Disabling default plugins, file watching and ancestor project config,
canonicalizing macOS temporary paths, and restoring the original ERROR log level
did not resolve the timeout. These are failed hypotheses, not fixes. Receipts:

- `2026-10-23-opencode-control-cold-start-timeout.json`
- `2026-10-23-opencode-control-configuration-timeout.json`
- `2026-10-23-opencode-control-session-debug-timeout.json`
- `2026-10-23-opencode-control-isolated.json`
- `2026-10-23-opencode-control-project-isolation.json`
- `2026-10-23-opencode-control-canonical-paths.json`
- `2026-10-23-opencode-control-error-log-level.json`

All are under `verification/`. They retain the error, server logs where enabled,
exact fake requests and server/lock cleanup receipts. Three older probes reported
incomplete temporary-directory cleanup: the server could recreate npm cache files
after the tree was removed. The inspector now stops the server first, with four
cleanup regression tests. Two leftover paths were proven from their receipts/logs
and removed after verifying no OpenCode process remained. The earliest failure
omitted its temporary path; prefix-only deletion was refused. See
`verification/2026-10-23-engine-preflight-cleanup-reconciliation.json`.
`opencode-control-verified.json` is an
earlier **failed** diagnostic receipt despite its filename; inspect its `passed`
field. The aggregate receipt is
[`verification/2026-10-23-engine-preflight.json`](../verification/2026-10-23-engine-preflight.json).

Pinned upstream source shows that instance bootstrap initializes configuration
and plugins, and configuration schedules dependency installation which plugins
can wait for. Dependency bootstrap is a next diagnostic target; it is not a
confirmed explanation for this timeout. Do not patch the installed binary, disable
RTK or increase the worker budget to conceal an unresolved startup failure.

## Reproducing a bounded probe

These scripts are opt-in diagnostic tools, separate from the daemon. They first
refuse an existing OpenCode process or shared worker lock. Use the actual shared
lock for any installed-engine probe, not a fresh private lock that bypasses the
single-worker policy. The fixture uses only its temporary workspace and an
allowlist of owned commands. It never selects a production model or task.

```bash
python3 scripts/inspect_opencode.py --binary /path/to/opencode \
  --worker-lock /path/to/shared/opencode-worker.lock \
  --output verification/api-local.json --api-output verification/openapi-local.json

python3 scripts/probe_opencode_control.py --binary /path/to/opencode \
  --rtk-plugin /path/to/rtk.ts --api-version v1 \
  --worker-lock /path/to/shared/opencode-worker.lock \
  --output verification/control-local.json --api-output verification/openapi-local.json
```

The control fixture requires RTK on PATH, verifies a representative hook rewrite,
checks actual tool history, and requires assistant-role acknowledgement. It also
verifies the exit-37 gate. A timeout fails the probe; there is no
automatic retry. Session, tool and model receipts stay separate from API health.

Server process-group ownership and tool PID ancestry/fingerprints are tracked;
cleanup stops the owned server before removing its temporary workspace. Only a
lock with this invocation's nonce is removed. Fake executable tests cover cleanup
on normal completion and injected failure, plus existing/changed lock ownership.
These tests do not substitute for actual installed-engine integration checks.

## Gates before the Phone pilot

1. Make dependency/session startup an explicit bounded readiness phase and retain
   failed-start evidence. The strengthened exit-status gate has now passed.
2. Wire the bounded selected-session client to the coordinator and daemon; finish
   event/checkpoint reconciliation and the launcher. No blind prompt replay.
3. Verify true build exit status and full error receipts; enforce early builds,
   bounded blocker waits and stale-progress/loop detection.
4. Test model-offline/restart and urgent control with fake services, then verify
   the chosen engine/RTK on Ubuntu and the actual Strata tool-call path.
5. Pair a distinct worker identity, prove cross-host single-worker cutover and
   expose only the approved serial-restricted validation adapter. Verify image
   requests end-to-end before relying on screenshots.

The first live task remains the separately authorized fresh Phone pilot. No
service has been installed, no new developer paired, and no pilot dispatched.
GLM recovery remains held.

Primary references inspected on 2026-10-23:
[OpenCode server](https://opencode.ai/docs/server/),
[plugin loading](https://opencode.ai/docs/plugins/),
[CLI flags](https://opencode.ai/docs/cli/),
[1.19.3 instance bootstrap](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/project/bootstrap.ts),
[1.19.3 configuration](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/config/config.ts),
[1.19.3 dependency installation](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/core/src/npm.ts).
