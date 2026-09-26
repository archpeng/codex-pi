#!/usr/bin/env python3
"""Cross-project persistent Pi worker lifecycle for the Codex-Pi plugin.

Pure Python stdlib. This runtime never executes the Codex CLI and never starts a
Codex agent; the only coding-agent process it launches is Pi, or the executable
named by the explicit ``PI_BIN`` test/override environment variable.

Execution facts live under ``<git-common-dir>/codex-pi/tasks/<task>/``. Raw
round logs, briefs and receipts are immutable once written. A detached worker
holds the task lock until it has finished the owned Pi process group.

Exit code 0 means the Pi process completed execution. It is never acceptance
PASS; acceptance stays with the project's own checks and the main review.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from pi_size import sanitize_snapshot
from pi_summary import bounded, compact, read_meta, summarize

SCHEMA_VERSION = 1
DEFAULT_MODEL = "deepseek/deepseek-flash"
DEFAULT_THINKING = "max"
DEFAULT_TIMEOUT = 14400
MAX_TIMEOUT = 604800
MAX_PROMPT_BYTES = 2_000_000
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
READ_ONLY_TOOLS = "read,grep,find,ls"
WRITABLE_TOOLS = "read,write,edit,bash"
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
TERMINAL_STATES = ("completed", "failed", "timed_out", "cancelled", "interrupted")
ACTIVE_STATES = ("starting", "running")
CONFIG_KEYS = ("schemaVersion", "model", "thinking", "constraints", "checks",
               "maxWorkers", "timeoutSeconds")
HELPER_FILES = ("pi_task.py", "pi_summary.py", "pi_check.py", "pi_copy.py", "pi_size.py",
                "VERSION")
REFERENCE_EXTENSIONS = ("md", "markdown", "txt", "json", "sh", "bash", "zsh", "py",
                        "js", "mjs", "cjs", "ts", "tsx", "yaml", "yml", "toml", "cfg", "ini")


class LockHeld(Exception):
    """Another process owns the lock."""


# ---------------------------------------------------------------------------
# small filesystem / process helpers (atomic + terminate are reused by pi_check)
# ---------------------------------------------------------------------------

def atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


def primary_root(checkout: Path) -> Path:
    """Resolve the repository's primary (main) worktree, if any.

    Used only to reject the Git primary checkout as an implementation worktree.
    Config and state always follow the checkout supplied in --repo; the runtime
    never redirects to or loads config from another checkout automatically.
    """
    try:
        listing = git(checkout, "worktree", "list", "--porcelain")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"cannot resolve the primary worktree for {checkout}") from exc
    for line in listing.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):]).resolve()
    raise ValueError(f"no primary worktree found for {checkout}")


def canonical_root(repo: Path) -> Path:
    """Resolved checkout top-level of the path supplied in --repo.

    Nested paths normalize to their checkout root. The runtime deliberately does
    not redirect to the Git primary worktree: a linked project checkout keeps
    its own config and identity (for example eugl-bake-pos-recovery-bake03).
    """
    repo = repo.expanduser()
    if not repo.exists():
        raise ValueError(f"repository path does not exist: {repo}")
    try:
        top = git(repo if repo.is_dir() else repo.parent, "rev-parse", "--show-toplevel")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError(f"not a git repository: {repo}") from exc
    return Path(top).resolve()


def git_common_dir(cwd: Path) -> Path:
    try:
        raw = git(cwd, "rev-parse", "--git-common-dir")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"cannot resolve git common dir for {cwd}") from exc
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def inside(path: Path, root: Path) -> bool:
    path, root = Path(path).resolve(), Path(root).resolve()
    return path == root or root in path.parents


def lock_fd(path: Path, blocking: bool = False, timeout: float = 30.0) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    if not blocking:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockHeld(f"lock is held: {path}") from None
        return fd
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LockHeld(f"lock wait timed out: {path}") from None
            time.sleep(0.05)


def lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin may return EPERM for signal 0 after the group leader is reaped.
        # Verify the process table; EPERM alone never proves the group is gone.
        try:
            rows = subprocess.check_output(["ps", "-axo", "pgid=,stat="], text=True, timeout=3)
        except (subprocess.SubprocessError, OSError):
            return True
        return any(len(parts := row.split()) == 2 and parts[0] == str(pgid)
                   and not parts[1].startswith("Z") for row in rows.splitlines())


def terminate(child: subprocess.Popen) -> None:
    """Own the child's process group until every descendant is gone.

    The leader may exit on TERM while its descendants ignore it; waiting only
    for the leader leaks those writers. This is the tested group fix, kept
    intact for the detached lifecycle.
    """
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        child.poll()  # reap the leader so it does not keep an empty group visible
        if not group_alive(child.pid):
            child.wait()
            return
        time.sleep(0.05)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def looks_like_path(entry: str) -> bool:
    if entry.startswith(("/", "~", ".")) or "\\" in entry:
        return True
    parts = entry.replace("\\", "/").split("/")
    if ".." in parts:
        return True
    if " " not in entry and "/" in entry:
        return True
    return bool(re.fullmatch(r"[\w.-]+\.(" + "|".join(REFERENCE_EXTENSIONS) + r")", entry))


def validate_reference(root: Path, entry: str, kind: str) -> None:
    if not looks_like_path(entry):
        return
    if ".." in entry.replace("\\", "/").split("/"):
        raise ValueError(f"{kind} reference uses path traversal and is rejected: {entry!r}")
    candidate = Path(entry).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if not inside(candidate, root):
        raise ValueError(f"{kind} reference escapes the repository and is rejected: {entry!r}")


def require_allowed_model(model, context: str) -> str:
    """Hard model restriction: deepseek/deepseek-flash and nothing else."""
    if model != DEFAULT_MODEL:
        raise ValueError(f"{context} model {model!r} is not allowed; this runtime only permits "
                         f"{DEFAULT_MODEL!r} (no substitution or fallback is performed; existing "
                         "evidence is not modified)")
    return model


def config_hint(root: Path) -> str:
    """Point at a sibling checkout holding config without loading it."""
    try:
        listing = git(root, "worktree", "list", "--porcelain")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    found = []
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line[len("worktree "):]).resolve()
        if candidate != root and (candidate / ".agents" / "codex-pi.json").is_file():
            found.append(str(candidate))
    if not found:
        return ""
    return ("; config exists only in another checkout: " + ", ".join(found) +
            "; configs are never loaded across checkouts, so pass --repo inside that checkout")


def load_config(root: Path) -> dict:
    path = root / ".agents" / "codex-pi.json"
    if not path.exists():
        raise ValueError(
            f"missing project config {path}; create it as "
            '{"schemaVersion":1,"model":"deepseek/deepseek-flash","thinking":"max",'
            '"constraints":["AGENTS.md"],"checks":[],"maxWorkers":1,"timeoutSeconds":14400}'
            + config_hint(root))
    real = path.resolve()
    if not inside(real, root):
        raise ValueError(f"config path escapes the repository (symlink or traversal): {path} -> {real}")
    try:
        data = json.loads(real.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"config is not valid JSON: {real}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config must be a JSON object: {real}")
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise ValueError(f"config has unsupported keys {unknown}; allowed: {list(CONFIG_KEYS)}")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(f"config schemaVersion must be {SCHEMA_VERSION}, got {data.get('schemaVersion')!r}")

    model = data.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("config model must be a non-empty string")
    require_allowed_model(model, "config")
    thinking = data.get("thinking", DEFAULT_THINKING)
    if not isinstance(thinking, str) or thinking not in THINKING_LEVELS:
        raise ValueError(f"config thinking must be one of {list(THINKING_LEVELS)}")
    constraints = data.get("constraints", [])
    checks = data.get("checks", [])
    for label, entries in (("constraints", constraints), ("checks", checks)):
        if not isinstance(entries, list) or any(not isinstance(item, str) or not item.strip() for item in entries):
            raise ValueError(f"config {label} must be an array of non-empty strings")
        for entry in entries:
            validate_reference(root, entry, label[:-1] if label.endswith("s") else label)
    max_workers = data.get("maxWorkers", 1)
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 64:
        raise ValueError("config maxWorkers must be an integer between 1 and 64")
    timeout = data.get("timeoutSeconds", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"config timeoutSeconds must be positive and at most {MAX_TIMEOUT}")
    return {"path": str(real), "schemaVersion": SCHEMA_VERSION, "model": model, "thinking": thinking,
            "constraints": list(constraints), "checks": list(checks),
            "maxWorkers": max_workers, "timeoutSeconds": timeout}


# ---------------------------------------------------------------------------
# state layout and admission
# ---------------------------------------------------------------------------

def state_root(common: Path) -> Path:
    return common / "codex-pi"


def task_dir_for(common: Path, task: str) -> Path:
    if not TASK_RE.fullmatch(task):
        raise ValueError("task must match [A-Za-z0-9][A-Za-z0-9_-]{0,99}")
    return state_root(common) / "tasks" / task


def require_task_arg(task: str) -> str:
    if not TASK_RE.fullmatch(task):
        raise ValueError("task must match [A-Za-z0-9][A-Za-z0-9_-]{0,99}")
    return task


def list_rounds(task_dir: Path):
    rounds_dir = task_dir / "rounds"
    result = []
    if rounds_dir.is_dir():
        for entry in rounds_dir.iterdir():
            if entry.is_dir() and entry.name.isdigit() and int(entry.name) >= 1:
                result.append((int(entry.name), entry))
    return sorted(result)


def active_tasks(state: Path) -> list:
    tasks_dir = state / "tasks"
    result = []
    if tasks_dir.is_dir():
        for entry in sorted(tasks_dir.iterdir()):
            if entry.is_dir() and lock_is_held(entry / ".task.lock"):
                result.append(entry.name)
    return result


def claim_worktree(common: Path, worktree: Path, task: str) -> Path:
    claims = state_root(common) / "worktrees"
    claims.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()
    path = claims / f"{key}.claim.json"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        owner = read_json(path, {}) or {}
        raise ValueError(f"worktree {worktree} is already claimed by task {owner.get('task', 'unknown')}; "
                         "claims last for the task lifetime and are not auto-released, so use a "
                         "separate checkout for every new writer task") from None
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"schemaVersion": 1, "task": task, "worktree": str(worktree),
                   "createdAt": time.time()}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def release_claim(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def validate_worktree(common: Path, root: Path, worktree_arg: str):
    if not worktree_arg or not str(worktree_arg).strip():
        raise ValueError("worktree is required")
    worktree = Path(worktree_arg).expanduser()
    if not worktree.exists() or not worktree.is_dir():
        raise ValueError(f"worktree does not exist: {worktree}; create a separate checkout first")
    real = worktree.resolve()
    try:
        top = Path(git(real, "rev-parse", "--show-toplevel")).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ValueError(f"worktree is not a git checkout: {real}") from exc
    if top != real:
        raise ValueError(f"worktree argument must be the checkout root {top}, got {real}")
    wt_common = git_common_dir(real)
    if wt_common != common:
        raise ValueError(f"worktree {real} belongs to git common dir {wt_common}, not {common}; "
                         "foreign worktrees are rejected")
    try:
        primary = primary_root(root)
    except ValueError:
        primary = root
    if real == primary:
        raise ValueError("worktree must be a separate checkout, not the repository's Git primary checkout")
    if real == root:
        raise ValueError("worktree must be a separate checkout, not the configured project checkout passed as --repo")
    try:
        head = git(real, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        head = None
    return real, head


# ---------------------------------------------------------------------------
# brief and helper snapshot
# ---------------------------------------------------------------------------

def runtime_version() -> str:
    version_file = Path(__file__).resolve().parent / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def snapshot_helpers(source: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in HELPER_FILES:
        source_file = source / name
        if not source_file.is_file():
            raise ValueError(f"runtime helper missing: {source_file}")
        data = source_file.read_bytes()
        (destination / name).write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    return hashes


def compose_brief(task: dict, round_number: int, prompt: str, prior: dict | None) -> str:
    read_only = bool(task["readOnly"])
    tools = READ_ONLY_TOOLS if read_only else WRITABLE_TOOLS
    task_dir = Path(task["taskDir"])
    round_dir = task_dir / "rounds" / str(round_number)
    helper = task_dir / "tools" / "pi_check.py"
    checks_dir = round_dir / "round.checks"
    lines = [prompt.rstrip(), "", "---", "## Appended Codex-Pi worker contract", "",
             "User directive: do not execute the Codex CLI (`codex`), do not launch any Codex",
             "agent, and do not call an OpenAI model through Codex. The Codex main session",
             "reviews outcomes; this Pi session implements and reports. Never run `codex`.",
             "",
             "Model policy: this mechanism permits only `deepseek/deepseek-flash`. Do not call,",
             "delegate to, or spawn any nested agent/model on another provider or model, and do",
             "not fall back automatically to any other model. If the pinned allowed model is not",
             "available, stop and report that instead of switching models.",
             "",
             f"Mode: {'read-only' if read_only else 'writable'}; allowed tools: {tools}.",
             ("Read-only is a tool allowlist, not a security sandbox."
              if read_only else
              "This is explicitly not a security sandbox; bash has full local capability."),
             "",
             "Applicable AGENTS.md files discovered from the worktree remain authoritative.",
             "Prompt templates, skills and extensions are disabled for this session."]
    constraints = task.get("constraints") or []
    if constraints:
        lines += ["", "Project constraints (references/instructions; the runtime never executes them):"]
        lines += [f"  - {entry}" for entry in constraints]
    checks = task.get("checks") or []
    if checks:
        lines += ["", "Project checks (references/instructions only; never invent acceptance):"]
        lines += [f"  - {entry}" for entry in checks]
    if prior:
        lines += ["", f"Previous round {prior.get('round')}: outcome={prior.get('state')} "
                      f"exit={prior.get('exitCode')} head={prior.get('endHead')}. "
                      "That is execution evidence only; read its summary before continuing."]
    lines += [
        "",
        "Record real check evidence with this task's frozen helper:",
        f'  python3 "{helper}" --output-dir "{checks_dir}" --id <safe-id> \\',
        f'      --timeout-seconds {int(task["timeoutSeconds"])} -- <real check command>',
        "Receipts capture the true exit/signal/timeout, log sha256, HEAD and dirty state.",
        "Optional declared directory budget for that check (path and budget required together):",
        f'  python3 "{helper}" --output-dir "{checks_dir}" --id <safe-id> \\',
        f'      --watch-path PATH --max-bytes N [--health-interval-seconds 15] -- <real check command>',
        "The guard counts regular-file bytes under PATH without following symlinks; incomplete "
        "measurements stay unknown, and a known breach stops only that owned check process group.",
        "Safe evidence copy (symlinks are preserved literally and never followed; DEST must be new):",
        f'  python3 "{task_dir / "tools" / "pi_copy.py"}" SOURCE DEST [--max-bytes N]',
        f'Only "{task_dir / "tools"}" and "{checks_dir}" may be written outside the worktree.',
        "",
        "End with a concise report of changes and evidence. Never claim acceptance PASS;",
        "a zero exit code only proves execution finished.",
    ]
    return "\n".join(lines) + "\n"


def write_brief(path: Path, text: str) -> str:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o444)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def worker_env() -> dict:
    return os.environ.copy()


def spawn_worker(task_dir: Path, round_number: int, lock_fd_value: int, timeout_seconds: float) -> None:
    task = read_json(task_dir / "task.json", {}) or {}
    worktree = Path(task.get("worktree", "."))
    script = task_dir / "tools" / "pi_task.py"
    argv = [sys.executable, str(script), "_worker", "--task-dir", str(task_dir),
            "--round", str(round_number), "--lock-fd", str(lock_fd_value),
            "--timeout-seconds", str(timeout_seconds)]
    with (task_dir / "supervisor.log").open("a", encoding="utf-8") as output:
        subprocess.Popen(argv, cwd=str(worktree), stdin=subprocess.DEVNULL,
                         stdout=output, stderr=output, start_new_session=True,
                         pass_fds=(lock_fd_value,), env=worker_env())


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

def cancel_requested(task_dir: Path, round_number: int, field: str = "cancel.json") -> bool:
    data = read_json(task_dir / field, None)
    return bool(isinstance(data, dict) and data.get("round") == round_number)


def finish_round(task_dir: Path, round_number: int, round_dir: Path, task: dict,
                 state: dict, outcome: str, code, ended: dict) -> None:
    worktree = Path(task["worktree"])
    try:
        end_head = git(worktree, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        end_head = None
    state.update(state=outcome, exitCode=code, endedAt=time.time(), endHead=end_head, **ended)
    with (round_dir / "round.meta").open("a", encoding="utf-8") as meta:
        meta.write(f"exit={code}\nend={state['endedAt']}\nhead={end_head}\n"
                   f"outcome={outcome}\ntimed_out={bool(ended.get('timedOut'))}\n"
                   f"cancelled={bool(ended.get('cancelled'))}\n")
    atomic(round_dir / "round.state.json", state)
    try:
        data = summarize(round_dir / "round.jsonl", worktree, round_dir,
                         DEFAULT_MODEL, checks_dir=round_dir / "round.checks")
        atomic(round_dir / "round.summary.json", data)
        (round_dir / "round.summary.txt").write_text(compact(data), encoding="utf-8")
    except Exception as exc:  # summary failure must not hide the raw evidence
        state["summaryError"] = str(exc)
        atomic(round_dir / "round.state.json", state)


def run_worker(args) -> int:
    task_dir = Path(args.task_dir)
    task = read_json(task_dir / "task.json", None)
    if not isinstance(task, dict):
        raise ValueError(f"worker cannot read task.json under {task_dir}")
    # Independent revalidation before any evidence write or process launch: the
    # internal _worker entry and an edited/stale task.json must not bypass the
    # model restriction.
    require_allowed_model(task.get("model"), "frozen task")
    round_number = int(args.round)
    round_dir = task_dir / "rounds" / str(round_number)
    if not round_dir.is_dir():
        raise ValueError(f"round directory is missing: {round_dir}")
    for name in ("round.jsonl", "round.err"):
        (round_dir / name).touch(exist_ok=True)

    # Supervisor lease: held only by this supervisor and never inherited by Pi,
    # so a vanished supervisor is detectable even while an orphaned Pi child
    # still holds the task lock. A previous supervisor may still be exiting, so
    # wait briefly for its lease instead of failing a fresh continuation.
    state_path = round_dir / "round.state.json"
    state = read_json(state_path, {}) or {}
    state.update(taskDir=str(task_dir), round=round_number)
    try:
        lock_fd(task_dir / ".supervisor.lock", blocking=True, timeout=10)
    except LockHeld as exc:
        state.update(state="unknown", exitCode=None, endedAt=time.time(),
                     error=f"supervisor lease unavailable: {exc}")
        atomic(state_path, state)
        return 1
    worktree = Path(task["worktree"])
    try:
        start_head = git(worktree, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        start_head = None
    state.update(state="running", supervisorPid=os.getpid(), startedAt=time.time(),
                 startHead=start_head, timedOut=False, cancelled=False, exitCode=None,
                 workerScript=str(Path(__file__).resolve()), runtimeVersion=runtime_version())
    atomic(state_path, state)

    if cancel_requested(task_dir, round_number):
        atomic(task_dir / "cancel.observed", {"round": round_number, "at": time.time(), "beforeSpawn": True})
        finish_round(task_dir, round_number, round_dir, task, state, "cancelled", None,
                     {"timedOut": False, "cancelled": True, "note": "cancellation was requested before Pi started"})
        return 0

    tools = READ_ONLY_TOOLS if task["readOnly"] else WRITABLE_TOOLS
    pi_bin = os.environ.get("PI_BIN") or "pi"
    argv = [pi_bin, "-p", "--mode", "json", "--session-id", task["sessionId"],
            "--session-dir", task["sessionDir"], "--model", DEFAULT_MODEL,
            "--thinking", task["thinking"], "--tools", tools,
            "--no-extensions", "--no-skills", "--no-prompt-templates",
            "@" + str(round_dir / "brief.md")]

    child = None
    outcome, code = "failed", 1
    timed_out = cancelled = False
    error = None
    caught = {"signal": None}

    def interrupted(sig, _frame):
        caught["signal"] = sig
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with (round_dir / "round.jsonl").open("ab") as out, (round_dir / "round.err").open("ab") as err:
            # Pi inherits the task lock, so a SIGKILLed supervisor cannot free
            # capacity while its Pi child is still running.
            child = subprocess.Popen(argv, cwd=str(worktree), stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=err, start_new_session=True,
                                     env=worker_env(), pass_fds=(args.lock_fd,))
        state["piPid"] = child.pid
        atomic(state_path, state)
        deadline = time.monotonic() + float(args.timeout_seconds)
        while True:
            raw = child.poll()
            if raw is not None:
                code = raw if raw >= 0 else 128 - raw
                outcome = "completed" if code == 0 else "failed"
                break
            if cancel_requested(task_dir, round_number):
                cancelled, outcome = True, "cancelled"
                break
            if time.monotonic() >= deadline:
                timed_out, outcome, code = True, "timed_out", 124
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        outcome, code = "interrupted", 128 + (caught["signal"] or signal.SIGINT)
    except Exception as exc:
        error = str(exc)
        outcome, code = "unknown", None
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        if child is not None:
            terminate(child)
            raw = child.returncode
            if outcome == "completed":
                code = raw if raw is not None and raw >= 0 else code
            elif outcome in ("cancelled", "timed_out", "interrupted") and raw is not None:
                if outcome != "timed_out":
                    code = raw if raw >= 0 else 128 - raw
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    if cancelled:
        atomic(task_dir / "cancel.observed", {"round": round_number, "at": time.time()})
    finish_round(task_dir, round_number, round_dir, task, state, outcome, code,
                 {"timedOut": timed_out, "cancelled": cancelled, "error": error})
    return 0


# ---------------------------------------------------------------------------
# start / continue
# ---------------------------------------------------------------------------

def project_context(repo_arg: str):
    root = canonical_root(Path(repo_arg))
    config = load_config(root)
    common = git_common_dir(root)
    return root, config, common


def ensure_round_inputs(task_dir: Path, round_number: int, prompt: str, task: dict,
                        prior: dict | None) -> Path:
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError(f"prompt exceeds {MAX_PROMPT_BYTES} bytes")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    round_dir = task_dir / "rounds" / str(round_number)
    round_dir.mkdir(parents=True)
    (round_dir / "brief.md").parent.mkdir(parents=True, exist_ok=True)
    try:
        if (round_dir / "brief.md").exists():
            raise ValueError(f"round {round_number} already has a brief; never overwrite evidence")
        digest = write_brief(round_dir / "brief.md", compose_brief(task, round_number, prompt, prior))
    except FileExistsError:
        raise ValueError(f"round {round_number} already has a brief; never overwrite evidence") from None
    atomic(round_dir / "round.state.json",
           {"schemaVersion": SCHEMA_VERSION, "round": round_number, "state": "starting",
            "startedAt": time.time(), "exitCode": None, "timedOut": False, "cancelled": False,
            "briefSha256": digest, "taskDir": str(task_dir)})
    with (round_dir / "round.meta").open("a", encoding="utf-8") as meta:
        meta.write(f"task={task['task']} round={round_number} model={task['model']} "
                   f"thinking={task['thinking']}\nworktree={task['worktree']}\n"
                   f"start={time.time()}\nbrief_sha256={digest}\n")
    return round_dir


def record_worker_failure(task_dir: Path, round_number: int, error: str) -> None:
    path = task_dir / "rounds" / str(round_number) / "round.state.json"
    state = read_json(path, {}) or {}
    state.update(state="unknown", exitCode=None, endedAt=time.time(), error=error)
    atomic(path, state)


def cmd_start(args) -> dict:
    root, config, common = project_context(args.repo)
    require_allowed_model(config["model"], "config")
    task = require_task_arg(args.task)
    worktree, start_head = validate_worktree(common, root, args.worktree)
    prompt = read_prompt(args)
    state = state_root(common)
    tasks_dir = state / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_dir = tasks_dir / task

    admission = lock_fd(state / ".admission.lock", blocking=True, timeout=30)
    claim = None
    lock_value = None
    created = False
    try:
        if task_dir.exists():
            raise ValueError(f"task {task!r} already exists at {task_dir}; existing evidence is left "
                             "untouched, use pi_continue for it")
        os.mkdir(task_dir)
        created = True
        claim = claim_worktree(common, worktree, task)
        busy = active_tasks(state)
        if len(busy) >= config["maxWorkers"]:
            raise ValueError(f"project maxWorkers={config['maxWorkers']} reached; active tasks: {busy}")
        lock_value = lock_fd(task_dir / ".task.lock")
        (task_dir / "session").mkdir(parents=True, exist_ok=True)
        hashes = snapshot_helpers(Path(__file__).resolve().parent, task_dir / "tools")
        task_json = {
            "schemaVersion": SCHEMA_VERSION, "task": task, "taskDir": str(task_dir),
            "repo": str(root), "commonDir": str(common), "worktree": str(worktree),
            "configPath": config["path"], "readOnly": bool(args.read_only),
            "model": config["model"], "thinking": config["thinking"],
            "constraints": config["constraints"], "checks": config["checks"],
            "maxWorkers": config["maxWorkers"], "timeoutSeconds": config["timeoutSeconds"],
            "createdAt": time.time(), "startHead": start_head,
            "runtimeVersion": runtime_version(), "helperHashes": hashes,
            "sessionId": task, "sessionDir": str(task_dir / "session"),
        }
        atomic(task_dir / "task.json", task_json)
        ensure_round_inputs(task_dir, 1, prompt, task_json, None)
        spawn_worker(task_dir, 1, lock_value, config["timeoutSeconds"])
    except Exception as exc:
        if lock_value is not None:
            os.close(lock_value)
            lock_value = None
        if created:
            # Only a directory created by this invocation may be cleaned or
            # marked; a duplicate start must never mutate existing evidence.
            if claim is not None:
                release_claim(claim)
            if not (task_dir / "task.json").exists():
                shutil.rmtree(task_dir, ignore_errors=True)
            else:
                record_worker_failure(task_dir, 1, f"worker failed to start: {exc}")
        raise
    finally:
        if lock_value is not None:
            os.close(lock_value)
        os.close(admission)
    return {"ok": True, "task": task, "round": 1, "state": "starting",
            "repo": str(root), "worktree": str(worktree),
            "readOnly": bool(args.read_only), "model": config["model"], "thinking": config["thinking"],
            "sessionId": task, "sessionDir": str(task_dir / "session"),
            "evidence": evidence_paths(task_dir, 1),
            "note": "worker returned immediately; exit 0 will mean completed execution, never acceptance PASS"}


def cmd_continue(args) -> dict:
    root = canonical_root(Path(args.repo))
    common = git_common_dir(root)
    task = require_task_arg(args.task)
    task_dir = task_dir_for(common, task)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task!r}; expected evidence at {task_dir}")
    frozen = read_json(task_dir / "task.json", None)
    if not isinstance(frozen, dict):
        raise ValueError(f"task {task!r} has no readable task.json; inspect {task_dir} before continuing")
    frozen_repo_raw = frozen.get("repo")
    if not isinstance(frozen_repo_raw, str) or not frozen_repo_raw:
        raise ValueError(f"task {task!r} has no frozen repository identity; refusing to continue")
    if Path(frozen_repo_raw).resolve() != root:
        raise ValueError(f"task {task!r} belongs to checkout {frozen_repo_raw}, not {root}; "
                         "pass --repo inside the frozen checkout (result/wait/cancel can still use "
                         "common-dir evidence)")
    require_allowed_model(frozen.get("model"), "frozen task")
    config = load_config(root)
    worktree, _ = validate_worktree(common, root, frozen["worktree"])
    prompt = read_prompt(args)
    state = state_root(common)

    admission = lock_fd(state / ".admission.lock", blocking=True, timeout=30)
    lock_value = None
    round_dir = None
    number = 0
    try:
        try:
            lock_value = lock_fd(task_dir / ".task.lock")
        except LockHeld:
            raise ValueError("task lock is still held by an active worker or an orphaned Pi; "
                             "wait or inspect before continuing") from None
        if lock_is_held(task_dir / ".supervisor.lock"):
            lease_deadline = time.monotonic() + 2
            while time.monotonic() < lease_deadline and lock_is_held(task_dir / ".supervisor.lock"):
                time.sleep(0.05)
        if lock_is_held(task_dir / ".supervisor.lock"):
            raise ValueError("supervisor lease is still held; task is not terminal-known")
        rounds = list_rounds(task_dir)
        if not rounds:
            raise ValueError(f"task {task!r} has no completed round; do not continue an unknown run")
        for value, candidate in rounds:
            round_state = read_json(candidate / "round.state.json", None)
            meta = candidate / "round.meta"
            if not isinstance(round_state, dict):
                raise ValueError(f"round {value} has no state evidence; stale artifacts, inspect {candidate}")
            if round_state.get("state") in ACTIVE_STATES or round_state.get("state") not in TERMINAL_STATES:
                raise ValueError(f"round {value} is not terminal-known (state={round_state.get('state')!r}); "
                                 "do not auto-continue or replay an unknown run")
            if not meta.is_file() or "exit" not in read_meta(meta):
                raise ValueError(f"round {value} has no exit evidence; stale artifacts, inspect {candidate}")
            if not (candidate / "round.jsonl").is_file():
                raise ValueError(f"round {value} raw log is missing; stale artifacts, inspect {candidate}")
        latest_number, latest_dir = rounds[-1]
        latest_state = read_json(latest_dir / "round.state.json", {})
        busy = [name for name in active_tasks(state) if name != task]
        if len(busy) >= config["maxWorkers"]:
            raise ValueError(f"project maxWorkers={config['maxWorkers']} reached; active tasks: {busy}")
        claims = state_root(common) / "worktrees"
        key = hashlib.sha256(str(Path(frozen["worktree"])).encode("utf-8")).hexdigest()
        claim_file = claims / f"{key}.claim.json"
        owner = read_json(claim_file, {}) or {}
        if owner.get("task") != task:
            raise ValueError(f"worktree claim missing or foreign for {frozen['worktree']}; inspect evidence")
        number = latest_number + 1
        prior = {"round": latest_number, "state": latest_state.get("state"),
                 "exitCode": latest_state.get("exitCode"), "endHead": latest_state.get("endHead")}
        round_dir = ensure_round_inputs(task_dir, number, prompt, frozen, prior)
        spawn_worker(task_dir, number, lock_value, frozen["timeoutSeconds"])
    except Exception as exc:
        if lock_value is not None:
            os.close(lock_value)
            lock_value = None
        if round_dir is not None and round_dir.exists():
            record_worker_failure(task_dir, number, f"worker failed to start: {exc}")
        raise
    finally:
        if lock_value is not None:
            os.close(lock_value)
        os.close(admission)
    return {"ok": True, "task": task, "round": number, "state": "starting", "repo": str(root),
            "worktree": str(worktree), "readOnly": bool(frozen.get("readOnly")),
            "model": frozen.get("model"), "thinking": frozen.get("thinking"),
            "sessionId": frozen.get("sessionId"), "sessionDir": frozen.get("sessionDir"),
            "evidence": evidence_paths(task_dir, number),
            "note": "same pinned session and worktree; exit 0 is execution only, never acceptance PASS"}


def read_prompt(args) -> str:
    if getattr(args, "prompt_file", None):
        path = Path(args.prompt_file)
        if not path.is_file():
            raise ValueError(f"prompt file does not exist: {path}")
        return path.read_text(encoding="utf-8")
    if getattr(args, "prompt", None) is not None:
        return args.prompt
    raise ValueError("provide --prompt-file (preferred) or --prompt")


# ---------------------------------------------------------------------------
# result / wait / cancel
# ---------------------------------------------------------------------------

def evidence_paths(task_dir: Path, round_number: int | None) -> dict:
    result = {"taskDir": str(task_dir), "toolsDir": str(task_dir / "tools"),
              "supervisorLog": str(task_dir / "supervisor.log")}
    if round_number is None:
        return result
    round_dir = task_dir / "rounds" / str(round_number)
    result.update({"roundDir": str(round_dir), "brief": str(round_dir / "brief.md"),
                   "jsonl": str(round_dir / "round.jsonl"), "stderr": str(round_dir / "round.err"),
                   "meta": str(round_dir / "round.meta"), "state": str(round_dir / "round.state.json"),
                   "summaryJson": str(round_dir / "round.summary.json"),
                   "summaryText": str(round_dir / "round.summary.txt"),
                   "checksDir": str(round_dir / "round.checks")})
    return result


def effective_state(state: dict | None, task_held: bool, supervisor_alive: bool,
                    is_latest: bool) -> str:
    raw = (state or {}).get("state")
    if raw == "starting" and task_held and is_latest:
        # The parent or a just-spawned worker holds the task lock but the
        # supervisor lease has not been written yet; this is still active.
        return "starting"
    if raw in ACTIVE_STATES:
        return raw if (supervisor_alive and is_latest) else "unknown"
    if raw in TERMINAL_STATES:
        return raw
    return "unknown"


def round_compact(task_dir: Path, number: int, round_dir: Path, task_held: bool,
                  supervisor_alive: bool, latest: bool) -> dict:
    state = read_json(round_dir / "round.state.json", {}) or {}
    got = effective_state(state, task_held, supervisor_alive, latest)
    started = state.get("startedAt")
    ended = state.get("endedAt")
    return {"round": number, "state": got, "exitCode": state.get("exitCode"),
            "timedOut": bool(state.get("timedOut")), "cancelled": bool(state.get("cancelled")),
            "startedAt": started, "endedAt": ended,
            "durationMs": int((ended - started) * 1000) if isinstance(started, (int, float))
            and isinstance(ended, (int, float)) else None,
            "startHead": state.get("startHead"),
            "endHead": state.get("endHead") or state.get("head"),
            "briefSha256": state.get("briefSha256"),
            "summaryError": state.get("summaryError"),
            "evidence": evidence_paths(task_dir, number)}


def load_round_summary(task_dir: Path, task: dict, round_dir: Path, number: int):
    summary_path = round_dir / "round.summary.json"
    data = read_json(summary_path, None)
    if isinstance(data, dict) and "source" in data:
        return data
    log = round_dir / "round.jsonl"
    if not log.is_file():
        return None
    try:
        data = summarize(log, Path(task["worktree"]), round_dir, task.get("model"),
                         checks_dir=round_dir / "round.checks")
        atomic(summary_path, data)
        (round_dir / "round.summary.txt").write_text(compact(data), encoding="utf-8")
        return data
    except Exception as exc:
        return {"schema_version": 1, "acceptance": "not_verified",
                "error": f"summary unavailable: {exc}"}


# ---------------------------------------------------------------------------
# bounded read-only status (no summaries, no transcript scans, no mutation)
# ---------------------------------------------------------------------------

STATUS_MAX_DIR_ENTRIES = 512
STATUS_MAX_RECEIPTS = 200
STATUS_MAX_FILE_BYTES = 2_000_000
STATUS_MAX_MARKER_BYTES = 16_384
STATUS_TAIL_BYTES = 8192
STATUS_TAIL_LINES = 20
STATUS_MAX_LINE = 400
CHECK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
LOG_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
VALID_COUNT_KEYS = ("run", "pass", "fail", "skip")
VALID_COUNT_FORMATS = ("go_verbose_top_level",)
MAX_COUNT_VALUE = 10 ** 12


def _mtime(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


MAX_PID = 2 ** 31 - 1


def _pid_value(value):
    """Keep only a plausible POSIX pid; corrupt huge integers are unknown."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_PID:
        return None
    return value


