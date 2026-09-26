---
name: collaborate
description: Delegate an authorized coding task to Pi while the current Codex conversation designs and reviews it. Use for Codex–Pi cooperation across projects; not for Codex-only coding or design-only discussion.
---

# Codex decides and reviews; Pi implements

Use the existing Codex main conversation and Pi workers only. Every Pi worker in this mechanism MUST use `deepseek/deepseek-flash`; other models/providers, nested model calls and automatic fallback are forbidden, including investigation and review tasks. Never launch a Codex CLI command, another Codex model/session/subagent, automatic Codex review, or `codex queue`. This applies to installation, recovery, helper scripts and project-specific legacy runners as well as normal execution.

The user's authorized outcome and project contracts govern the work. This plugin transports tasks and evidence; it does not own the project plan, determine product acceptance, or add approval requirements. Keep necessary business, database and interaction checks. Read the project's `.agents/codex-pi.json` with the `project` command; its constraints and checks are references, not automatic execution or a second plan.

## Delegate a reviewable result

Codex establishes the intended behavior, material design decisions, allowed scope, baseline and observable acceptance. Investigate decisive premises; Pi can perform a bounded read-only investigation when useful. Give Pi a whole independently reviewable result with relevant file/contract references. Do not pre-investigate every function or split routine repairs into repeated dispatches.

Pi owns implementation, meaningful tests, integration, productive repairs and scoped local commits. Ordinary compile errors, expected red tests and same-scope failures do not require permission. Design contradictions, changed authority/scope or repeated ineffective repairs come back with a reproduction, attempted fixes and evidence. Do not reduce acceptance to make a check pass.

Use a separate Git worktree for implementation. Resolve shared resources and project-specific parallel eligibility before dispatch; the runtime's capacity limit is not permission to run any two phases concurrently. A task reserves its worktree for its lifetime; new tasks use new worktrees. A worktree and a tool allowlist are not a security sandbox.

## Use scripts without model supervision loops

Use Python to run the bundled `runtime/pi_task.py`; resolve it relative to this plugin, or use the canonical local source `/Users/jlpeng/plugins/codex-pi/runtime/pi_task.py`. This is an ordinary shell command, not MCP. See the command examples in [runtime guidance](references/runtime.md).

1. `project --repo <repo>` reads the project settings. The runtime permits only `deepseek/deepseek-flash`; the project profiles use thinking `max`. Do not change the model to work around errors.
2. `start --repo <repo> --task <id> --worktree <worktree> --prompt-file <brief>` (optionally `--read-only`) starts Pi and returns its exact identity and evidence paths. Include result, scope, decisions, meaningful checks and genuine stop conditions in the prompt. Let Pi work continuously.
3. Do independent authorized work. If actually blocked on Pi, use `wait` for a bounded wait. Software does the waiting; don't loop status/tail/sleep/write_stdin calls merely to keep the model awake. A wait timeout leaves Pi running.
4. Read `result` at delivery or for an actual user status request/recovery. Start with its compact state and evidence references. Inspect original logs only for a material question. A finished process, exit 0, model claim, summary or command response is not acceptance.
5. Codex reviews the delivered candidate and original evidence. Where project rules require an independent reviewer, the Codex main reviewer must not be the Pi implementer. Do not spawn another Codex agent. Group material findings and use `continue` for a whole repair, preserving the exact Pi session. Confirm affected behavior after repairs; reuse still-valid evidence. If Codex itself implemented the candidate, use a distinct read-only Pi review task when independent review is required, then retain final judgment in the main conversation.

The local scripts do not provide automatic wake-up after the Codex turn ends. **Do not claim automatic continuation or silently replace it with a heartbeat, Codex CLI, or extra model.** A pending tool can return a result while the turn remains active; otherwise retain task identity and report that a later main-conversation action must collect the result. Use `cancel` only when cancellation is intended, never just because a wait ended.

## Evidence and continuity

Keep native Pi session, immutable briefs/rounds, full output, exact command receipts, model/usage, candidate revision and unresolved failures on disk. Use the bundled check helper for actual acceptance commands; it records real exit and log hashes, not PASS. Missing counters or exits remain unknown, skip/no-match is not success, and retries do not erase failures. Keep project plan updates in its existing authority after real acceptance.

Main context retains current result, boundaries, revisions, unresolved questions and evidence paths. Avoid rereading all history or repeating the full diff/test run when valid evidence suffices. Context compression and output limits are project/client choices, not a universal fixed threshold. Token savings are unmeasured until comparable completed phases show them; output reasoning is not counted twice.

For tools, configuration and recovery details read [runtime guidance](references/runtime.md) only when needed. Legacy project runners that invoke Codex CLI are not an allowed fallback.
