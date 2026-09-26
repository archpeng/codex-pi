# Local runtime commands

Python scripts call the existing Pi CLI. Pi is pinned to `deepseek/deepseek-flash`, thinking `max`; any other model is rejected. The optional Codex `queue` transport delivers to the existing desktop task without invoking a second model. Authentication stays in the user's existing local configuration. No extra MCP, heartbeat or service is installed.

Use an accepted plugin runtime for new operations. Replace the example paths and exact owner UUID before executing; do not interpolate user text into shell commands.

```sh
python3 /absolute/plugin/runtime/pi_task.py project --repo /absolute/repo
python3 /absolute/plugin/runtime/pi_task.py start --repo /absolute/repo --task TASK-1 --worktree /absolute/worktree --prompt-file /absolute/brief.md
python3 /absolute/plugin/runtime/pi_board.py register --repo /absolute/repo --task TASK-1 --thread OWNER_UUID --transport cli-queue --title 'Task title' --goal 'Reviewable result'
```

Start and immediately register; registration also catches a task that already finished. Registration defaults to `offline` unless `cli-queue` is explicit. The transport requires a local Codex CLI supporting `queue`, the desktop application, and its exact existing task UUID. `--codex-bin /absolute/codex` chooses a specific executable. Never guess an owner, invoke `exec/resume/fork` or launch an app-server to deliver a message. A task started by old helpers must adopt the accepted runtime at a terminal boundary before its supervisor can provide new notifications.

Project configuration lives in `.agents/codex-pi.json`: schemaVersion 1, pinned model/thinking, existing constraint paths, acceptance command guidance, maxWorkers and timeoutSeconds. It is not another PLAN or proof of acceptance. Use an isolated worktree; a task reserves its worktree for its lifetime. `start --read-only` limits Pi tools. `PI_BIN` may select the installed Pi executable.

## One read when needed

```sh
python3 /absolute/plugin/runtime/pi_board.py show --repo /absolute/repo --task TASK-1
python3 /absolute/plugin/runtime/pi_task.py status --repo /absolute/repo --task TASK-1 --round 1
python3 /absolute/plugin/runtime/pi_task.py result --repo /absolute/repo --task TASK-1 --round 1
python3 /absolute/plugin/runtime/pi_task.py continue --repo /absolute/repo --task TASK-1 --prompt-file /absolute/repair.md
```

Normal supervision stays inside the detached Pi supervisor. Do independent work, then end the main turn when blocked on Pi. Do not build main-model polling loops around `wait`; the low-level bounded wait command is only a diagnostic. Answer a user's progress question from one compact snapshot. Observe actual command/deadline/receipt evidence, not PID or output growth alone.

`result` is collected once per terminal round. It retains raw pointers and bounded summaries, all attempts, model usage and native Pi session evidence below the Git common directory `codex-pi/tasks/`. Open only the relevant receipt/log/diff. Continue the exact saved task for repairs; do not replace a live or unknown worker with a new identity. A reused session does not keep idle Pi processes alive after the round ends.

## Phase contracts (0.5: accepted O1, verified O2)

An authorized complete phase may be dispatched with a frozen contract: give `start`/`continue` a
`--contract-file` JSON (schema in `docs/validation/phase-autonomy-o1-20260926.md`). The contract
freezes goal, complete result, baseline, scope, design ref/hash, acceptance item IDs with real check
commands, the phase budget and the autonomous-repair/escalation boundary. The brief references the
contract; the project PLAN/design remains authoritative and no parallel plan is created.

```sh
python3 /absolute/plugin/runtime/pi_task.py progress --repo WT --task TASK --round N \
    --activity implementing --step '...' --completed-criteria ITEM --next '...' --evidence-ref PATH
python3 /absolute/plugin/runtime/pi_task.py readiness --repo REPO --task TASK --round N
python3 /absolute/plugin/runtime/pi_task.py phase-status --repo REPO --task TASK
python3 /absolute/plugin/runtime/pi_board.py decide --repo REPO --task TASK --event-id EVENT \
    --decision accept --reviewed-head FULL_SHA --phase PHASE --contract-hash HASH
```

