# Strata Harness

Permanent local coding agents for an [Agent Board](https://github.com/Blackoutburst/agent-board).
Each agent is OpenCode plus any model: whatever provider your OpenCode already knows
(hosted, custom, or a model server on your own machine), or any OpenAI-compatible
endpoint. Claude Code acts as manager: it assigns tickets, reviews the agents'
branches and merges them.

The harness is optional. A board plus Claude Code alone can already work
autonomously; the harness adds third-party sub-agents.

## Quickstart

Needs Python 3.10+, Git and [OpenCode](https://opencode.ai), and a running local board
(`python3 scripts/local_board.py install` in the agent-board repository).

```sh
git clone git@github.com:Blackoutburst/Strata-Harness.git && cd Strata-Harness
./harness doctor
./harness agent add glm --model <provider>/<model> --display-name "GLM 5.3 Flash"
./harness agent add local --base-url http://127.0.0.1:8080/v1 --model <id>   # any compatible server
./harness autostart install          # run all agents now and at every login
```

(`harness.cmd` on Windows.) Or simply ask Claude Code in this directory to set it up:
[CLAUDE.md](CLAUDE.md) has the steps it follows and the manager workflow.

`agent add` takes the endpoint, AI SDK package, model limits/costs and key location
from your OpenCode configuration (`--model provider/model`, see `opencode models`), or
from flags (`--base-url`, `--model`, `--npm`, `--key-env`). Nothing about any provider
is built in. It pairs the agent with the board, grants it worker permission and runs
one live check through the agent's sandbox.

### How an agent works

- It takes tickets assigned to it (`assignee_agent_id`), one at a time, and keeps the
  claim alive while working.
- It clones the project repository (HTTPS/SSH URL or local path) into its own
  checkout and works on `agent/<name>/<number>-<slug>`.
- OpenCode runs with the agent's own HOME and XDG config/data/cache/state, so several
  agents run at once without sharing sessions, databases or settings. A loopback
  proxy holds the real API key; the coding process only sees a throwaway token and no
  other credentials from your environment. A plugin keeps file tools inside the
  checkout and reserves push, remote and branch changes to the harness. This
  isolates agents from each other and from your files; it is not an OS security sandbox.
- At the end the harness pushes the branch to origin, posts the agent's report with
  tokens and cost on the ticket, and moves it to review (or blocked with the reason).
  Feedback on the ticket plus status todo sends it back to the same agent and branch.

```sh
./harness agent list | show NAME | remove NAME
./harness run NAME --ticket ID      # one ticket in the foreground
./harness serve [--agent NAME]      # what autostart runs
```

Agent data lives in `~/.local/share/strata-harness/agents/<name>/` (`HARNESS_HOME` to change).

## Earlier Ubuntu pilot

The rest of this repository is the earlier single-worker pilot: an Ubuntu daemon with a
Strata model server, network-isolated Android builds and a Mac emulator bridge.

### Implemented and verified

- systemd user supervisor with offline backoff, SQLite receipts, status, graceful
  shutdown and an OS-held supervisor lock.
- Paired worker identity distinct from the manager; selected-task/versioned
  claim lifecycle, timed renewals and addressed inbox polling without LLM calls.
- Durable canonical message/cursor journal, deduplication, safe-boundary guidance,
  separate received/delivered/acknowledged/applied receipts, urgent pause/stop,
  bounded manager waits and source-progress/loop checks.
- Owned OpenCode 1.19.3 v1 process/session adapter with RTK 1.0.1, controlled tool
  grammar, repository boundary, exact command failures and capped execution.
- A model-scoped loopback proxy recording request settings and raw usage while
  keeping the real Strata credential outside the coding process.
- JDK17 Android build gateway in a network-isolated namespace, approved build
  controls, full error logs and source/APK hash binding.
- A serial-restricted validation bridge to the owned silent API35 emulator. It
  snapshots/restores the VM and checks settings, role and permission fingerprints.
  Real instrumentation failures and zero-test fixtures remain failures/gaps.

All **119 tests pass on macOS and Ubuntu Python 3.10.12**. The four new post-pilot
regressions separate bounded build-control reviews from failure budgeting; that
change is tested source, not installed in the running daemon. The deployed pilot
used the earlier 115-test version. Actual Ubuntu probes
verified installed-engine RTK rewriting, safe-boundary manager acknowledgement,
abort cleanup, nonzero exit37, forbidden command/read rejection, and a real Strata
image read through OpenCode. Clean offline baseline Android builds passed. A real
baseline instrumentation failure was preserved with its PNG and exact VM rollback;
the denied fixture independently observed external denial and explicitly rejected
a baseline APK with no denied-test annotation. These are readiness observations,
not accepted Phone implementation results. See the final receipts in
[verification/](verification/) and [the architecture](docs/architecture.md).

The own-identity Ubuntu user service is installed/enabled with linger. At readiness
completion it has no selected coding task. The first selected execution is the
human-authorized [fresh Phone E2E pilot](docs/phone-e2e-pilot.md), with one 90-minute,
400-step budget. Its results are separate from the closed original benchmark.
Passing infrastructure checks do not establish autonomous-worker reliability over
an entire real task; that is what the bounded pilot measures.

## Run and inspect

```sh
cp config.example.json config.local.json
# Supply the existing private credential in STRATA_API_KEY, outside source/logs.
python3 -m strata_board_harness --config config.local.json run --once
python3 -m strata_board_harness --config config.local.json run
python3 -m strata_board_harness --config config.local.json status
python3 -m unittest discover -s tests -v
```

No runtime selection means no claim and no coding process. A private runtime file
selects one exact task/workspace/readiness receipt; Board prose cannot expand it.
The state directory defaults to `$XDG_STATE_HOME/strata-board-harness` or
`~/.local/state/strata-board-harness`; `STATE_DIRECTORY` takes precedence.

## Ubuntu deployment and boundaries

The current installation uses `deploy/strata-board-harness.user.service`, source in
`~/.local/lib/strata-board-harness`, private configuration in
`~/.config/strata-board-harness`, and state in `~/.local/state/strata-board-harness`.
`loginctl enable-linger` lets the user service remain after logout. Inspect with:

```sh
systemctl --user status strata-board-harness
journalctl --user -u strata-board-harness -f
python3 -m strata_board_harness --config ~/.config/strata-board-harness/config.json status
```

The install/pairing scripts refuse replacement of existing identities/configs.
Credentials stay outside the project. Inspect existing state and exact owned
processes before an upgrade. Never clear another worker lock or infer death from
a claim expiry. The supplied root-service unit is a foundation example and needs
explicit workspace/cache/SDK mounts before coding use; it is not the deployed unit.

For this first pilot, the Board and emulator remain on the Mac behind private SSH
forwards. The Ubuntu coordinator is independent of model inference, but these
control/validation dependencies still require the Mac and tunnel. A live bridge
reservation guards the Mac coding lock, and an owned process lock guards Ubuntu;
this verified single-host coding cutover is not a general distributed lease service.

A stopped model does not restart the daemon or Strata. Coordination persists while
inference is unavailable; after bounded claim grace the attempt checkpoints and
releases only after verified cleanup. A capped/stopped pilot requires deliberate
manager reconciliation; it never automatically launches another attempt.

## Contracts and further evidence

- [Architecture and lifecycle](docs/architecture.md)
- [Board adapter](docs/board-adapter.md), [coordination journal](docs/coordination-journal.md)
  and [coordinator](docs/coordinator.md)
- [Installed engine control](docs/engine-control.md) and [session client](docs/engine-client.md)
- [Selected build/test interface](docs/build-test-interface.md)
- [Original failure audit and engine comparison](docs/harness-audit-2026-10-23.md)

Earlier dated receipts preserve cold-start failures, intermediate test counts and
pre-wiring boundaries. Current integration receipts supersede those readiness
snapshots, without erasing their failures. CI, privileged system deployment and
physical ROM/SIM acceptance remain independent manager checks. Hardware energy is
unmeasured; fewer tokens alone do not establish lower power consumption.

Current live observations and preserved infrastructure failures: [Phone pilot status](docs/pilot-status-2026-10-23.md).

The first pilot is now **stopped and incomplete**, with no runtime acceptance.
Its source/logs are preserved, coding processes and claim released, and the daemon
remains available. No automatic follow-up pilot is authorized. See the
[pilot outcome and post-pilot policy boundary](docs/phone-pilot-outcome-2026-10-23.md).
