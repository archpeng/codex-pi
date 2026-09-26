# Local runtime

The plugin provides a skill and deterministic Python scripts; no MCP server or additional Node packages are required. Only Pi calls a model; no Codex executable, API, reviewer or notification process is started. It reuses the user's installed Pi and provider authentication without copying credentials into the plugin.

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

`wait` waits in software for at most 60 seconds. A timeout returns a still-running state without interrupting Pi. Do not loop to simulate automatic wake-up. Exiting the launch command does not kill its detached worker; use the saved identity from a later main-conversation call. Execution timeout is independent and terminates the owned process group. A missing supervisor is not proof of completion; uncertain state stays unknown.

Cancellation applies to this runtime's owned worker, not legacy project Pi processes. No automatic state import, lock stealing, session guessing, cleanup, new phase launch, review or commit acceptance occurs.

The local source is `/Users/jlpeng/plugins/codex-pi`. Changes are made there, not in Codex's managed plugin cache. Each active run keeps its runtime identity/snapshot. Validate updated sources and let existing runs finish before switching future runs. Do not use Codex CLI commands to install or refresh this plugin.

## Commands

Use the installed plugin runtime path (on this computer the canonical source is shown below).
Write the complete brief to a file, then pass its path; do not interpolate untrusted text into shell code.

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py project --repo /absolute/repo
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py start --repo /absolute/repo --task TASK-1 --worktree /absolute/worktree --prompt-file /absolute/brief.md
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py wait --repo /absolute/repo --task TASK-1 --timeout-ms 60000
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
