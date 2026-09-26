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

## Keep the main conversation responsive

Use Python to run the bundled `runtime/pi_task.py`; resolve it relative to this plugin, or use the canonical local source `/Users/jlpeng/plugins/codex-pi/runtime/pi_task.py`. This is an ordinary shell command, not MCP. See the command examples in [runtime guidance](references/runtime.md).

1. `project --repo <repo>` reads the project settings. The runtime permits only `deepseek/deepseek-flash`; the project profiles use thinking `max`. Do not change the model to work around errors.
2. `start --repo <repo> --task <id> --worktree <worktree> --prompt-file <brief>` (optionally `--read-only`) starts Pi and returns its exact identity and evidence paths. Include result, scope, decisions, meaningful checks and genuine stop conditions in the prompt. Let Pi work continuously.
3. Do independent authorized work. When waiting is necessary, use an ordinary `pi_task.py wait` tool call for at most 60 seconds. Keep each tool invocation bounded; do not hide an unbounded loop inside a shell command or orchestrator cell. Between calls, process new user input before another wait. Repeat bounded waits only while continuing the authorized task. Answer status questions from `status` immediately; don't postpone the answer until Pi finishes. Waiting does not cancel Pi. Normal unchanged status needs no repeated narration or transcript reads; communicate meaningful progress and errors, and maintain the host's required update cadence.
4. Use cheap `status` for progress/recovery and collect `result` once when reviewing a delivered round. Inspect original logs for a material question. A live process or recently written log is not proof of useful progress. Check failures and deadline expiry need diagnosis: distinguish expected red tests and productive repairs from unchanged failing reruns. A finished process, exit 0, model claim, summary or command response is not acceptance.
5. Codex reviews the delivered candidate and original evidence. Where project rules require an independent reviewer, the Codex main reviewer must not be the Pi implementer. Do not spawn another Codex agent. Group material findings and use `continue` for a whole repair, preserving the exact Pi session. Confirm affected behavior after repairs; reuse still-valid evidence. If Codex itself implemented the candidate, use a distinct read-only Pi review task when independent review is required, then retain final judgment in the main conversation.

The trusted `Stop` hook is a quick safety check for an already-finished armed round; it never waits for running Pi. It cannot wake an idle task when Pi finishes later. Ordinary bounded tool waiting keeps the active main task able to collect completion. If an armed hook actually delivers an event, collect the exact result and acknowledge it; direct tool results do not invent hook delivery or permit early ack. User interruption suspends handoffs without cancelling Pi; respect that intent. Unknown ownership, missing evidence or unacknowledged delivery needs explicit recovery, never an automatic duplicate worker. **Do not replace this with a heartbeat, Codex CLI, or extra model.** Use `cancel` only when cancellation is intended. For migration from 0.2's long Stop waits, read [handoff guidance](references/handoff.md).

## Evidence and continuity

Keep native Pi session, immutable briefs/rounds, full output, exact command receipts, model/usage, candidate revision and unresolved failures on disk. Use the bundled check helper for actual acceptance commands; it records real exit and log hashes, not PASS. Missing counters or exits remain unknown, skip/no-match is not success, and retries do not erase failures. Keep project plan updates in its existing authority after real acceptance.

Main context retains current result, boundaries, revisions, unresolved questions and evidence paths. Avoid rereading all history or repeating the full diff/test run when valid evidence suffices. Context compression and output limits are project/client choices, not a universal fixed threshold. Token savings are unmeasured until comparable completed phases show them; output reasoning is not counted twice.

For tools, configuration and recovery details read [runtime guidance](references/runtime.md) only when needed. Legacy project runners that invoke Codex CLI are not an allowed fallback.