def _clip(text, limit: int = STATUS_MAX_LINE) -> str:
    text = str(text).replace("\x00", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _safe_basename(value):
    """Bounded single-component file name; never a path, dotfile or separator."""
    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    if value in (".", "..") or value.startswith(".") or "/" in value or "\\" in value \
            or "\x00" in value:
        return None
    return value


def _read_bounded_json(path: Path, limit: int):
    """Read at most ``limit`` bytes and parse JSON; never parse unbounded data."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError:
        return None, "unreadable"
    if len(raw) > limit:
        return None, "oversized"
    try:
        return json.loads(raw.decode("utf-8")), None
    except ValueError:
        return None, "invalid"


def _sanitize_counts(value):
    """Keep only the fixed numeric counters and the known format marker."""
    if not isinstance(value, dict):
        return None
    counts = {}
    for key in VALID_COUNT_KEYS:
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= MAX_COUNT_VALUE:
            counts[key] = item
    if not counts:
        return None
    fmt = value.get("format")
    if isinstance(fmt, str) and fmt in VALID_COUNT_FORMATS:
        counts["format"] = fmt
    return counts


def _pid_running(pid) -> bool:
    pid = _pid_value(pid)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OverflowError:
        return False
    except PermissionError:
        return True


def _tail_evidence(path: Path):
    """Bounded tail read: never the full log or transcript."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"path": str(path), "error": _clip(exc, 200)}
    try:
        with path.open("rb") as stream:
            if size > STATUS_TAIL_BYTES:
                stream.seek(size - STATUS_TAIL_BYTES)
            raw = stream.read(STATUS_TAIL_BYTES)
    except OSError as exc:
        return {"path": str(path), "bytes": size, "error": _clip(exc, 200)}
    lines = raw.decode("utf-8", errors="replace").splitlines()[-STATUS_TAIL_LINES:]
    return {"path": str(path), "bytes": size, "mtime": _mtime(path),
            "tail": [_clip(line) for line in lines],
            "truncated": size > STATUS_TAIL_BYTES}


def _safe_receipt(path: Path):
    """Small safe receipt metadata; rejects unrelated JSON and unbounded strings."""
    data, problem = _read_bounded_json(path, STATUS_MAX_FILE_BYTES)
    if problem is not None:
        return None, f"{problem} receipt"
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None, "unrecognized receipt shape"
    check_id = data.get("id")
    log_name = data.get("log")
    digest = data.get("log_sha256")
    if not isinstance(check_id, str) or not CHECK_ID_RE.fullmatch(check_id):
        return None, "unsafe receipt id"
    if not isinstance(log_name, str) or not log_name.endswith(".log") \
            or _safe_basename(log_name) is None:
        return None, "unsafe receipt log identity"
    if not isinstance(digest, str) or not LOG_DIGEST_RE.fullmatch(digest):
        return None, "unsafe receipt log hash"
    code = data.get("exit_code")
    if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
        return None, "unsafe receipt exit code"
    failed = (code is not None and code != 0) or bool(data.get("timed_out"))
    return {"id": check_id, "exitCode": code, "timedOut": bool(data.get("timed_out")),
            "failed": failed, "testCounts": _sanitize_counts(data.get("test_counts")),
            "startedAt": _number(data.get("started_at")), "endedAt": _number(data.get("ended_at")),
            "log": log_name, "receipt": path.name,
            "resourceLimit": sanitize_snapshot(data.get("resource_limit") or data.get("resourceLimit"))}, None


def _scan_checks(checks_dir: Path) -> dict:
    result = {"dir": str(checks_dir), "exists": checks_dir.is_dir(), "entries": 0,
              "partial": False, "running": None, "legacyCandidate": None, "ignored": [],
              "ignoredCount": 0,
              "receipts": {"total": 0, "scanned": 0, "truncated": False, "partial": False,
                           "failedAttempts": 0, "latest": None, "latestFailed": None,
                           "latestSuccessful": None, "failedRecent": [], "unknownExitRecent": []},
              "resourceGuard": {"breached": False, "unknown": False, "breaches": [],
                                "unknownScans": [], "running": None, "latestReceipt": None,
                                "note": "local no-follow byte guard; unknown is not verified and "
                                        "is never under budget"}}

    def note_ignored(name: str, reason: str) -> None:
        result["ignoredCount"] += 1
        if len(result["ignored"]) < 20:
            result["ignored"].append({"name": name, "reason": reason})

    if not result["exists"]:
        return result
    try:
        entries = []
        for index, entry in enumerate(checks_dir.iterdir()):
            if index >= STATUS_MAX_DIR_ENTRIES:
                result["partial"] = True
                break
            if entry.is_file():
                entries.append(entry)
    except OSError:
        return result
    result["entries"] = len(entries)
    result["receipts"]["partial"] = result["partial"]
    markers = [path for path in entries if path.name.endswith(".running")]
    receipt_files = [path for path in entries if path.name.endswith(".json")]
    logs = [path for path in entries if path.name.endswith(".log")]
    resolved = checks_dir.resolve()

    # Valid receipts first: only proven receipts may suppress a log candidate or
    # supersede a stale running marker. Unrelated or corrupt .json stays visible
    # as ignored evidence and never hides an unreceipted log.
    parsed = []
    for receipt in sorted(receipt_files, key=_mtime, reverse=True)[:STATUS_MAX_RECEIPTS]:
        item, problem = _safe_receipt(receipt)
        if item is None:
            note_ignored(receipt.name, problem)
            continue
        parsed.append(item)
    result["receipts"]["total"] = len(receipt_files)
    result["receipts"]["scanned"] = len(parsed)
    result["receipts"]["truncated"] = len(receipt_files) > STATUS_MAX_RECEIPTS
    result["receipts"]["failedAttempts"] = sum(1 for item in parsed if item["failed"])
    valid_receipt_stems = {Path(item["receipt"]).stem for item in parsed}

    def order(item):
        return item.get("endedAt") or item.get("startedAt") or 0

    result["receipts"]["latest"] = max(parsed, key=order, default=None)
    result["receipts"]["latestFailed"] = max((item for item in parsed if item["failed"]),
                                              key=order, default=None)
    result["receipts"]["latestSuccessful"] = max(
        (item for item in parsed if not item["failed"] and item["exitCode"] == 0),
        key=order, default=None)
    result["receipts"]["failedRecent"] = sorted(
        (item for item in parsed if item["failed"]), key=order, reverse=True)[:10]
    result["receipts"]["unknownExitRecent"] = sorted(
        (item for item in parsed if item["exitCode"] is None), key=order, reverse=True)[:10]

    active_marker_stems = set()
    for marker in sorted(markers, key=_mtime, reverse=True):
        stem = marker.name[: -len(".running")]
        if stem in valid_receipt_stems:
            note_ignored(marker.name, "stale running marker superseded by a valid receipt")
            continue
        data, problem = _read_bounded_json(marker, STATUS_MAX_MARKER_BYTES)
        started = _number(data.get("started_at")) if isinstance(data, dict) else None
        deadline = _number(data.get("deadline_at")) if isinstance(data, dict) else None
        timeout = _number(data.get("timeout_seconds")) if isinstance(data, dict) else None
        marker_id = data.get("id") if isinstance(data, dict) else None
        log_name = data.get("log") if isinstance(data, dict) else None
        pid = data.get("pid") if isinstance(data, dict) else None
        if problem is not None or started is None or deadline is None or deadline < started \
                or timeout is None or not 0 < timeout <= 604800 \
                or not isinstance(marker_id, str) or not CHECK_ID_RE.fullmatch(marker_id) \
                or not isinstance(log_name, str) or not log_name.endswith(".log") \
                or _safe_basename(log_name) is None:
            note_ignored(marker.name, f"{problem or 'invalid'} running marker")
            continue
        log_path = checks_dir / log_name
        if not inside(log_path, resolved):
            note_ignored(marker.name, "unsafe running marker log identity")
            continue
        now = time.time()
        if result["running"] is None:
            result["running"] = {
                "marker": marker.name, "id": marker_id,
                "pid": _pid_value(pid),
                "pidAlive": _pid_running(pid), "startedAt": started, "deadlineAt": deadline,
                "elapsedMs": int(max(0.0, min(now - started, 10 ** 9)) * 1000), "timeoutSeconds": timeout,
                "deadlinePassed": bool(now > deadline),
                "deadlineScope": "wrapper timeout only; never an inner command deadline",
                "log": log_name, "logEvidence": _tail_evidence(log_path),
                "resourceLimit": sanitize_snapshot(data.get("resource_limit") or data.get("resourceLimit")),
                "uncertain": True,
                "note": "a running marker proves a wrapper attempt started; a live pid is not progress"}
        active_marker_stems.add(stem)

    for log in sorted(logs, key=_mtime, reverse=True):
        if log.stem in valid_receipt_stems or log.stem in active_marker_stems:
            continue
        result["legacyCandidate"] = {
            "log": log.name, "uncertain": True,
            "reason": "no validated receipt and no live running marker; quiet output is not failure",
            "recordedAt": _mtime(log), "logEvidence": _tail_evidence(log),
            "note": "legacy attempt candidate only: not proof of an active, hung or failed check"}
        break

    # Local no-follow resource-guard snapshot. Unknown is never "under budget";
    # a breach is an observation, never a pass or acceptance.
    guard = {"breached": False, "unknown": False, "breaches": [], "unknownScans": [],
             "running": None, "latestReceipt": None,
             "note": "local no-follow byte guard; unknown is not verified and is never under budget"}

    def add_guard(source: str, name, snapshot) -> None:
        if not isinstance(snapshot, dict):
            return
        entry = {"source": source, "name": name, "path": snapshot.get("path"),
                 "maxBytes": snapshot.get("maxBytes"), "observedBytes": snapshot.get("observedBytes"),
                 "breached": bool(snapshot.get("breached")), "unknown": bool(snapshot.get("unknown")),
                 "reason": snapshot.get("reason")}
        if entry["breached"]:
            guard["breached"] = True
            if len(guard["breaches"]) < 5:
                guard["breaches"].append(entry)
        if entry["unknown"]:
            guard["unknown"] = True
            if len(guard["unknownScans"]) < 5:
                guard["unknownScans"].append(entry)

    if isinstance(result["running"], dict):
        add_guard("running", result["running"].get("marker"), result["running"].get("resourceLimit"))
        if isinstance(result["running"].get("resourceLimit"), dict):
            guard["running"] = result["running"]["resourceLimit"]
    for item in parsed:
        add_guard("receipt", item.get("receipt"), item.get("resourceLimit"))
    if isinstance(result["receipts"].get("latest"), dict) \
            and isinstance(result["receipts"]["latest"].get("resourceLimit"), dict):
        guard["latestReceipt"] = result["receipts"]["latest"]["resourceLimit"]
    result["resourceGuard"] = guard
    return result


# ---------------------------------------------------------------------------
# compact observer check (per-observer observation dedup, never acceptance)
# ---------------------------------------------------------------------------

OBSERVER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
OBSERVER_DIR = "observers"
OBSERVER_CURSOR_MAX_BYTES = 65536
OBSERVER_MAX_ALERTS = 200
OBSERVER_MAX_LIST = 20
OBSERVER_LOCK_TIMEOUT = 10.0


def _observer_state_paths(task_dir: Path, observer: str):
    """Cursor/lock paths below the task evidence dir, refusing symlink escapes."""
    if not isinstance(observer, str) or not OBSERVER_RE.fullmatch(observer):
        raise ValueError("observer must be a safe owner thread id (letters, digits, '_' or '-', "
                         "max 100 chars)")
    task_real = task_dir.resolve()
    directory = task_dir / OBSERVER_DIR
    if directory.is_symlink():
        raise ValueError(f"observer directory must not be a symlink: {directory}")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"observer path is not a directory: {directory}")
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.resolve() != task_real / OBSERVER_DIR:
        raise ValueError(f"observer directory escapes the task evidence dir: {directory}")
    cursor = directory / f"{observer}.json"
    lock = directory / f"{observer}.lock"
    for path in (cursor, lock):
        if path.is_symlink():
            raise ValueError(f"observer state file must not be a symlink: {path}")
        if path.exists() and path.resolve() != directory.resolve() / path.name:
            raise ValueError(f"observer state file escapes the task evidence dir: {path}")
    return cursor, lock


