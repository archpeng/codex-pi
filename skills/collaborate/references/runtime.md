# Local runtime

The plugin provides a skill and deterministic Python scripts; no MCP server or additional Node packages are required. The worker runner only calls Pi; it starts no Codex executable, API client or reviewer. An opted-in, trusted synchronous host hook can return a continuation reason to the existing Codex task. It reuses the user's installed Pi and provider authentication without copying credentials into the plugin.

The main conversation supplies a Git repository, a task identifier and an isolated worktree. Create worktrees with ordinary Git. `start` is a new task; `continue` is another immutable round using that task's saved session. A pending or unknown task must be inspected, not replayed with a new identity to bypass its lock.

Each project has `.agents/codex-pi.json`:

```json
{
  "schemaVersion": 1,
  "model": "deepseek/deepseek-flash",
  "thinking": "max",
  "constraints": ["AGENTS.md"],
  "checks": ["The affected behavior tests and this project's required checks"],
  "maxWorkers": 1,
  "timeoutSeconds": 14400
}
```

Constraints are existing project documents. Checks are guidance for choosing and performing the actual acceptance, not trusted success declarations or shell hooks. Shared database identity, phase dependencies and permission boundaries remain project decisions. The only allowed model is `deepseek/deepseek-flash`. Any other project model or frozen task model is rejected before launching Pi; there is no automatic fallback.

Execution facts are stored below the repository's Git common directory, under `codex-pi/tasks/`. They do not replace PLAN, contracts or owner facts. Raw stdout/JSONL, stderr, the brief and receipts remain local. The result is bounded; follow its pointers for actual evidence. Keep these artifacts private just like source and build logs; the plugin makes no external publication.

`status` reads a bounded progress snapshot without generating a transcript summary. `wait` waits in software for at most 60 seconds and returns current state; timeout leaves Pi running. Use separate ordinary tool calls while actively waiting, processing user input between them. Do not bury repeated waits in one long tool call, end the main turn expecting an idle wake, or read full logs on every interval. Execution timeout is independent. A missing supervisor is not proof of completion; uncertain state stays unknown.

Cancellation applies to this runtime's owned worker, not legacy project Pi processes. No automatic state import, lock stealing, session guessing, cleanup, new phase launch, review or commit acceptance occurs.

The local source is `/Users/jlpeng/plugins/codex-pi`. Changes are made there, not in Codex's managed plugin cache. Each active run keeps its runtime identity/snapshot. Validate updated sources and let existing runs finish before switching future runs. Do not use Codex CLI commands to install or refresh this plugin.

## Commands

Use the installed plugin runtime path (on this computer the canonical source is shown below).
Write the complete brief to a file, then pass its path; do not interpolate untrusted text into shell code.

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py project --repo /absolute/repo
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py start --repo /absolute/repo --task TASK-1 --worktree /absolute/worktree --prompt-file /absolute/brief.md
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py wait --repo /absolute/repo --task TASK-1 --timeout-ms 60000
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py status --repo /absolute/repo --task TASK-1
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py result --repo /absolute/repo --task TASK-1
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py continue --repo /absolute/repo --task TASK-1 --prompt-file /absolute/repair.md
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py cancel --repo /absolute/repo --task TASK-1
```

`start --read-only` limits Pi to read/grep/find/ls. A worktree is reserved for the task lifetime; use a fresh worktree for a new task. `PI_BIN` can select the installed Pi executable when it is absent from PATH. On this machine it is `/Users/jlpeng/.nvm/versions/node/v24.8.0/bin/pi`. Python uses only its standard library.

For real acceptance commands, use the task's frozen check helper and the selected round's receipt directory from `result.evidence` (`toolsDir` and `checksDir`). Run from the task worktree:

```sh
python3 /absolute/task/tools/pi_check.py --output-dir /absolute/round/round.checks --id affected-tests --timeout-seconds 600 -- make test
```

Replace `make test` with the project's actual command. Each attempt writes its own receipt and log; rerunning does not erase a failure. The generated Pi brief already includes these exact task-specific helper paths.

New check attempts expose a running marker before finishing and preserve the final immutable receipt and raw log. A legacy unreceipted log is only a possible current check, not verified activity. Quiet output alone is not a failure. Diagnose the first relevant error and actual check deadline; never keep rerunning an unchanged failure or change acceptance to make it pass. Project prechecks and nested command timeouts remain project-owned.

For quick completion handoff, migration and trust requirements, read [handoff guidance](handoff.md). Canonical `status` and `wait` can inspect old task evidence without replacing a running worker's frozen helpers. The richer check marker starts with new helper snapshots. Existing task evidence and Pi session remain reusable.
