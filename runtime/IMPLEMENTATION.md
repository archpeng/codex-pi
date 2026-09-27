# Codex-Pi runtime implementation

Cross-project persistent Pi worker lifecycle for the Codex main session. Final
architecture: **Codex main → shell → shared Python lifecycle scripts → Pi CLI**.
There is no MCP server, no Node dependency and no package manifest in this
plugin. Python owns admission, persistence, evidence, model policy and process
control; the main session calls the CLI through its shell.

The Pi execution path starts only Pi. The opted-in board transport invokes only `codex --disable daemon_auto_start queue` to notify the existing desktop task; it never starts another Codex model. CLI plugin management is also permitted for installation. Current commands and limits are in [runtime guidance](../skills/collaborate/references/runtime.md) and [handoff guidance](../skills/collaborate/references/handoff.md).

## Entrypoints

```sh
python3 runtime/pi_task.py project  --repo <path-inside-project>
python3 runtime/pi_task.py start    --repo <path> --task <id> --worktree <linked-checkout> --prompt-file <brief>
python3 runtime/pi_task.py continue --repo <path> --task <id> --prompt-file <follow-up>
python3 runtime/pi_task.py result   --repo <path> --task <id> [--round N]
python3 runtime/pi_task.py wait     --repo <path> --task <id> [--round N] [--timeout-ms 60000]
python3 runtime/pi_task.py cancel   --repo <path> --task <id>
python3 runtime/pi_task.py --help
```

Every command prints one JSON object to stdout and exits non-zero with an
actionable stderr message on error. `PI_BIN` overrides the Pi executable (test
double or explicit path only). Helpers used by the Pi worker are
`runtime/pi_summary.py` and `runtime/pi_check.py`.

| File | Role |
| --- | --- |
| `runtime/pi_task.py` | Admission, config, model policy, brief, detached worker, timeout, cancel, result/wait, CLI |
| `runtime/pi_summary.py` | Bounded round summary, check-receipt aggregation, usage, reported-model check |
| `runtime/pi_check.py` | One-check receipt: true exit/signal/timeout, log sha256, HEAD/dirty, counts |
| `runtime/pi_handoff.py` | Short legacy handoff, board-route recovery and interruption |
| `runtime/pi_board.py` | Shared evidence board, event decisions, queue claims and deterministic CLI transport |
| `runtime/pi_takeover.py` | Read-only failure-policy fold from exact review decisions; three rejected deliveries transfer implementation to the existing Codex task |
| `runtime/pi_copy.py` / `pi_size.py` | Bounded evidence copying and byte scans without following symlinks |
| `hooks/hooks.json` | Plugin-discovered synchronous Stop, Interrupt and recovery commands; requires host trust |
| `runtime/VERSION` | Runtime version copied into every task state |

`pi_summary.py` and `pi_check.py` are derived from the already-tested
`eugl-ds-delegated-execution` local helpers. No community server code is used.

## Host continuation

`pi_board.py` projects bounded execution/check evidence into a shared local board. The existing Pi supervisor refreshes it roughly every 15 seconds and on terminal exit. It enqueues only actionable events to the exact registered desktop task UUID. Idle delivery starts a turn; busy delivery waits for the current turn to end. A local observation does not call a model. A real owner turn still loads its normal context.

Event content and main decisions are distinct from queue claims. Claims use a short file lock, released before the bounded CLI invocation. A confirmed send is not repeatedly sent while review is pending. An ambiguous post-spawn failure is uncertain, requiring explicit recovery; the queue has no caller idempotency key. Pause and uncertain delivery are visible through compact recovery evidence. Short hooks provide interruption/recovery and suppress overlapping legacy Stop notifications for a board route; no synchronous hook waiting or heartbeat is required.

