# Strata Board Harness

A Python 3.10+ standard-library supervisor for a selected development worker using
Strata and the Blackoutburst Board. The Ubuntu daemon owns coordination and durable
state; the personal PC supplies inference and may be stopped independently.
This directory is a separate project. Git initialization and publication are left
to the human.

## Implemented and verified

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