def _read_observer_cursor(path: Path, observer: str, task: str):
    """Read a bounded cursor; corrupt/oversized/foreign metadata is unknown, not success."""
    if not path.exists():
        return None, "absent"
    data, problem = _read_bounded_json(path, OBSERVER_CURSOR_MAX_BYTES)
    if problem is not None:
        return None, problem
    if (not isinstance(data, dict) or data.get("schemaVersion") != 1
            or data.get("observer") != observer or data.get("task") != task
            or not isinstance(data.get("signal"), dict)
            or not isinstance(data.get("alerts"), dict)):
        return None, "unrecognized"
    alerts = {}
    for key, meta in list(data["alerts"].items())[:OBSERVER_MAX_ALERTS]:
        if not isinstance(key, str) or len(key) > 400 or not isinstance(meta, dict):
            continue
        first_seen = meta.get("firstSeenAt")
        if isinstance(first_seen, bool) or not isinstance(first_seen, (int, float)):
            first_seen = 0
        alerts[key] = {"kind": _clip(meta.get("kind"), 60), "firstSeenAt": first_seen}
    return {"signal": data["signal"], "alerts": alerts}, None


def _observer_signal(status: dict) -> dict:
    """Meaningful structure only: no mtimes, log sizes, timestamps or token churn."""
    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    running = checks.get("running")
    latest = receipts.get("latest")
    guard = checks.get("resourceGuard") or {}
    resource_sources = sorted({
        f"{entry.get('source')}:{entry.get('name')}" for entry in
        (guard.get("breaches") or []) + (guard.get("unknownScans") or [])
        if isinstance(entry, dict)})
    return {
        "round": status.get("round"),
        "latestRound": status.get("latestRound"),
        "state": status.get("state"),
        "recordedState": status.get("recordedState"),
        "activeWorker": bool((status.get("ownership") or {}).get("activeWorker")),
        "supervisorAlive": bool((status.get("ownership") or {}).get("supervisorAlive")),
        "running": None if not isinstance(running, dict) else {
            "id": running.get("id"), "marker": running.get("marker")},
        "latestReceipt": None if not isinstance(latest, dict) else {
            "id": latest.get("id"), "receipt": latest.get("receipt"),
            "exitCode": latest.get("exitCode"), "timedOut": bool(latest.get("timedOut")),
            "failed": bool(latest.get("failed"))},
        "failedAttempts": receipts.get("failedAttempts"),
        "resource": {"breached": bool(guard.get("breached")),
                     "unknown": bool(guard.get("unknown")),
                     "sources": resource_sources[:20]},
    }