The application must be running/available for a visible desktop continuation. The supervisor cannot detect its own death. Command deadlines and optional no-follow directory guards detect only their declared limits. Tested desktop evidence is in [CLI queue validation](../docs/validation/cli-queue-20260926.md) and the [tiny real Pi fixture](../docs/validation/quick-pi-acceptance-20260926.md). Retained historical lifecycle tests below are not the final event-transport release result.

## Model policy (hard restriction)

This mechanism may run Pi with **only `deepseek/deepseek-flash`**:

- `load_config` rejects any other value, including `openai-codex/gpt-6-luna`,
  unqualified IDs such as `deepseek-flash`, case/whitespace-altered copies, and
  an explicitly empty or non-string value. The allowed model is never
  substituted silently; omitting the key defaults to the allowed model.
- `start` re-asserts the config value, and `continue` additionally requires the
  frozen task model to be the allowed model. A task frozen under another model
  cannot be continued even when the current profile is valid; the rejection
  happens before any lock or round work and leaves all existing evidence
  unmodified.
- The detached worker independently revalidates the frozen model immediately
  after reading `task.json` — before touching round files, acquiring locks or
  launching anything — so the internal `_worker` entry and an edited or stale
  `task.json` cannot bypass the check.
- The spawn argv pins the constant `deepseek/deepseek-flash`; it never uses
  `task["model"]`, an automatic choice or a provider fallback.
- Thinking is preserved from config (the profiles use `max`).
- Pi's **reported** provider/model still comes from raw events and is compared
  with the required model. A mismatch stays visible as `model_check:
  "mismatch"` and the result stays `acceptance: "not_verified"`; argv alone is
  not treated as proof that the provider ran the requested model.
- Pi execution never invokes Codex or another model. Offline tests use process doubles; real CLI queue validation is separate;
  disallowed IDs appear only as offline fixture metadata.

## Repository resolution

`--repo` may point anywhere inside a checkout. The runtime resolves the git
toplevel of the supplied path and keeps **that checkout** as the project
identity (`canonical_root`). It deliberately does not redirect to the Git
primary worktree and never loads another checkout's config automatically.

This matters for the authorized installation: the project checkout
`/Users/jlpeng/works/eugl-bake-pos-recovery-bake03` is itself a linked worktree
whose Git primary is `/Users/jlpeng/works/eugl-bake-pos-recovery`; the config
exists only in the linked project checkout, and the primary is not the target.

- `project`/`start` use the config of the supplied checkout only. If it is
  missing, the error names the path and, when a sibling checkout holds
  `.agents/codex-pi.json`, points at it without loading it.
- `validate_worktree` rejects **both** the configured project checkout passed as
  `--repo` and the repository's **Git primary checkout** as implementation
  worktrees. A different linked checkout is accepted.
- `continue` requires the frozen `repo` identity to match the supplied
  checkout; a different checkout is rejected with guidance to pass `--repo`
  inside the frozen checkout. This closes `--repo` inversion attempts even when
  the other checkout has no config. `result`, `wait` and `cancel` continue to
  read common-dir evidence from any checkout of the project.
- The freeze records the checkout that started the task, so a task can never
  silently switch projects or configs on continuation.

## State layout

Everything lives under the repository's **git common dir**:

```
<git-common-dir>/codex-pi/
  .admission.lock                         per-project start/continue admission
  worktrees/<sha256(realpath)>.claim.json permanent one-writer claim per worktree
  tasks/<task>/
    task.json                             frozen repo/worktree/model/thinking/session/runtime
    .task.lock                            held by the supervisor AND inherited by Pi
    .supervisor.lock                      held only by the live supervisor
    tools/                                frozen helper snapshot + VERSION for this task
    session/                              pinned Pi session directory
    cancel.json / cancel.observed
    supervisor.log
    rounds/<n>/
      brief.md                            immutable (0444) and sha256-pinned
      round.jsonl / round.err             raw Pi stdout/stderr
      round.meta / round.state.json       exact exit, timeout, cancel, HEAD, error
      round.summary.json / round.summary.txt
      round.checks/                       pi_check receipts
```

