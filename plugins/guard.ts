import { existsSync, realpathSync } from "node:fs"
import { dirname, resolve, sep } from "node:path"

// Exact commands only. The gateway owns argv, timeouts, logs and device scope.
const permitted = /^(?:ls|rtk ls|git status --short|rtk git status --short|harness-tool (?:status|build|test|logs|screenshot|checkpoint|commit)(?: [A-Za-z0-9_.:-]{1,2048})?)$/
const root = realpathSync(process.cwd())
function selectedPath(value: unknown, writing = false) {
  if (typeof value !== "string") return
  const target = resolve(root, value)
  if (target !== root && !target.startsWith(root + sep)) throw new Error("Outside selected workspace")
  const parts = target.slice(root.length).split(sep)
  if (parts.includes(".git") || (writing && parts.includes(".harness"))) throw new Error("Harness or Git control path is read-only")
  let parent = target
  while (!existsSync(parent)) parent = dirname(parent)
  const actual = realpathSync(parent)
  if (actual !== root && !actual.startsWith(root + sep)) throw new Error("Source symlink escapes selected workspace")
}
export default {
  id: "strata-command-boundary",
  async server() {
    return {
      "tool.execute.before": (input: any, output: any) => {
        const tool = String(input?.tool ?? "").toLowerCase()
        const args = output?.args ?? {}
        if (tool === "bash" || tool === "shell") {
          if (typeof args.command !== "string" || !permitted.test(args.command)) {
            throw new Error("Command outside selected harness grammar; use harness-tool operations")
          }
          selectedPath(args.workdir)
          if (args.command.startsWith('harness-tool test')) args.timeout = 1810000
          else if (args.command.startsWith('harness-tool build')) args.timeout = 920000
        }
        if (["read", "edit", "write", "glob", "grep", "list"].includes(tool)) {
          selectedPath(args.filePath ?? args.path, tool === "edit" || tool === "write")
        }
        if (tool === "apply_patch" && typeof args.patchText === "string") {
          for (const match of args.patchText.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm)) selectedPath(match[1], true)
        }
      },
    }
  },
}
