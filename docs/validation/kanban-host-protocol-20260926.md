# Kanban gate: desktop verification protocol

Status: superseded historical experiment, NOT an operational guide. The desktop gate was not validated and is not shipped. The accepted design uses the existing Pi supervisor and supported CLI queue; see [current design](../kanban-coordination-proposal.md). Existing business automation `pi-harness` remains paused. The protocol below is retained only as investigation history.

## Scope and identity

- Main design/reviewer task: `01a0da81-3b76-7fd0-aac2-0169cb6de7a1`.
- Implementation: same Pi session/task `HEARTBEAT-HEALTH-20260926`, round 2, `deepseek/deepseek-flash`, thinking max, worktree `/private/tmp/codex-pi-heartbeat-health-20260926`.
- No Codex CLI, external model, new daemon, private application IPC, managed cache edit or manual trust-store edit.
- Test only a temporary opt-in board and native heartbeat bound to this main task. No business worker start/cancel, no business acceptance or automatic continuation during this probe.
- New plugin source/hook definitions must be installed/reloaded and reviewed/trusted through the app before host tests. Current callable tools cannot perform that trust action. Prepare a concrete reviewed candidate first.

## Verified prerequisites (read-only)

Official hooks documentation: <https://learn.chatgpt.com/docs/hooks>. UserPromptSubmit supports blocking the incoming prompt; background hooks do not start a new idle turn. Neither statement proves automatic heartbeat gate behavior in this installed app.

Local app bundle inspected read-only: `/Applications/ChatGPT.app/Contents/Resources/app.asar`, member `.vite/build/main-C-Mhak1n.js`, SHA-256 `457c79be69620d4489e94c14ac665f81731d869635606dcf175b7b4cc2e8b467`.

The inspected scheduler checks renderer eligibility and thread activity before dispatch. An active unfinished turn, waiting-on-input/approval flags or uncertain active history defers a scheduled heartbeat. Scheduled dispatch calls normal turn start with `turnTrigger = automation_heartbeat_scheduled`. This property is not documented as a hook input; do not assume it reaches hook JSON. The scheduled completion handler treats a turn status other than `completed` as a failed heartbeat. A stopped hook therefore needs a real completion/status test as well as a token test.

The actual prior native message envelope contains `heartbeat`, `automation_id`, `current_time_iso`, `instructions`. Only a registered exact owner/id/nonce/instruction envelope may be filtered. This is conservative message matching, not a cryptographically authenticated origin. Normal user text and ambiguous envelopes pass through.

## Experiment sequence

1. Review candidate, run meaningful local behavior checks and validate plugin package. Inspect exact hook diff and make source update available; app trust is a separate prerequisite.
2. Register one temporary board/automation to this owner, initially paused. Capture installed version, hook trust state read-only, board revision and event state, hook audit offset, transcript byte offset and cumulative token counters. Do not expose full prompts or credentials in reports.
3. Enable the native probe only once trusted. Finish the current turn so the app can actually schedule; an actively running main turn prevents a valid idle scheduling experiment. Do not pretend a manual hook invocation is a scheduled heartbeat.
4. No-event probe: require real scheduled hook audit, no model requests/token increase, short completed turn, no repeated failure/user notification. Duplicate token telemetry with unchanged cumulative counters is not new usage. Measure more than one no-event tick to exclude one-shot behavior.
5. Publish one synthetic actionable card through the public helper (explicit test evidence). Require a subsequent scheduled request to resume this exact task, provide only relevant card/event context, make one explicit test decision, and record the exact event/candidate. Test records are never business acceptance.
6. Duplicate and interruption: republish the same event, verify no new pending event; read/deliver alone never handles it. An interrupted unhandled event remains recoverable. Explicit pause blocks automatic continuation and does not cancel Pi.
7. Exercise failure/timeout/resource-limit events using tiny local owned process doubles. Verify correct state, bounded evidence, no child survivors, dedup, and dispatch behavior. Do not allocate GB or kill unrelated processes.
8. Responsiveness: while local checks are running and while scheduled ticks compete, ordinary user prompts must start/complete normally. Check lock contention and malformed/missing board cases; normal prompt path does not wait on board I/O. App traces and observed response duration are required; subprocess latency alone proves only local behavior. Do not fabricate a human-interaction result if the user has not sent a prompt.
9. Stop/pause the probe at its verification end or first material host failure. Leave business tasks unchanged until full required integration behavior is established. If no-event gate is model-free but produces failed automations, do not make it default or hide the failure with notification settings.

## Results to retain

For each probe retain: installed source/version, hook event/input key names (not full user prompt), timestamp, gate outcome/latency, task/turn identity, relevant event identity, application turn status, model-request count, uncached input, cached input, output, and a bounded raw-evidence reference. Track stdout/control behavior separately from an actual user-facing notification.

Saving a small card does not replace the active conversation context. Changed-event turns still incur the task's effective history; model-free unchanged ticks and fewer evidence round trips are the first savings to prove. No claimed percentage saving before comparable measurements.
