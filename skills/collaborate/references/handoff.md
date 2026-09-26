# Event handoff and recovery

The existing Pi supervisor refreshes the shared board about every 15 seconds and dispatches only actionable events. The board describes the goal, phase, round, candidate commit, command/check evidence and pending question. Queue delivery, event handling and code acceptance are separate facts. Ordinary progress does not wake the main model. An idle desktop task resumes automatically; a busy task handles the queued message after its current turn. Already queued messages cannot be treated as permission to override a later pause.

## Handle one event

Read its exact task, round, event ID and question. Reuse a result already collected for that round. Verify the relevant diff, exact candidate and original check receipts, then decide:

```sh
python3 /absolute/plugin/runtime/pi_board.py decide --repo /absolute/repo --task TASK-1 --event-id EVENT_ID --decision accept --reviewed-head FULL_COMMIT_SHA --note 'Verified checks and review evidence'
```

Use `changes_requested` with concrete findings when repairs are needed; `reject` rejects the candidate and `resolve` handles a non-review incident. Acceptance requires the full real immutable candidate commit and project evidence. Decisions on old rounds never accept the current round. If a packet reports overflow, read remaining pending board events and handle them in this same main turn; a terminal supervisor will not supply another periodic tick. A repeated delivered event is a receipt to deduplicate, not a reason to rerun Pi or checks.

Group material findings and continue the same Pi session. Productive red tests and routine repairs remain with Pi. Investigate the first relevant error before repeating an unchanged failure. A resource breach or deadline should produce a bounded question and evidence, not an infinite retry or automatic acceptance.

## Pause and uncertain delivery

```sh
python3 /absolute/plugin/runtime/pi_board.py pause --repo /absolute/repo --task TASK-1 --note 'User paused handoff'
python3 /absolute/plugin/runtime/pi_board.py resume --repo /absolute/repo --task TASK-1 --thread OWNER_UUID
python3 /absolute/plugin/runtime/pi_board.py recover --thread OWNER_UUID
python3 /absolute/plugin/runtime/pi_board.py rearm --repo /absolute/repo --task TASK-1 --event-id EVENT_ID
```

User Interrupt pauses the owner's handoff route; it does not cancel Pi. A progress question does not resume a pause. Resume only when the user authorizes continuation. `pi_task.py cancel` separately cancels the owned worker; verify it has ended before starting repair.

Confirmed queue delivery is not retried merely because review is unfinished. A timeout or ambiguous crash may have delivered already: retain the uncertain claim, inspect evidence and use explicit recovery only when needed. `rearm` may duplicate a previously uncertain delivery because CLI queue has no caller idempotency key. Never rearm a live inflight send, steal locks, clear decisions or claim exactly-once delivery. Failures are visible through board/recovery evidence; no second model repairs transport.

Hooks stay short and serve pause/recovery. CLI queue tasks suppress overlapping legacy Stop delivery. A supervisor cannot report its own death without another observer: after a process/host failure, recovery occurs on the next normal interaction. There is no universal two-minute discovery guarantee. Declared command deadlines and resource budgets provide bounded detection while their supervisor is alive; desktop response can additionally wait for its active turn to finish.

## Adopt an update safely

Update the canonical plugin and install through the formal plugin mechanism. Never modify managed caches, hook trust or private app IPC/queue databases. If the app requests Hook review/trust, the user does that in the app. A plugin update does not rewrite a running worker's helpers.

At a verified terminal boundary, with no worker/supervisor ownership, run the accepted runtime:

```sh
python3 /absolute/new-plugin/runtime/pi_task.py upgrade --repo /absolute/repo --task TASK-1
python3 /absolute/new-plugin/runtime/pi_board.py register --repo /absolute/repo --task TASK-1 --thread OWNER_UUID --transport cli-queue --title 'Task title' --goal 'Authorized result'
```

Verify frozen helper hashes/version and unchanged Pi session/worktree before `continue`. Registering a terminal task can enqueue a review event immediately; respect already-completed review evidence rather than duplicating work. Keep legacy business workers intact until their own safe boundary. For an old 0.2 long Stop hook, the explicit `pi_handoff.py release --event-key EVENT_KEY --session-id OWNER_UUID` invalidates that exact binding without cancelling Pi; verify the old hook exited.

## Fast acceptance

Use one real tiny Pi task: write fixed bytes to one file, run a deterministic check with a ten-second timeout, commit only that file, and exit. Bound the whole round to three minutes. Test completion → own supervisor → same desktop task → visible main reply. A 15-second fixture passed this path on 2026-09-26. Use isolated short child processes for failure, cancellation, timeout and duplicate/race tests; they need no additional model calls. Transport tests and green unit tests do not waive independent code review.