def _observer_alerts(status: dict) -> list:
    """Current actionable facts. Each has a stable key for observation dedup."""
    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    directory = checks.get("dir")
    round_number = status.get("round")
    alerts = []

    def add(key, kind, severity, message, evidence=None, review=False):
        alerts.append({"key": key, "kind": kind, "severity": severity, "round": round_number,
                       "message": _clip(message, 300), "evidence": evidence or {},
                       "reviewRequired": bool(review)})

    def receipt_evidence(name):
        if isinstance(directory, str) and isinstance(name, str):
            return str(Path(directory) / name)
        return None

    for item in receipts.get("failedRecent") or []:
        stem = item.get("receipt")
        if not isinstance(stem, str):
            continue
        evidence = {"checksDir": directory,
                    "receipt": receipt_evidence(stem),
                    "log": receipt_evidence(item.get("log"))}
        if item.get("timedOut"):
            add(f"check-timeout:{stem}", "check_timeout", "high",
                f"check {item.get('id')!r} timed out; the wrapper stopped the owned process group",
                evidence)
        else:
            add(f"check-failed:{stem}", "check_failed", "high",
                f"check {item.get('id')!r} failed with exit {item.get('exitCode')}", evidence)
    for item in receipts.get("unknownExitRecent") or []:
        stem = item.get("receipt")
        if not isinstance(stem, str):
            continue
        add(f"check-unknown:{stem}", "check_unknown", "medium",
            f"check {item.get('id')!r} has an unknown exit code; the receipt is inconclusive",
            {"checksDir": directory, "receipt": receipt_evidence(stem)})
    guard = checks.get("resourceGuard") or {}
    for breach in guard.get("breaches") or []:
        name = breach.get("name") or breach.get("source")
        add(f"resource-breach:{name}", "resource_breach", "high",
            f"resource budget breached: observed {breach.get('observedBytes')} bytes exceed "
            f"max {breach.get('maxBytes')} under {breach.get('path')} ({breach.get('reason')})",
            {"checksDir": directory, "name": name})
    state = status.get("state")
    recorded = status.get("recordedState")
    ownership = status.get("ownership") or {}
    state_evidence = {"state": (status.get("evidence") or {}).get("state")}
    if state == "unknown" and recorded in ACTIVE_STATES:
        add(f"orphan:{round_number}", "orphan", "high",
            "recorded active state without a live supervisor lease; ownership is unknown, "
            "not progress", state_evidence)
    if recorded in TERMINAL_STATES and ownership.get("activeWorker") \
            and not ownership.get("supervisorAlive"):
        add(f"lingering:{round_number}:{recorded}", "lingering_worker", "high",
            "a worker lock is still held after a terminal record; inspect before reuse",
            state_evidence)
    if state in TERMINAL_STATES:
        add(f"round-terminal:{round_number}:{state}", "round_terminal", "high",
            f"round {round_number} reached terminal state {state}; main review is required and "
            "this observation is not acceptance", state_evidence, review=True)
    return alerts


