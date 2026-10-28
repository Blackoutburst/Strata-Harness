# Roadmap: portable, local, multi-agent harness

Target set by the owner on 2026-10-28.

## Final workflow

1. The user clones the Board repository and the harness repository on one device.
2. The user asks Claude to set up the Board MCP and to create a set of permanent agents
   (each one a model + harness profile: Qwen on Strata, GLM Flash, DeepSeek Flash, …).
3. The user creates tasks on the Board.
4. Claude, as manager, assigns tasks to those agents, reviews their work, and merges it.
   Every agent runs fully locally on the device that cloned the repositories.

## Requirements

- **Plain clone + setup.** No hand-copied files or host-specific paths. A setup step
  creates config, credentials, agent identities and services from the cloned repository,
  driven by Claude.
- **Strict sandboxing between agents.** Several OpenCode instances run at once and never
  step on each other. Each agent slot owns its own:
  - OpenCode home / config / data (its own `opencode.db`)
  - workspace and Git checkout
  - model proxy, gateway and ports
  - worker lock and state directory
  - Board identity and claim
  - emulator (or other runtime target), when the task needs one
- **Local-first.** One device runs everything: Board, harness daemons, engines and
  emulators. Remote hosts (an Ubuntu box, a separate model server) become optional.
- **Model-agnostic.** A slot declares its provider (Strata, Mistral EU, …), model id,
  context limit and pricing; nothing assumes a specific model.
- **Task-agnostic.** E2E testing is one task type. Playbooks, emulator targets and
  validation steps are chosen per task type; feature and bugfix work must also fit.

## Where the current harness stands

The current deployment (2026-10) is a pilot with a fixed topology: one Ubuntu daemon, one
Mac bridge and emulator, a reverse SSH tunnel, and the manager driving dispatch from the
Mac. Pieces already moving toward the target:

- Per-run OpenCode isolation (own `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `opencode.db`).
- Model proxy with configurable upstream, key and model; token usage is recorded.
- Emulator allowlist for several isolated emulators (`emulator-5556`, `emulator-5558`).
- Per-slot worker lock paths and state directories are configurable.

Done (2026-10-28, `strata_board_harness/local/`, `./harness`):

- Agents (slots) with their own OpenCode home/config/data/cache, checkout per ticket,
  per-run proxy and lock; any number run at once.
- Local mode: board, agents and engines on one device, no tunnel.
- `harness agent add` creates an agent from the user's OpenCode provider setup or an
  explicit endpoint, pairs it with the board, grants worker permission and checks the model.
- Board-driven dispatch: agents take tickets assigned to them; review feedback returns
  a ticket to the same agent and branch. Autostart on macOS, Linux and Windows.
- Task-agnostic prompts (task, criteria, project context, discussion, standing instructions).

Remaining:

- OS-level sandboxing (bubblewrap on Linux, sandbox-exec on macOS) on top of the
  per-agent directory isolation.
- Per-task-type playbooks and optional runtime targets (emulators) for local agents.

Original next steps:

1. **Slots.** A slot (a, b, c, …) is a named, fully separate instance: config, state,
   service unit, Board identity, lock, ports, emulator. Dispatch takes `--slot`.
2. **Local mode.** Daemon, bridge and engine on the same device, with no tunnel.
3. **Setup command.** `setup` creates a slot from the clone: directories, keys from the
   user's provider auth, Board agent registration, service files.
4. **Per-task-type playbooks and optional runtime targets.**