A detached worker executes the **snapshot** `tasks/<task>/tools/pi_task.py`, so
updating the plugin cannot change an active run. `task.json` records the runtime
version and helper hashes. Worktree claims are conservative, task-lifetime
reservations: they are never auto-released and never auto-deleted, so every new
task needs a new linked checkout. This is stated by `project` and by the claim
error.

## Command contract

- `project` — resolved project checkout, config, capabilities/limits (including
  `allowedModels` and the worktree rule), active tasks. No model request, no
  worker. Missing config is an actionable error; the config must stay inside the
  repo (symlink/traversal rejected) and constraint/check entries are references
  only, never executed.
- `start` — fresh task, immutable round 1, detached worker; returns identity and
  evidence paths immediately. The config model must be the allowed model.
  `--read-only` limits Pi to `read,grep,find,ls`; writable uses
  `read,write,edit,bash` and is explicitly not a security sandbox. Every brief
  contains the user directive never to run `codex`. The command returns while
  the worker continues after the caller process exits.
- `continue` — only a terminal-known task of the same frozen checkout, same
  pinned session and worktree, next immutable round. Refuses while the task lock
  is held (including by an orphaned Pi), when the previous state is unknown,
  when the frozen model is not permitted, or when brief/exit/log evidence is
  stale; no automatic retry or replay.
- `result` — bounded state, all rounds (including every failed prior attempt),
  bounded summary and evidence pointers; no raw logs or command traces by
  default. `supervisorAlive` and `activeWorker` expose lock truth.
- `wait` — internal bounded wait (default/max 60000 ms); timeout never cancels
  Pi. This command itself cannot wake a finished main turn; registered board
  delivery is the separate automatic continuation path. Do not spin repeat waits.
- `cancel` — writes an explicit request for the live supervisor, which stops its
  Pi process group including descendants that ignore SIGTERM. If the supervisor
  is gone but an orphaned Pi still holds the task lock, it returns
  `request: "orphaned"` without signaling any unverified PID.

Exit code 0 always means **completed execution only**. Every result carries
`acceptance: "not_verified"`.

## Lock and liveness model

- The detached supervisor holds `.task.lock` and `.supervisor.lock`.
- Pi inherits `.task.lock` (`pass_fds`), so a SIGKILLed supervisor cannot free
  capacity or the worktree while its Pi child is still running. `maxWorkers` and
  `continue` therefore keep rejecting.
- `.supervisor.lock` is never inherited, so a vanished supervisor is detectable.
  An active recorded state without a live supervisor lease is `unknown`, never
  `completed` and never a reason to start a continuation — even when an orphaned
  Pi still holds the task lock.
- A parent-side `starting` record while the task lock is held is still treated
  as active (pre-lease spawn window), not as a vanished supervisor.
- `cancel` uses the lease to distinguish a live worker from an orphan; it never
  signals a PID it cannot prove it owns.

## Review repairs in this revision

1. **Duplicate `start` cannot mutate prior evidence.** `cmd_start` tracks
   whether this invocation created the task directory; cleanup and failure
   marking only apply to a directory this invocation created. Duplicate starts
   return an error and leave every existing state/brief/raw/meta byte unchanged
   for active, failed and completed tasks.
2. **No configured/primary checkout bypass and no config cross-loading.**
   `canonical_root` stays on the supplied checkout; the Git primary is only used
   to reject it as a worktree; the configured checkout is rejected too; missing
   configs produce a sibling-checkout hint instead of loading another project.
   `continue` preserves the frozen repo identity and rejects a different
   `--repo` checkout with guidance.
3. **Orphaned Pi keeps capacity protection.** Pi inherits the task lock and the
   supervisor lease detects supervisor death. With `maxWorkers=1`, killing only
   the supervisor leaves the Pi child alive, a second start is rejected, the
   original result is `unknown`, and `continue` is blocked until the operator
   inspects and cleans the exact orphan process.