def _compact_checks(status: dict) -> dict:
    """Changed-beat check projection without transcript or log tails."""
    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    running = checks.get("running")
    return {
        "dir": checks.get("dir"), "partial": bool(checks.get("partial")),
        "running": None if not isinstance(running, dict) else {
            "id": running.get("id"), "marker": running.get("marker"),
            "pidAlive": running.get("pidAlive"), "startedAt": running.get("startedAt"),
            "deadlineAt": running.get("deadlineAt"),
            "resourceLimit": running.get("resourceLimit")},
        "receipts": {"total": receipts.get("total"), "scanned": receipts.get("scanned"),
                     "truncated": bool(receipts.get("truncated")),
                     "failedAttempts": receipts.get("failedAttempts"),
                     "latest": receipts.get("latest"),
                     "latestFailed": receipts.get("latestFailed"),
                     "latestSuccessful": receipts.get("latestSuccessful")},
        "legacyCandidate": checks.get("legacyCandidate"),
    }


def build_observer_check(repo_arg: str, task_arg: str, observer_arg: str) -> dict:
    """One compact dedup observation; writes only this observer's cursor."""
    observer = observer_arg
    if not isinstance(observer, str) or not OBSERVER_RE.fullmatch(observer):
        raise ValueError("observer must be a safe owner thread id (letters, digits, '_' or '-', "
                         "max 100 chars)")
    task = require_task_arg(task_arg)
    root = canonical_root(Path(repo_arg))
    common = git_common_dir(root)
    task_dir = task_dir_for(common, task)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task!r} for repository {root}; no evidence at {task_dir}")
    status = build_status(repo_arg, task)
    cursor_path, lock_path = _observer_state_paths(task_dir, observer)
    signal = _observer_signal(status)
    current = _observer_alerts(status)
    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    scan_complete = not checks.get("partial") and not receipts.get("truncated")
    unknown = []
    if not scan_complete:
        unknown.append("the check evidence scan was partial/truncated; missing evidence is "
                       "unknown, not success")
    if not checks.get("exists"):
        unknown.append("no check evidence directory exists yet; absence is missing evidence")

    fd = lock_fd(lock_path, blocking=True, timeout=OBSERVER_LOCK_TIMEOUT)
    try:
        cursor, problem = _read_observer_cursor(cursor_path, observer, task)
        first = cursor is None
        if problem not in (None, "absent"):
            unknown.append(f"observer cursor was {problem}; dedup state was reset and nothing "
                           "was acknowledged")
        stored = cursor["alerts"] if cursor is not None else {}
        stored_signal = cursor["signal"] if cursor is not None else None
        changed = first or stored_signal != signal
        current_keys = {alert["key"] for alert in current}
        new_alerts = [alert for alert in current if alert["key"] not in stored]
        unresolved = [dict(alert, new=False) for alert in current if alert["key"] in stored]
        if not scan_complete:
            # A bounded/partial scan cannot prove a previously seen fact is gone.
            for key, meta in stored.items():
                if key not in current_keys:
                    unresolved.append({
                        "key": key, "kind": meta.get("kind") or "retained", "severity": "medium",
                        "round": status.get("round"),
                        "message": "previously observed fact retained while the bounded scan is "
                                   "incomplete", "evidence": {}, "reviewRequired": False, "new": False})
        now = time.time()
        record = {}
        for alert in current:
            previous = stored.get(alert["key"]) if isinstance(stored.get(alert["key"]), dict) else {}
            record[alert["key"]] = {"kind": alert["kind"],
                                    "firstSeenAt": previous.get("firstSeenAt") or now}
        if not scan_complete:
            for alert in unresolved:
                previous = stored.get(alert["key"]) if isinstance(stored.get(alert["key"]), dict) else {}
                record.setdefault(alert["key"], {"kind": alert.get("kind"),
                                                 "firstSeenAt": previous.get("firstSeenAt") or now})
        if len(record) > OBSERVER_MAX_ALERTS:
            newest = sorted(record.items(), key=lambda item: item[1].get("firstSeenAt") or 0)
            record = dict(newest[-OBSERVER_MAX_ALERTS:])
        atomic(cursor_path, {"schemaVersion": 1, "observer": observer, "task": task,
                             "updatedAt": now, "signal": signal, "alerts": record,
                             "lastObservation": "changed" if changed else "unchanged"})
    finally:
        os.close(fd)

    def compact(alert, is_new: bool) -> dict:
        return {"key": alert.get("key"), "kind": alert.get("kind"),
                "severity": alert.get("severity"), "round": alert.get("round"),
                "new": is_new, "message": _clip(alert.get("message"), 300),
                "reviewRequired": bool(alert.get("reviewRequired")),
                "evidence": alert.get("evidence") or {}}

    guard = checks.get("resourceGuard") or {}
    review_required = status.get("state") in TERMINAL_STATES
    result = {
        "schemaVersion": 1, "command": "check", "observer": observer,
        "task": status.get("task"), "repo": status.get("repo"),
        "round": status.get("round"), "latestRound": status.get("latestRound"),
        "state": status.get("state"), "recordedState": status.get("recordedState"),
        "changed": changed, "firstObservation": first,
        "observation": "changed" if changed else "unchanged",
        "newAlertCount": len(new_alerts), "unresolvedAlertCount": len(unresolved),
        "alerts": [compact(alert, alert["key"] not in stored) for alert in current[:OBSERVER_MAX_LIST]],
        "newAlerts": [compact(alert, True) for alert in new_alerts[:OBSERVER_MAX_LIST]],
        "unresolvedAlerts": [compact(alert, False) for alert in unresolved[:OBSERVER_MAX_LIST]],
        "resources": {"breached": bool(guard.get("breached")), "unknown": bool(guard.get("unknown")),
                      "breaches": guard.get("breaches") or [],
                      "unknownScans": guard.get("unknownScans") or [],
                      "running": guard.get("running"), "latestReceipt": guard.get("latestReceipt")},
        "acceptance": "not_verified", "reviewRequired": review_required,
        "unknown": unknown,
        "dedup": "observation dedup for this observer only; it never acknowledges delivery, "
                 "acceptance, handoff or task ownership",
        "evidence": status.get("evidence"),
        "cursor": {"path": str(cursor_path), "written": True},
    }
    if len(current) > OBSERVER_MAX_LIST:
        result["alertsTruncated"] = len(current) - OBSERVER_MAX_LIST
    if changed:
        result["checks"] = _compact_checks(status)
        result["instruction"] = ("changed evidence: review only the listed new alerts and their exact "
                                 "evidence pointers; terminal observations stay unaccepted until main "
                                 "review; quiet logs are not progress")
    else:
        result["instruction"] = ("unchanged since the previous observation for this observer: do not "
                                 "reread logs and do not wait; nothing new was delivered")
    if not scan_complete:
        result["instruction"] += "; the bounded scan was partial, so absence is unknown, not success"
    if review_required:
        result["instruction"] += "; a terminal round requires main review and is never acceptance"
    return result


