# Synchronous completion handoff

This is an opt-in bridge at the host's stopping boundary. It does not launch a Codex executable or call a model. The existing Codex task is continued by the host after a synchronous `Stop` hook returns `decision: "block"` with a fixed reason. The application must load and trust the current plugin hooks first. Installing the skill alone is insufficient.

## Arm only the current task's authorized work

Start Pi normally, preserving the returned repository, task and exact round. Register that round with `runtime/pi_handoff.py arm`. The current Codex shell's `CODEX_THREAD_ID` supplies the session identity; outside that environment an explicit session id is required. Do not infer a session from a working directory or another task's transcript. A task launched before the hook update can be registered without restarting Pi.

The plugin's shared continuation state is separate from immutable worker evidence. Projects do not need their own hook scripts or another copy of the project plan. Registrations point to exact results and never change the worker's model, session, worktree or acceptance criteria.

Use the returned round, not an assumed latest round. In the current Codex shell:

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_handoff.py arm --repo /absolute/repo --task TASK-1 --round 1
```

Keep the returned `eventKey`. Inspect state without delivering or accepting anything:

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_handoff.py status --event-key EVENT_KEY
```

The `Stop` handler waits locally for at most the registered window (default and maximum four hours). Its host timeout is slightly longer so it can save a recovery result before the host times it out. This is a configured upper bound, not evidence that the desktop application survives every sleep, restart or connection loss for four hours. The worker's timeout remains an independent limit. No repeated model status requests are needed while the hook is waiting.

When a terminal worker result arrives, the hook emits a compact reason containing validated identifiers and exact result/acknowledgement commands. Pi-generated narrative is not injected into the continuation prompt. Execution success, failure, cancellation and worker timeout require review; none is product acceptance.

## Receipt and another round

The main conversation reads the exact result and original evidence needed for review, then acknowledges the delivered handoff. Acknowledgement is receipt only. Group any repair findings, use `pi_task.py continue`, and arm the newly returned round. Never automatically replay implementation because a notification or acknowledgement is missing.

The continuation includes the exact commands. Their general shape is:

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_task.py result --repo /absolute/repo --task TASK-1 --round 1
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_handoff.py ack --event-key EVENT_KEY
```

Acknowledgement belongs to the bound Codex session; another session must not consume it. A new Pi round gets a new registration. Ordinary repeated registration must not undo a user interruption.

After inspecting an interrupted, expired or uncertain registration and resolving its cause, explicitly resume that exact round:

```sh
python3 /Users/jlpeng/plugins/codex-pi/runtime/pi_handoff.py arm --repo /absolute/repo --task TASK-1 --round 1 --resume
```

This still uses the current `CODEX_THREAD_ID`. Resume does not reset a delivered or acknowledged notification. Acknowledgement is allowed only after an offer was recorded, and requires the owning session even when the event key is known.

Repeated or concurrent hooks do not offer the same round twice. If the process dies after recording an offer but before the host receives it, the unacknowledged record remains visible for recovery; there is no claim of exactly-once host delivery.

## Interruption and recovery

- A user `Interrupt` suspends this session's outstanding handoffs promptly. It does not cancel Pi, resume the user-stopped conversation, or silently re-arm the wait.
- Hook waiting expiry leaves Pi running. Preserve the exact task and round, inspect the handoff status and worker result on the next authorized action, and explicitly resume only when intended.
- A worker's own timeout is a terminal failure to collect; it is distinct from the hook's waiting window expiring. Neither is successful acceptance.
- An orphaned worker, uncertain ownership, invalid record or unavailable result is not a completed task. Recover from the recorded identity and evidence instead of dispatching a duplicate.
- An application/session shutdown cannot be followed by an unsolicited wake from these scripts. Recovery hooks can inform the next naturally occurring turn; they do not start that turn.

Do not add a heartbeat, Codex CLI process or extra model as an implicit fallback.

## Host trust and validation

Update the personal `codex-pi` plugin from its marketplace entry, review the bundled hook commands, and trust the current definitions in the application's hook review interface. Do not edit managed plugin caches or synthesize trust records. A plugin update may require a fresh review.

Subprocess tests exercise the real handler and worker supervisor with controlled Pi processes. A separate real DeepSeek task checks the worker boundary. Neither proves that the desktop loaded, trusted and continued the conversation. Record a desktop continuation only after an actual armed round has returned through the host, preserving its handoff identity and subsequent acknowledgement.

Official contracts: [Stop continuation](https://learn.chatgpt.com/docs/hooks#stop), [Interrupt](https://learn.chatgpt.com/docs/hooks#interrupt), [plugin hook packaging and trust](https://developers.openai.com/plugins/build/plugins).
