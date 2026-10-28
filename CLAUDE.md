# Strata Harness: instructions for Claude Code

This repository runs permanent local coding agents (OpenCode + any model) that take
tickets from an Agent Board. Claude Code is the **manager**: it creates agents,
assigns tickets, reviews branches and merges. The harness is optional; without it,
Claude can take board tickets and do the work itself.

## Setting up (when the user asks to "set up the board and agents")

1. Board. If `curl -s http://127.0.0.1:9140/health` fails, clone
   `git@github.com:Blackoutburst/agent-board.git` and run
   `python3 scripts/local_board.py install` there (starts it now and at login, no login page).
2. Connect yourself (the manager): `python3 <agent-board>/connect.py --client claude-code`.
   Tell the user to restart Claude Code (or run `/mcp`) so the `agent_board` tools load.
   Register with `register_agent` (agent_kind `claude-code`, a stable identity_key), then run
   `./harness manager grant` so you may accept or return tickets the agents put in review,
   and register again.
3. Check prerequisites: `./harness doctor` (OpenCode and git on PATH, board reachable).
4. Create each agent the user asks for, one command each:
   - A model the user's OpenCode already knows (any provider they configured or logged into):
     `./harness agent add glm --model <provider>/<model> --display-name "GLM 5.3 Flash"`
     (list what OpenCode knows with `opencode models`).
   - Any OpenAI-compatible server, including local ones (llama.cpp, Ollama, vLLM, LM Studio):
     `./harness agent add local --base-url http://127.0.0.1:8080/v1 --model <id> [--key-env VAR]`
     Use `--npm @ai-sdk/anthropic` (or another AI SDK package) for other API styles.
   `agent add` pairs the agent with the board, grants it worker permission, and runs a
   live model check. Never print or copy API keys; the harness reads them itself.
5. Start the agents: `./harness autostart install` (or `./harness serve` in the foreground).

## Managing (the loop)

- Projects need a repository: an HTTPS/SSH Git URL or an absolute local path.
- Create tickets with a clear description and acceptance criteria and set
  `assignee_agent_id` to the agent's board profile (`./harness agent show <name>`).
  Unassigned tickets are left for you unless an agent was added with `--take-unassigned`.
- An agent works on `agent/<name>/<number>-<slug>`, pushes that branch to the
  repository's origin, posts a report on the ticket and moves it to **review**
  (or **blocked** with the reason).
- Review: fetch the branch, read the diff, run the tests yourself. Then `claim_ticket` it
  (it stays in review) and either merge the branch into the target branch and set the
  ticket **done** with outcome/evidence, or post concrete feedback on the ticket and set
  it back to **todo** with `release_claim`: the same agent resumes on its branch and
  sees the discussion.
- Agents start a ticket only when its dependencies are **done**, so merge prerequisites first.
- Agents never push to main, change remotes, or weaken tests; you own merges.
- Cost and tokens per run are in each report and in `~/.local/share/strata-harness/agents/<name>/runs/`.

## Development of this repository

Run `python3 -m unittest discover -s tests` after changes. Local-agent code lives in
`strata_board_harness/local/`; the rest is the earlier single-worker Ubuntu pilot.