def cmd_check(args) -> dict:
    return build_observer_check(args.repo, args.task, args.observer)


def _wait_probe(repo_arg: str, task_arg: str, round_arg=None) -> str:
    """Cheap active-state probe: one state read plus two lock checks, no scans."""
    task = require_task_arg(task_arg)
    root = canonical_root(Path(repo_arg))
    common = git_common_dir(root)
    task_dir = task_dir_for(common, task)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task!r} for repository {root}; no evidence at {task_dir}")
    rounds = list_rounds(task_dir)
    if not rounds:
        raise ValueError(f"task {task!r} has no rounds yet; start it before waiting")
    latest_number = rounds[-1][0]
    selected_number = latest_number if round_arg is None else int(round_arg)
    if selected_number not in [number for number, _ in rounds]:
        raise ValueError(f"round {selected_number} does not exist for task {task!r}")
    selected_dir = task_dir / "rounds" / str(selected_number)
    task_held = lock_is_held(task_dir / ".task.lock")
    supervisor_alive = lock_is_held(task_dir / ".supervisor.lock")
    state = read_json(selected_dir / "round.state.json", None)
    state = state if isinstance(state, dict) else {}
    return effective_state(state, task_held, supervisor_alive, selected_number == latest_number)


def build_status(repo_arg: str, task_arg: str, round_arg=None) -> dict:
    """Bounded read-only state snapshot. Never starts Pi, builds a summary or scans a transcript."""
    task = require_task_arg(task_arg)
    root = canonical_root(Path(repo_arg))
    common = git_common_dir(root)
    task_dir = task_dir_for(common, task)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task!r} for repository {root}; no evidence at {task_dir}")
    frozen = read_json(task_dir / "task.json", None)
    if not isinstance(frozen, dict):
        raise ValueError(f"task {task!r} has no readable task.json under {task_dir}")
    rounds = list_rounds(task_dir)
    if not rounds:
        raise ValueError(f"task {task!r} has no rounds yet; start it before reading status")
    latest_number = rounds[-1][0]
    if round_arg is None:
        selected_number = latest_number
    else:
        selected_number = int(round_arg)
        if selected_number not in [number for number, _ in rounds]:
            raise ValueError(f"round {selected_number} does not exist for task {task!r}")
    selected_dir = task_dir / "rounds" / str(selected_number)
    task_held = lock_is_held(task_dir / ".task.lock")
    supervisor_alive = lock_is_held(task_dir / ".supervisor.lock")
    state = read_json(selected_dir / "round.state.json", None)
    if not isinstance(state, dict):
        state = {}
    raw_state = state.get("state")
    effective = effective_state(state, task_held, supervisor_alive, selected_number == latest_number)
    started = _number(state.get("startedAt"))
    ended = _number(state.get("endedAt"))
    now = time.time()
    elapsed_ms = int(((ended if ended is not None else now) - started) * 1000) if started is not None else None
    timeout = _number(frozen.get("timeoutSeconds"))
    deadline_at = started + timeout if started is not None and timeout is not None else None
    checks = _scan_checks(selected_dir / "round.checks")
    execution_activity = {}
    for label, name in (("roundJsonl", "round.jsonl"), ("roundErr", "round.err")):
        path = selected_dir / name
        try:
            info = path.stat()
            execution_activity[label] = {"path": str(path), "bytes": info.st_size,
                                         "mtime": info.st_mtime}
        except OSError:
            execution_activity[label] = {"path": str(path), "bytes": None, "mtime": None}
    execution_activity["note"] = ("raw execution evidence size/mtime only; a quiet or growing transcript "
                                  "is activity evidence, never useful progress and never acceptance")
    processes = {"taskLockHeld": task_held, "supervisorLeaseHeld": supervisor_alive}
    for key in ("supervisorPid", "piPid"):
        processes[key] = _pid_value(state.get(key))
    notes = ["status is a bounded read-only snapshot; it generates no summary and parses no transcript",
             "process existence and quiet logs are never progress or failure",
             "exit 0 means the Pi process completed execution only; acceptance stays not_verified"]
    if raw_state in ACTIVE_STATES and not supervisor_alive:
        notes.append("the recorded state is active but no supervisor lease is held; ownership is "
                    "unknown, not progress")
    if raw_state in TERMINAL_STATES and task_held and not supervisor_alive:
        notes.append("a Pi descendant may still hold the task lock after a terminal record; inspect "
                     "before reuse")
    if checks["running"] is not None:
        notes.append("the running marker and wrapper deadline come from pi_check; an inner command "
                     "deadline is never inferred from shell syntax")
    if checks["legacyCandidate"] is not None:
        notes.append("the latest unreceipted check log is an explicitly uncertain legacy candidate; "
                     "it is not proof of an active, hung or failed check")
    guard = checks.get("resourceGuard") or {}
    if guard.get("breached"):
        notes.append("a local no-follow resource guard reported a byte-budget breach; that is an "
                     "explicit resource observation, never a pass or acceptance")
    if guard.get("unknown"):
        notes.append("a resource measurement was incomplete or unreadable; budget status is unknown, "
                     "not verified under budget")
    if checks["partial"]:
        notes.append("the checks directory was only partially inspected (bounded subset of entries); "
                     "totals, latest receipts and candidates are not global and may be incomplete")
    elif checks["receipts"]["truncated"]:
        notes.append("receipt parsing was bounded to the newest entries; failed counts and latest "
                     "receipts may be incomplete")
    return {
        "schemaVersion": 1, "task": task, "repo": frozen.get("repo") or str(root),
        "worktree": frozen.get("worktree"), "readOnly": bool(frozen.get("readOnly")),
        "model": frozen.get("model"), "thinking": frozen.get("thinking"),
        "runtimeVersion": frozen.get("runtimeVersion"),
        "session": {"sessionId": frozen.get("sessionId"), "sessionDir": frozen.get("sessionDir")},
        "round": selected_number, "latestRound": latest_number,
        "state": effective, "recordedState": raw_state,
        "startedAt": started, "endedAt": ended, "elapsedMs": elapsed_ms, "deadlineAt": deadline_at,
        "exitCode": state.get("exitCode"), "timedOut": bool(state.get("timedOut")),
        "cancelled": bool(state.get("cancelled")),
        "startHead": state.get("startHead"), "endHead": state.get("endHead") or state.get("head"),
        "briefSha256": state.get("briefSha256"),
        "ownership": {"activeWorker": task_held, "supervisorAlive": supervisor_alive},
        "processes": processes, "executionActivity": execution_activity,
        "checks": checks, "evidence": evidence_paths(task_dir, selected_number),
        "acceptance": "not_verified", "notes": notes,
    }