Ordinary progress is self-report only, never a check receipt and never a queue message. Readiness
compares the exact contract with real `pi_check` receipts bound to the candidate; missing, failed,
skipped or unknown evidence is never ready (scope coverage is complete with a bounded cap, and
`forbidSkip`/`minRun` need parseable counts). Running self-repairable check failures/timeouts stay
local. A normally completed round with only mechanically missing evidence may be continued once in
the same session/worktree/budget (quota persisted per phase); the second shortfall, a real design
question or an unknown start escalates. A phase also emits at most two bounded non-blocking
progress echoes (default 10 minutes apart, persisted per `phaseId`) for verified receipts or
evidence-bearing check/repair milestones; ordinary progress, repeated writes and "still alive"
never queue. A recorded user pause blocks progress echoes, auto-continuation and explicit
`continue` until an explicit resume. `accept` binds phase, contract and candidate; a different
phase requires the previous one accepted on the board and no conflicting worker. One normalized
phase evidence snapshot (candidate known/unknown, per-item grades, scope, execution, readiness) is
built by `pi_task` and consumed by the board, progress events, `readiness` and the accept gate;
there is no second candidate or receipt-validity inference. A phase acceptance receipt must present
the item's declared command and a wrapper deadline within the contract's `commandTimeoutSeconds`;
a known command or timeout mismatch can never cover the item, missing or contradictory
identity/timing stays unknown, and every consumer uses that one verdict. The generated phase brief
uses that per-command cap while a legacy task keeps the project round-timeout example; the
whole-round supervisor timeout is a separate limit. `accept` re-reads the live status and
refuses when the round, contract revision, candidate, readiness, worktree HEAD or writer-free state
no longer match the stored event. Tasks without a
contract keep the legacy review path. O1 is accepted at candidate `7331da9`; O2 ran a real two-round
fixture (R1 `changes_requested`, same-session R2 accepted) documented in
[o2-eventfold-20260926.md](../../docs/validation/o2-eventfold-20260926.md); formal O3 installation and
business-task migration are still pending. See
[docs/validation/phase-autonomy-o1-20260926.md](../../docs/validation/phase-autonomy-o1-20260926.md)
for operations and the current verification boundary.

## Checks and resource protection

The generated brief contains task-specific frozen `toolsDir` and round-specific `checksDir`. Run from the Pi worktree:

```sh
python3 /absolute/task/tools/pi_check.py --output-dir /absolute/round/round.checks --id affected-tests --timeout-seconds 600 -- make test
python3 /absolute/task/tools/pi_check.py --output-dir /absolute/round/round.checks --id evidence-test --timeout-seconds 180 --watch-path /absolute/evidence --max-bytes 104857600 --health-interval-seconds 15 -- python3 real_check.py
python3 /absolute/task/tools/pi_copy.py /absolute/source /absolute/new-destination --max-bytes 104857600
```

Replace example commands and limits with meaningful project budgets. A running marker reports wrapper start, actual deadline, child identity and optional directory guard. Final immutable receipts bind command, revision, exit and log hash. Failed, skipped, interrupted, unknown or zero-test attempts never become a pass. The command timeout and whole Pi round timeout are separate. For a phase task the per-command `--timeout-seconds` must not exceed the contract's `commandTimeoutSeconds`; a receipt outside that bound or with a different command can never cover its acceptance item.

Directory guards measure declared regular-file bytes without following symlinks. An observed known breach stops only the owned command group and records the failure; an incomplete measurement stays unknown. The copy helper preserves literal symlinks and refuses existing destinations, recursion, known overages or unknown verification. These rules prevent the 14 MB → 6.5 GB expansion incident without asking the main model to poll.

See [handoff and recovery](handoff.md) for event decisions, uncertain delivery, interruption and safe migration.
