# Responsive waiting and quick handoff (0.3)

The active Codex main task waits through ordinary bounded tool calls. The Stop hook checks once for an already-terminal armed round; it never holds the conversation waiting for Pi. This replaces 0.2's multi-hour synchronous Stop design, which prevented a received user follow-up from being processed in the desktop incident on 2026-09-26.

## Wait in the active conversation

Use the exact saved repository/task/round. Do independent work first; if blocked on Pi, call:

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py wait --repo /absolute/repo --task TASK-1 --timeout-ms 60000
```

Each call returns within its bounded window. Process user steering between calls, answer progress questions with `status`, and continue waiting only while authorized. Do not chain an infinite wait loop inside one tool invocation. When Pi finishes, collect `result`, inspect relevant evidence and review. A tool result does not need a fabricated hook acknowledgement.

Normal local status reads do not call a model, but each resumed Codex tool cycle does use model context. This design trades some bounded coordination cost for responsiveness. It does not promise zero-token supervision, idle wake-up or offline continuation.

## Optional quick Stop safety check

An authorized exact round may be armed for the current Codex task (`CODEX_THREAD_ID`):

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_handoff.py arm --repo /absolute/repo --task TASK-1 --round 1
```

Keep the returned event key. If Stop sees a terminal result with released worker ownership, it returns a fixed continuation prompt once. If Pi is still running, it returns promptly without marking the round completed or fabricating an expiry. The armed record alone cannot wake an idle task later. Do not finish the main turn relying on that record to supervise ongoing work.

Collect and acknowledge only an actually delivered handoff:

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py result --repo /absolute/repo --task TASK-1 --round 1
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_handoff.py ack --event-key EVENT_KEY
```

Receipt is not acceptance. Repeated notifications are suppressed; a recorded offer is not proof the host received it. Unknown ownership or a lost supervisor requires diagnosis, not a replacement writer.

## Upgrade a task waiting inside a 0.2 Stop hook

Updating plugin files does not change a process already executing the old hook. From the updated canonical runtime, release the exact binding under its owning Codex task identity:

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_handoff.py release --event-key EVENT_KEY --session-id OWNER_CODEX_TASK_ID
```

The operation changes the binding generation/state so the old hook exits its existing local loop. It does not cancel Pi, delete evidence, steal ownership, acknowledge unseen results or change the model. Use an explicit other task id only when the user authorized migrating that task. Verify the old wait exited and a new main-task response appears; sending a message alone is not verification.

Read the current skill from canonical source in an already-open task, then use canonical `status`/bounded `wait`. Do not replace active frozen helper files or restart an implementation merely for this update. Newly started tasks get the new check markers; old tasks retain partial legacy progress visibility until a later task/helper snapshot.

## Interruption, installation and validation

A real user Interrupt suspends automatic handoff without cancelling Pi. Resume only when the user requests continued work. A status question never means cancel implementation. Keep fault cleanup scoped to owned processes and confirm exit before dispatching a repair in the same Pi session.

Refresh the plugin in the application and review/trust the changed hook definitions. Do not edit managed caches or trust records, call Codex CLI, or introduce a heartbeat/MCP/extra model as a workaround. Async hooks do not start a turn in an idle task.

Unit tests can prove quick hook return, ownership, waiting limits and duplicate suppression. Desktop acceptance additionally requires a user follow-up during a genuinely running Pi task to reach the main model, a progress response before Pi completion, and eventual single completion handling. Do not label simulated hook invocations as that host test.