def build_result(repo_arg: str, task_arg: str, round_arg=None) -> dict:
    task = require_task_arg(task_arg)
    root = canonical_root(Path(repo_arg))
    common = git_common_dir(root)
    task_dir = task_dir_for(common, task)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task!r} for repository {root}; no evidence at {task_dir}")
    frozen = read_json(task_dir / "task.json", None)
    if not isinstance(frozen, dict):
        raise ValueError(f"task {task!r} has no readable task.json under {task_dir}")
    rounds = list_rounds(task_dir)
    if not rounds:
        raise ValueError(f"task {task!r} has no rounds yet; start it before reading a result")
    latest_number = rounds[-1][0]
    if round_arg is None:
        selected_number = latest_number
    else:
        selected_number = int(round_arg)
        if selected_number not in [number for number, _ in rounds]:
            raise ValueError(f"round {selected_number} does not exist for task {task!r}")
    selected_dir = task_dir / "rounds" / str(selected_number)
    task_held = lock_is_held(task_dir / ".task.lock")
    supervisor_alive = lock_is_held(task_dir / ".supervisor.lock")
    compacts = [round_compact(task_dir, number, rdir, task_held, supervisor_alive,
                              number == latest_number) for number, rdir in rounds]
    selected_state = next(item for item in compacts if item["round"] == selected_number)
    selected_raw = (read_json(selected_dir / "round.state.json", {}) or {}).get("state")
    summary_data = load_round_summary(task_dir, frozen, selected_dir, selected_number)
    summary = bounded(summary_data) if isinstance(summary_data, dict) and "source" in summary_data else None
    summary_text = (selected_dir / "round.summary.txt").read_text(encoding="utf-8") \
        if (selected_dir / "round.summary.txt").is_file() else None
    notes = ["exit 0 means the Pi process completed execution; it is never acceptance PASS",
             "acceptance requires the project's own checks and independent main review"]
    if selected_state["state"] == "unknown":
        if selected_raw in ACTIVE_STATES and task_held:
            notes.append("the supervisor is gone but an owned Pi child still holds the task lock; "
                         "the outcome is unknown and no continuation may start")
        else:
            notes.append("the recorded state was active or missing without a live supervisor; "
                         "the outcome is unknown, not success")
    if selected_state["state"] in TERMINAL_STATES and task_held and not supervisor_alive:
        notes.append("a Pi descendant still holds the task lock after a terminal record; "
                     "inspect the recorded processes before reusing anything")
    if summary_data is not None and "error" in summary_data:
        notes.append(summary_data["error"])
    if summary is not None and not summary.get("usage_complete"):
        notes.append("model usage was not fully reported; missing values are unknown, not zero")
    if supervisor_alive:
        notes.append("the supervisor is alive and holds the task lock")
    return {
        "schemaVersion": 1, "task": task, "repo": frozen.get("repo") or str(root),
        "worktree": frozen.get("worktree"),
        "readOnly": bool(frozen.get("readOnly")), "model": frozen.get("model"),
        "thinking": frozen.get("thinking"),
        "session": {"sessionId": frozen.get("sessionId"), "sessionDir": frozen.get("sessionDir")},
        "round": selected_number, "latestRound": latest_number, "state": selected_state["state"],
        "exitCode": selected_state["exitCode"], "timedOut": selected_state["timedOut"],
        "cancelled": selected_state["cancelled"], "activeWorker": task_held,
        "supervisorAlive": supervisor_alive,
        "execution": "completed_execution" if selected_state["state"] == "completed"
        and selected_state["exitCode"] == 0 else selected_state["state"],
        "acceptance": "not_verified",
        "rounds": compacts,
        "summary": summary, "summaryText": summary_text,
        "usageComplete": bool(summary and summary.get("usage_complete")),
        "evidence": evidence_paths(task_dir, selected_number),
        "notes": notes,
        "runtimeVersion": frozen.get("runtimeVersion"),
    }