4. **Model hard restriction enforced end to end.** Config, start, continuation
   and the internal worker all require exactly `deepseek/deepseek-flash`; the
   spawn argv pins it; reported-model mismatches stay visible and unverified.
   The earlier defect fixes and the MCP/Node removal remain in effect: the
   deliverable is Python stdlib lifecycle only.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Historical lifecycle baseline on 2026-09-26: **52 tests, OK, exit 0** (37.321 s),
full output preserved at
`/tmp/pi-collab-research-20260926/final-runtime-tests.log`. No live model call,
no network, no Codex binary or GPT probe; the Pi process is always the offline
double in `tests/doubles/`. Coverage includes:

- config safety (missing/invalid/symlink/traversal/limits) plus the missing
  config sibling-checkout hint;
- model policy: disallowed, unqualified, whitespace-altered and empty models are
  rejected by `project` and `start` without running any Pi double; a stale
  frozen model is rejected by `continue` and by direct `_worker` invocation
  without changing evidence; a simulated reported-model mismatch remains
  `model_check: mismatch` / `not_verified`; the no-Codex trap is preserved;
- repository semantics: checkout-only config resolution, a second linked worker
  checkout accepted with the project checkout config, configured and Git primary
  checkouts rejected as worktrees, `--repo` inversion rejected via the missing
  config hint, frozen repo identity preserved on continue, and `result` readable
  from another checkout via common-dir evidence;
- read-only `project` checks against the real user paths
  (`eugl-bake-pos-recovery-bake03` and its Git primary `eugl-bake-pos-recovery`)
  with no model launch;
- lifecycle start/complete, pinned session across continuation, immutable
  evidence, failed attempts kept visible, missing usage stays unknown, reasoning
  not double-counted;
- duplicate-start preservation for active, failed and completed tasks;
- recovery: nonzero exit, timeout (124), cancel of a SIGTERM-ignoring
  descendant, lingering descendant after normal completion, unknown after a
  SIGKILLed supervisor, orphaned-Pi capacity/unknown/continue-block behavior;
- concurrency: same task, same worktree and `maxWorkers` races across real
  processes; project isolation for the same task id; foreign worktree;
- process detachment without MCP: the `start` command returns, its caller
  process exits, and the worker keeps running to completion.

## Limitations

- The runtime validates an existing linked checkout; the main session creates it
  (`git worktree add`). Claims are permanent for the task lifetime, so a new task
  needs a new checkout.
- `read_only` is a Pi tool allowlist, not a sandbox; writable `bash` has the
  user's full local capability.
- The model restriction is a hard configuration/runtime guard: the runtime
  rejects other model IDs, pins argv and surfaces reported mismatches, but it
  cannot cryptographically attest the upstream provider's identity.
- An orphaned Pi or supervisor is never killed automatically. `result` reports
  `unknown` plus recorded PIDs, and the operator must inspect and clean the exact
  processes; the runtime does not signal unverified PIDs.
- `wait` cannot deliver an automatic wake after a caller turn ends, and its
  timeout gives no evidence that Pi stopped.
- Config checks/constraints are text passed to Pi; the runtime never executes
  them. Real acceptance remains with the project's checks and Codex review.
- Unreported provider usage stays `null`/unknown; no cost or capability claims.
- `codex_usage.py` is intentionally not bundled: it audits Codex sessions and no
  current lifecycle command needs it.
- Requires Python 3.10+, Git, and `fcntl`/POSIX process groups (macOS/Linux).
  Tests use process doubles only; real Pi/provider behavior is not proven here.

Three reviewed quality failures in the same phase (across contract revisions) produce a retained Codex takeover. Distinct rounds count once; external blockers require explicit classification and a note. Continuation and internal-worker admission enforce the derived board policy. The runtime never implements or starts Codex: the existing main task reassesses the complete outcome and performs authorized work after verifying writer release. Project acceptance remains the completion authority.
