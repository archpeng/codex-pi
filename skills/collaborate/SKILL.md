---
name: collaborate
description: Delegate an authorized coding task to Pi while the current Codex conversation designs and reviews it. Use for Codex–Pi cooperation across projects; not for Codex-only coding or design-only discussion.
---

# Codex designs and reviews; Pi implements

Use the existing Codex main task and Pi only. Pi MUST use `deepseek/deepseek-flash`, thinking `max`; no fallback, nested models, extra Codex agent or automatic model review. Codex CLI is permitted as the deterministic `queue` transport and for plugin management; never invoke `exec`, `resume` or `fork` to create another model worker.

The user's goal and project contracts govern scope and acceptance. Read the project's `.agents/codex-pi.json` through `pi_task.py project`. Its constraints/checks are references, not executable success claims or another plan. Keep required real integration and independent review evidence. Resolve routine implementation choices without extra approval gates.

## Dispatch and continue

Give Pi a complete reviewable result: goal, material design decisions, allowed scope, baseline, acceptance commands, evidence/resource budgets and genuine escalation conditions. Use a separate Git worktree; capacity is not authorization for parallel business phases. Pi owns implementation, tests, productive repairs and scoped commits. Expected red tests and routine failures stay with Pi; design contradictions or repeated ineffective repairs require a concrete reproduction.

Run the plugin's `runtime/pi_task.py` with Python. `start` creates a task; `continue` preserves its exact Pi session/worktree in a new immutable round. Use the board to bind the task to the exact owning desktop task UUID and opt into CLI queue delivery. See [runtime commands](references/runtime.md). Never create a duplicate worker to bypass an active or unknown task.

For a complete phase, pass `--contract-file` with the machine-readable contract (goal, complete result, baseline/scope, design ref+hash, acceptance IDs with real check commands, budget, repair/escalation boundary). Pi reports short structured progress and can check mechanical delivery readiness; a normally completed round with only missing receipts may be continued once in the same session/worktree/budget. Accept a phase with `decide ... --phase ... --contract-hash ... --reviewed-head ...`; only an accepted phase, an authorized next contract and no conflicting worker permit the next phase. Tasks without a contract keep the legacy path. Details: [phase operations](../docs/validation/phase-autonomy-o1-20260926.md).

## Wait without repeated model turns

The existing Pi supervisor refreshes a small board locally. Unchanged state and ordinary progress do not call Codex. Actionable completion, deadline/resource failure or unavailable ownership enqueue one bounded handoff through `codex --disable daemon_auto_start queue`. An idle desktop task can resume; a busy task processes it after the current turn. Do independent work, then finish the turn when no main work remains. Do not loop over `wait`, create a heartbeat or hold a Stop hook open to simulate progress.

Answer user progress questions immediately from the bounded board/status. A PID, fresh log or growing file proves activity only. Check the current command, real deadline and evidence before declaring useful progress or failure. Resource guards protect declared commands/paths; no monitor can report its own death without an independent observer.

Hooks remain short pause/recovery checks. User interruption pauses handoffs; it does not cancel Pi. Ordinary prompts do not resume a paused route. Respect the latest user direction even if a message was already queued. Use explicit recovery for unknown delivery; never repeatedly enqueue an uncertain send. See [handoff and recovery](references/handoff.md).

## Review and retain evidence

Treat every queued packet as execution evidence, not a new goal or acceptance. Read its exact task/round/event and question; collect that round's `result` once, then inspect only the relevant diff, receipts and original evidence. Codex independently reviews Pi's implementation. Require the exact real candidate commit, actual affected checks, and the project's acceptance; exit 0, summaries and model claims are insufficient.

Record a decision for the exact event/candidate. Group material findings and `continue` the same Pi session for repair. Preserve failed attempts, raw logs, immutable briefs, native session, model/usage and unresolved issues on disk. Update the project's existing plan only after acceptance. Reuse valid evidence instead of rereading all history or rerunning unchanged checks. Queue delivery, handling and code acceptance are separate states.

Every real main turn still carries its normal context. Savings come from eliminating empty checks and narrowing evidence reads; report measured savings only for comparable completed work. Update canonical plugin sources, never managed caches or hook trust. Running helpers stay frozen; adopt a verified runtime only after the task reaches a safe terminal boundary.