def cmd_result(args) -> dict:
    return build_result(args.repo, args.task, args.round)


def cmd_status(args) -> dict:
    return build_status(args.repo, args.task, args.round)


def cmd_wait(args) -> dict:
    timeout_ms = args.timeout_ms
    if timeout_ms is None:
        timeout_ms = 60000
    timeout_ms = max(0, min(60000, int(timeout_ms)))
    deadline = time.monotonic() + timeout_ms / 1000
    timed_out = False
    state = None
    while True:
        state = _wait_probe(args.repo, args.task, args.round)
        if state not in ACTIVE_STATES:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(min(0.3, max(0.05, deadline - time.monotonic())))
    wait = {"timeoutMs": timeout_ms, "timedOut": timed_out,
            "note": "waiting never cancels the worker; repeat bounded waits only when needed",
            "instruction": ("repeat short bounded waits only when needed; process user steering between "
                            "calls; avoid verbose unchanged narration; collect the full result once when "
                            "the round is delivered")}
    if state in TERMINAL_STATES:
        result = build_result(args.repo, args.task, args.round)
        result["wait"] = wait
        return result
    status = build_status(args.repo, args.task, args.round)
    status["wait"] = wait
    return status


def cmd_cancel(args) -> dict:
    task = require_task_arg(args.task)
    root = canonical_root(Path(args.repo))
    common = git_common_dir(root)
    task_dir = task_dir_for(common, task)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task!r}; no evidence at {task_dir}")
    rounds = list_rounds(task_dir)
    if not rounds:
        raise ValueError(f"task {task!r} has no rounds yet")
    latest_number = rounds[-1][0]
    latest_dir = task_dir / "rounds" / str(latest_number)
    active = lock_is_held(task_dir / ".task.lock")
    supervised = lock_is_held(task_dir / ".supervisor.lock")
    if supervised and not active:
        # An exiting supervisor may briefly outlive its task lock.
        exit_deadline = time.monotonic() + 1
        while time.monotonic() < exit_deadline and lock_is_held(task_dir / ".supervisor.lock"):
            time.sleep(0.05)
        supervised = lock_is_held(task_dir / ".supervisor.lock")
    if active and not supervised:
        raw_latest = (read_json(latest_dir / "round.state.json", {}) or {}).get("state")
        if raw_latest == "starting":
            # A worker may be between process spawn and lease acquisition.
            lease_deadline = time.monotonic() + 3
            while time.monotonic() < lease_deadline and not lock_is_held(task_dir / ".supervisor.lock"):
                time.sleep(0.05)
            supervised = lock_is_held(task_dir / ".supervisor.lock")
    if not active and not supervised:
        result = build_result(args.repo, task)
        result["cancel"] = {"request": "not_active", "round": latest_number,
                            "note": "no owned supervisor or worker holds the task lock; nothing was signaled. "
                                    "A vanished worker's outcome stays unknown. This is not acceptance."}
        return result
    if not supervised:
        result = build_result(args.repo, task)
        result["cancel"] = {"request": "orphaned", "round": latest_number,
                            "note": "the supervisor is gone; an owned Pi child may still hold the task lock. "
                                    "This runtime does not signal unverified PIDs and does not auto-clean orphans; "
                                    "inspect the recorded process evidence before removing anything. "
                                    "The outcome stays unknown and is not acceptance."}
        return result
    nonce = uuid.uuid4().hex
    atomic(task_dir / "cancel.json", {"schemaVersion": 1, "task": task, "round": latest_number,
                                      "requestedAt": time.time(), "nonce": nonce})
    deadline = time.monotonic() + 3
    observed = False
    while time.monotonic() < deadline:
        recorded = read_json(task_dir / "cancel.observed", {}) or {}
        if recorded.get("round") == latest_number:
            observed = True
            break
        if not lock_is_held(task_dir / ".task.lock"):
            observed = True
            break
        time.sleep(0.1)
    result = build_result(args.repo, task)
    result["cancel"] = {"request": "observed" if observed else "requested", "round": latest_number,
                        "nonce": nonce, "observed": observed,
                        "note": "the owned worker stops its Pi process group; empty exit means unknown, never success"}
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_project(args) -> dict:
    root = canonical_root(Path(args.repo))
    config = load_config(root)
    common = git_common_dir(root)
    state = state_root(common)
    busy = active_tasks(state)
    return {"ok": True, "schemaVersion": 1, "repo": str(root), "gitCommonDir": str(common),
            "configPath": config["path"],
            "config": {key: config[key] for key in CONFIG_KEYS if key in config},
            "capabilities": {
                "piExecutable": os.environ.get("PI_BIN") or "pi",
                "readOnlyTools": READ_ONLY_TOOLS.split(","),
                "writableTools": WRITABLE_TOOLS.split(","),
                "worktreeRule": "existing linked checkout of this repository, never the configured "
                                "checkout passed as --repo and never the Git primary checkout; one writer "
                                "per task and worktree; claims last for the task lifetime and are not "
                                "auto-released, so use a new linked checkout for each new task",
                "evidenceRoot": str(state / "tasks"),
                "configCheckout": str(root),
            },
            "limits": {"maxWorkers": config["maxWorkers"], "timeoutSeconds": config["timeoutSeconds"],
                       "model": config["model"], "thinking": config["thinking"],
                       "allowedModels": [DEFAULT_MODEL],
                       "modelPolicy": "only deepseek/deepseek-flash is permitted; other IDs are "
                                      "rejected, never substituted or defaulted",
                       "readOnlyIsNotASecuritySandbox": True, "codexCliInvocations": 0},
            "activeTasks": busy[:20], "activeTaskCount": len(busy),
            "note": "configuration references are instructions only; this runtime executes no configured commands"}


def add_repo_task(parser):
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi_task.py",
        description="Persistent Pi worker lifecycle for the Codex-Pi plugin. "
                    "Never invokes the Codex CLI. The Codex main session calls these "
                    "subcommands through its shell.")
    sub = parser.add_subparsers(dest="command", required=True)

    project = sub.add_parser("project", help="resolve repo root, config, capabilities and limits")
    project.add_argument("--repo", required=True)
    project.set_defaults(func=cmd_project)

    start = sub.add_parser("start", help="create a fresh task, immutable round 1 and a detached worker")
    add_repo_task(start)
    start.add_argument("--worktree", required=True)
    start.add_argument("--prompt-file")
    start.add_argument("--prompt")
    start.add_argument("--read-only", action="store_true")
    start.set_defaults(func=cmd_start)

    cont = sub.add_parser("continue", help="continue a terminal-known task in its pinned session")
    add_repo_task(cont)
    cont.add_argument("--prompt-file")
    cont.add_argument("--prompt")
    cont.set_defaults(func=cmd_continue)

    result = sub.add_parser("result", help="bounded latest state and evidence pointers")
    add_repo_task(result)
    result.add_argument("--round", type=int)
    result.set_defaults(func=cmd_result)

    status = sub.add_parser("status", help="fast read-only round/check snapshot (no summary generation)")
    add_repo_task(status)
    status.add_argument("--round", type=int)
    status.set_defaults(func=cmd_status)

    check = sub.add_parser("check", help="compact per-observer dedup observation "
                                          "(writes only its own cursor)")
    add_repo_task(check)
    check.add_argument("--observer", required=True,
                       help="owner thread id whose private observation cursor is used")
    check.set_defaults(func=cmd_check)

    wait = sub.add_parser("wait", help="bounded internal wait for a terminal state (never cancels)")
    add_repo_task(wait)
    wait.add_argument("--round", type=int)
    wait.add_argument("--timeout-ms", type=int, default=60000)
    wait.set_defaults(func=cmd_wait)

    cancel = sub.add_parser("cancel", help="request cancellation of the owned worker's process group")
    add_repo_task(cancel)
    cancel.set_defaults(func=cmd_cancel)

    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--task-dir", required=True)
    worker.add_argument("--round", type=int, required=True)
    worker.add_argument("--lock-fd", type=int, required=True)
    worker.add_argument("--timeout-seconds", type=float, required=True)
    worker.set_defaults(func=run_worker)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "_worker":
        try:
            return run_worker(args)
        finally:
            try:
                os.close(args.lock_fd)
            except OSError:
                pass
    result = args.func(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, LockHeld, OSError, subprocess.CalledProcessError) as exc:
        print(f"pi_task: {exc}", file=sys.stderr)
        raise SystemExit(2)
