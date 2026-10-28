import { existsSync, realpathSync } from "node:fs"
import { dirname, resolve, sep } from "node:path"

// Local agents: file tools stay inside the ticket checkout; publishing and
// privilege commands are reserved to the harness and the manager.
const root = realpathSync(process.cwd())
const denied = [
  /\bgit\s+push\b/,
  /\bgit\s+remote\s+(?:add|set-url|remove|rm|rename)\b/,
  /\bgit\s+config\s+--(?:global|system)\b/,
  /\bgit\s+(?:checkout|switch)\s+(?:-[bBcC]\s+)?(?:main|master|development)\b/,
  /\bgh\s+(?:pr|release|repo)\b/,
  /\bsudo\b/,
  /\bssh\b|\bscp\b/,
  /\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(?:\/|~|\$HOME)(?:\s|$)/,
]

// Extra per-agent rules from the harness (agent.json "deny"), e.g. no device access.
for (const pattern of JSON.parse(process.env.HARNESS_DENY ?? "[]")) denied.push(new RegExp(pattern))

function inside(value: unknown, writing = false) {
  if (typeof value !== "string" || value === "") return
  const target = resolve(root, value)
  if (target !== root && !target.startsWith(root + sep)) throw new Error("Outside this ticket's checkout: " + value)
  const parts = target.slice(root.length).split(sep)
  if (writing && parts.includes(".git")) throw new Error("Git internals are read-only; use git commands")
  let parent = target
  while (!existsSync(parent)) parent = dirname(parent)
  const actual = realpathSync(parent)
  if (actual !== root && !actual.startsWith(root + sep)) throw new Error("Symlink escapes this ticket's checkout")
}

export default {
  id: "harness-workspace-guard",
  async server() {
    return {
      "tool.execute.before": (input: any, output: any) => {
        const tool = String(input?.tool ?? "").toLowerCase()
        const args = output?.args ?? {}
        if (tool === "bash" || tool === "shell") {
          const command = String(args.command ?? "")
          for (const rule of denied) {
            if (rule.test(command)) throw new Error("Reserved to the harness/manager: " + command.slice(0, 200))
          }
          inside(args.workdir)
        }
        if (["read", "edit", "write", "glob", "grep", "list"].includes(tool)) {
          inside(args.filePath ?? args.path, tool === "edit" || tool === "write")
        }
        if (tool === "apply_patch" && typeof args.patchText === "string") {
          for (const match of args.patchText.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm)) inside(match[1], true)
        }
      },
    }
  },
}
