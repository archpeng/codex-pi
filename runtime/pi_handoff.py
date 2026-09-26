#!/usr/bin/env python3
"""Bounded Codex Stop handoff for explicit Codex-Pi review cycles.

The Codex main session explicitly *arms* an existing Pi task round for its own
Codex session. Later, when that Codex turn stops, the synchronous Stop hook for
the same session performs exactly one quick read-only check and, if the round is
already terminal with released ownership, returns a single continuation prompt
with validated identifiers and result/ack commands. It never waits for an active
Pi task; pending events stay armed for a later Stop. Pi output is never copied
into the hook reason.

Legacy records with the old long ``waitSeconds`` remain readable, and the
owner-scoped ``release`` operation lets a main session migrate an already-waiting
0.2 hook without cancelling Pi: the record moves to ``suspended`` with the
explicit reason ``released for bounded tool waiting`` and a new generation. The
waiting hook observes that generation/state change on its next poll and exits;
the round keeps running.

This module never invokes the Codex CLI and never starts a model. It only reads
the existing ``pi_task`` lifecycle evidence and writes its own binding records
under ``${CODEX_PI_HANDOFF_ROOT:-${CODEX_HOME:-~/.codex}/codex-pi/handoffs}``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from pi_task import (  # noqa: E402
    ACTIVE_STATES,
    DEFAULT_MODEL,
    TERMINAL_STATES,
    TASK_RE,
    LockHeld,
    atomic,
    build_result,
    canonical_root,
    git_common_dir,
    list_rounds,
    lock_fd,
    lock_is_held,
    read_json,
    require_allowed_model,
    require_task_arg,
    task_dir_for,
)

SCHEMA_VERSION = 1
# New records carry waitSeconds 0: the Stop hook performs one quick pass and
# never waits for an active Pi task. MAX_WAIT_SECONDS only keeps legacy 0.2
# records (which may still say 14400) readable and validates old evidence.
DEFAULT_WAIT_SECONDS = 0
MAX_WAIT_SECONDS = 14400
MAX_EVENT_BYTES = 1_000_000
MAX_REASON_BYTES = 6000
MAX_MESSAGE_BYTES = 900
INTERRUPT_BUDGET_SECONDS = 0.75
RELEASE_REASON = "released for bounded tool waiting"
LIVE_STATES = ("armed", "suspended", "delivered")
RESUMABLE_STATES = ("suspended", "expired", "needs_recovery")
VALID_STATES = ("armed", "delivered", "acked", "suspended", "expired", "needs_recovery", "stale")
SESSION_RE = re.compile(r"[^\x00-\x1f\x7f]{1,200}\Z")
EVENT_KEY_RE = re.compile(r"[0-9a-f]{64}\Z")


def bounded(text, limit: int = MAX_MESSAGE_BYTES) -> str:
    text = str(text).replace("\x00", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# persistence layout
# ---------------------------------------------------------------------------

def handoff_root() -> Path:
    override = os.environ.get("CODEX_PI_HANDOFF_ROOT")
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root
    home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    return home / "codex-pi" / "handoffs"


def bindings_dir(root: Path) -> Path:
    return root / "bindings"


def sessions_dir(root: Path) -> Path:
    return root / "sessions"


def binding_path(root: Path, key: str) -> Path:
    return bindings_dir(root) / f"{key}.json"


def binding_lock_path(root: Path, key: str) -> Path:
    return bindings_dir(root) / f"{key}.lock"


def session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def session_marker_path(root: Path, session_id: str) -> Path:
    return sessions_dir(root) / f"{session_key(session_id)}.json"


def session_index_dir(root: Path, session_id: str) -> Path:
    return sessions_dir(root) / session_key(session_id)


def index_add(root: Path, session_id: str, key: str) -> None:
    directory = session_index_dir(root, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / key).touch(exist_ok=True)


def index_event_keys(root: Path, session_id: str) -> list:
    directory = session_index_dir(root, session_id)
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir() if entry.is_file())


def event_key(session_id: str, repo: Path, task: str, round_number: int) -> str:
    raw = "\x00".join((session_id, str(repo), task, str(int(round_number))))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_session_id(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session id must be a non-empty string (use --session-id or CODEX_THREAD_ID)")
    value = value.strip()
    if not SESSION_RE.fullmatch(value):
        raise ValueError("session id must be at most 200 printable characters")
    return value


def list_bindings(root: Path) -> list:
    result = []
    directory = bindings_dir(root)
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.json")):
        data = read_json(path, None)
        if isinstance(data, dict):
            result.append(data)
    return result


def session_binding_records(root: Path, session_id: str) -> list:
    """(event_key, record_or_None) for one session; corrupted files stay visible."""
    records = []
    seen = set()
    for key in index_event_keys(root, session_id):
        if not EVENT_KEY_RE.fullmatch(key):
            continue
        seen.add(key)
        records.append((key, read_json(binding_path(root, key), None)))
    for item in list_bindings(root):
        if item.get("sessionId") != session_id:
            continue
        key = item.get("eventKey")
        if isinstance(key, str) and key not in seen:
            records.append((key, item))
    return records


def validate_binding(binding, expected_key=None):
    """Full record validation shared by every consumption path.

    Returns ``(record, None)`` or ``(None, reason)``. The event key must stay
    derivable from the immutable session/repo/task/round identity and match the
    filename the record was read from.
    """
    if not isinstance(binding, dict):
        return None, "binding is not a JSON object"
    if binding.get("schemaVersion") != SCHEMA_VERSION:
        return None, f"schemaVersion must be {SCHEMA_VERSION}"
    key = binding.get("eventKey")
    if not isinstance(key, str) or not EVENT_KEY_RE.fullmatch(key):
        return None, "eventKey is missing or not a 64-character hex digest"
    if expected_key is not None and key != expected_key:
        return None, "eventKey does not match the binding filename"
    try:
        session = validate_session_id(binding.get("sessionId"))
    except ValueError as exc:
        return None, str(exc)
    repo_raw = binding.get("repo")
    if not isinstance(repo_raw, str) or not repo_raw.strip():
        return None, "repo is missing"
    repo = Path(repo_raw).expanduser()
    if not repo.is_absolute():
        return None, "repo must be an absolute path"
    repo = repo.resolve()
    task = binding.get("task")
    if not isinstance(task, str) or not TASK_RE.fullmatch(task):
        return None, "task is not a valid identifier"
    round_number = binding.get("round")
    if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
        return None, "round must be a positive integer"
    if event_key(session, repo, task, round_number) != key:
        return None, "eventKey does not match the stored session/repo/task/round identity"
    state = binding.get("state")
    if state not in VALID_STATES:
        return None, f"unknown binding state {state!r}"
    wait_seconds = binding.get("waitSeconds")
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) \
            or not (0 <= float(wait_seconds) <= MAX_WAIT_SECONDS):
        return None, "waitSeconds is not a bounded number"
    armed_at = binding.get("armedAt")
    if isinstance(armed_at, bool) or not isinstance(armed_at, (int, float)):
        return None, "armedAt is not a number"
    if binding.get("frozenModel") != DEFAULT_MODEL:
        return None, f"frozenModel must be {DEFAULT_MODEL!r}"
    return binding, None


def mark_binding_state(root: Path, key: str, state: str, error=None,
                      expect_generation=None, expect_armed: bool = False):
    """Locked compare-and-set state transition.

    Never demotes a delivered or acked record, and never overwrites a newer
    resume (different generation). Returns the updated record or None when the
    transition was refused. Never called while holding another binding lock.
    """
    updates = {"state": state, "lastCheckedAt": time.time(),
               "lastError": bounded(error or "", 400)}
    if state == "suspended":
        updates["suspendedAt"] = time.time()
    try:
        fd = lock_fd(binding_lock_path(root, key), blocking=True, timeout=2)
    except LockHeld:
        return None
    try:
        path = binding_path(root, key)
        data = read_json(path, None)
        if not isinstance(data, dict):
            return None
        if data.get("state") in ("delivered", "acked"):
            return None
        if expect_armed and data.get("state") != "armed":
            return None
        if expect_generation is not None and binding_generation(data) != expect_generation:
            return None
        data.update(updates)
        atomic(path, data)
        return data
    finally:
        os.close(fd)


def binding_generation(binding: dict) -> int:
    generation = binding.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return 0
    return generation


def observed_interrupt_generation(binding: dict) -> int:
    observed = binding.get("observedInterruptGeneration")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        return 0
    return observed


def session_interrupt_generation(root: Path, session_id: str) -> int:
    marker = read_json(session_marker_path(root, session_id), None)
    if not isinstance(marker, dict):
        return 0
    generation = marker.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return 1 if "suspendedAt" in marker else 0
    return generation


def interrupted_since(root: Path, session_id: str, binding: dict) -> bool:
    return session_interrupt_generation(root, session_id) > observed_interrupt_generation(binding)


def binding_repo(binding: dict):
    raw = binding.get("repo")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def evidence_paths(repo: Path, task: str, round_number):
    try:
        common = git_common_dir(repo)
        task_dir = task_dir_for(common, task)
    except (ValueError, OSError):
        return None, None
    round_dir = task_dir / "rounds" / str(round_number)
    return task_dir, round_dir


def collect_command(repo: Path, task: str, round_number: int) -> str:
    task_dir, _ = evidence_paths(repo, task, round_number)
    helper = (task_dir / "tools" / "pi_task.py") if task_dir is not None \
        else Path(__file__).resolve().parent / "pi_task.py"
    return (f"python3 {shlex.quote(str(helper))} result --repo {shlex.quote(str(repo))} "
            f"--task {shlex.quote(task)} --round {int(round_number)}")


def ack_command(key: str) -> str:
    return f"python3 {shlex.quote(str(Path(__file__).resolve()))} ack --event-key {key}"


def status_command(key: str) -> str:
    return f"python3 {shlex.quote(str(Path(__file__).resolve()))} status --event-key {key}"


def compact_binding(binding: dict, root: Path) -> dict:
    repo = binding_repo(binding)
    task = binding.get("task") if isinstance(binding.get("task"), str) else None
    round_number = binding.get("round")
    round_number = round_number if isinstance(round_number, int) and not isinstance(round_number, bool) else None
    task_dir = round_dir = None
    live = None
    if repo is not None and task is not None and round_number is not None:
        task_dir, round_dir = evidence_paths(repo, task, round_number)
        state = read_json(round_dir / "round.state.json", None) if round_dir is not None else None
        if isinstance(state, dict):
            live = {"roundState": state.get("state"), "exitCode": state.get("exitCode"),
                    "timedOut": bool(state.get("timedOut")), "cancelled": bool(state.get("cancelled"))}
    key = binding.get("eventKey")
    commands = {}
    if isinstance(key, str) and EVENT_KEY_RE.fullmatch(key):
        commands = {"status": status_command(key), "ack": ack_command(key)}
    if repo is not None and task is not None and round_number is not None:
        commands["result"] = collect_command(repo, task, round_number)
    return {
        "eventKey": key, "sessionId": binding.get("sessionId"),
        "repo": str(repo) if repo is not None else binding.get("repo"),
        "task": task, "round": round_number,
        "state": binding.get("state"), "armedAt": binding.get("armedAt"),
        "generation": binding.get("generation"),
        "observedInterruptGeneration": binding.get("observedInterruptGeneration"),
        "waitSeconds": binding.get("waitSeconds"), "attempts": binding.get("attempts"),
        "deliveredAt": binding.get("deliveredAt"), "ackedAt": binding.get("ackedAt"),
        "ackedFromState": binding.get("ackedFromState"), "suspendedAt": binding.get("suspendedAt"),
        "releaseReason": binding.get("releaseReason"), "releasedAt": binding.get("releasedAt"),
        "lastWaitExpiredAt": binding.get("lastWaitExpiredAt"),
        "lastCheckedAt": binding.get("lastCheckedAt"), "lastError": binding.get("lastError"),
        "delivery": binding.get("delivery"), "liveRoundState": live,
        "evidence": {
            "taskDir": str(task_dir) if task_dir is not None else None,
            "roundDir": str(round_dir) if round_dir is not None else None,
            "state": str(round_dir / "round.state.json") if round_dir is not None else None,
            "summary": str(round_dir / "round.summary.json") if round_dir is not None else None,
            "checksDir": str(round_dir / "round.checks") if round_dir is not None else None,
        },
        "commands": commands,
        "acceptance": "not_verified",
    }


# ---------------------------------------------------------------------------
# arm
# ---------------------------------------------------------------------------

def cmd_arm(args) -> dict:
    repo = canonical_root(Path(args.repo))
    common = git_common_dir(repo)
    task = require_task_arg(args.task)
    round_number = int(args.round)
    if round_number < 1:
        raise ValueError("round must be a positive integer")
    task_dir = task_dir_for(common, task)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task!r} for repository {repo}; no evidence at {task_dir}")
    frozen = read_json(task_dir / "task.json", None)
    if not isinstance(frozen, dict):
        raise ValueError(f"task {task!r} has no readable task.json; inspect {task_dir}")
    if frozen.get("task") != task:
        raise ValueError(f"task identity mismatch: task.json records {frozen.get('task')!r}")
    frozen_repo = frozen.get("repo")
    if not isinstance(frozen_repo, str) or not frozen_repo.strip():
        raise ValueError(f"task {task!r} has no frozen repository identity")
    if Path(frozen_repo).expanduser().resolve() != repo:
        raise ValueError(f"task {task!r} belongs to checkout {frozen_repo}, not {repo}")
    frozen_common = frozen.get("commonDir")
    if isinstance(frozen_common, str) and frozen_common.strip() \
            and Path(frozen_common).expanduser().resolve() != common:
        raise ValueError(f"task {task!r} belongs to git common dir {frozen_common}, not {common}")
    try:
        require_allowed_model(frozen.get("model"), "handoff frozen task")
    except ValueError as exc:
        raise ValueError(f"task {task!r} is not eligible for handoff: {exc}") from None
    try:
        from pi_board import board_file_for_common, read_board
        board_file = board_file_for_common(common)
        if board_file.is_file():
            board, _problem = read_board(board_file)
            card = (board or {}).get("cards", {}).get(task)
            if isinstance(card, dict) and card.get("transport") == "cli-queue":
                raise ValueError(
                    f"task {task!r} is registered with the cli-queue transport; the board queue "
                    "owns notifications, so the legacy Stop handoff is not armed for it")
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - board read trouble must not change legacy arming
        pass
    numbers = [number for number, _ in list_rounds(task_dir)]
    if round_number not in numbers:
        raise ValueError(f"unknown round {round_number} for task {task!r}; known rounds: {numbers}")
    if round_number != numbers[-1]:
        raise ValueError(f"round {round_number} is stale for task {task!r}; latest round is {numbers[-1]}; "
                         "arm the latest round only")
    round_dir = task_dir / "rounds" / str(round_number)
    state = read_json(round_dir / "round.state.json", None)
    if not isinstance(state, dict):
        raise ValueError(f"round {round_number} has no readable round.state.json; stale evidence, inspect {round_dir}")
    if state.get("round") not in (None, round_number):
        raise ValueError(f"round identity mismatch: state records {state.get('round')!r}")
    recorded_dir = state.get("taskDir")
    if isinstance(recorded_dir, str) and recorded_dir.strip() and Path(recorded_dir).resolve() != task_dir.resolve():
        raise ValueError("round state taskDir does not match the frozen task directory; stale evidence")

    session = args.session_id if args.session_id else os.environ.get("CODEX_THREAD_ID")
    session = validate_session_id(session)
    wait_seconds = DEFAULT_WAIT_SECONDS if args.wait_seconds is None else float(args.wait_seconds)
    if wait_seconds > 0:
        raise ValueError(
            "a positive Stop wait window is no longer supported: the Stop hook performs one quick "
            "check and leaves pending events armed instead of waiting for an active Pi task; "
            "omit --wait-seconds (legacy records remain readable) and use bounded pi_task.py wait/status "
            "from ordinary tool calls")
    if wait_seconds < 0:
        raise ValueError("--wait-seconds must not be negative")

    root = handoff_root()
    key = event_key(session, repo, task, round_number)
    # Capture the interrupt generation before any blocking work: an interrupt
    # that happens while this arm waits for locks must not be erased by the
    # later armedAt timestamp.
    observed_generation = session_interrupt_generation(root, session)
    # Shared admission lock plus the binding lock serialize the check/read/write
    # with ack, delivery and Interrupt (consistent order: admission, binding).
    admission = lock_fd(root / ".admission.lock", blocking=True, timeout=30)
    try:
        binding_lock = lock_fd(binding_lock_path(root, key), blocking=True, timeout=10)
        try:
            path = binding_path(root, key)
            existing = None
            if path.exists():
                raw = read_json(path, None)
                if not isinstance(raw, dict):
                    raise ValueError(f"existing binding {path} is corrupt; inspect it explicitly before re-arming")
                existing, reason = validate_binding(raw, key)
                if existing is None:
                    raise ValueError(f"existing binding {key} is invalid ({reason}); "
                                     "inspect it explicitly before re-arming")
                state = existing.get("state")
                if state in ("armed", "delivered"):
                    return {"ok": True, "eventKey": key, "idempotent": True, "resumed": False,
                            "binding": compact_binding(existing, root),
                            "note": "this session already armed the same repo/task/round; no duplicate notification"}
                if state == "acked":
                    return {"ok": True, "eventKey": key, "idempotent": True, "resumed": False,
                            "binding": compact_binding(existing, root),
                            "note": "this exact event was already collected and acknowledged; not re-armed"}
                if not getattr(args, "resume", False):
                    raise ValueError(f"existing handoff {key} is {state!r}; explicit --resume is required "
                                     "to re-arm an expired, suspended or needs_recovery event")
                if state not in RESUMABLE_STATES:
                    raise ValueError(f"existing handoff {key} state {state!r} cannot be resumed; "
                                     "inspect it explicitly")

            for other in list_bindings(root):
                if other.get("eventKey") == key:
                    continue
                if (other.get("repo") == str(repo) and other.get("task") == task
                        and other.get("round") == round_number and other.get("sessionId") != session
                        and other.get("state") in LIVE_STATES):
                    raise ValueError(f"live handoff {other.get('eventKey')} for this repo/task/round belongs to "
                                     f"session {other.get('sessionId')!r}; ack it before rebinding another session")

            now = time.time()
            resumed = existing is not None
            previous_error = (existing or {}).get("lastError") if resumed else None
            interrupted = session_interrupt_generation(root, session) > observed_generation
            record = {
                "schemaVersion": SCHEMA_VERSION, "eventKey": key, "sessionId": session,
                "repo": str(repo), "commonDir": str(common), "task": task, "round": round_number,
                "worktree": frozen.get("worktree"), "frozenModel": frozen.get("model"),
                "generation": (binding_generation(existing) + 1) if resumed else 1,
                "observedInterruptGeneration": observed_generation,
                "createdAt": existing.get("createdAt", now) if resumed else now,
                "armedAt": now, "waitSeconds": wait_seconds,
                "state": "suspended" if interrupted else "armed", "attempts": 0,
                "lastCheckedAt": None,
                "lastError": "user interrupted this Codex session while arm was in flight; "
                             "re-arm with --resume" if interrupted else None,
                "lastWaitExpiredAt": None,
                "deliveredAt": None, "delivery": None,
                "suspendedAt": now if interrupted else None,
                "ackedAt": None, "ackedFromState": None, "ackNote": None,
                "rearmCount": (int(existing.get("rearmCount") or 0) + 1) if resumed else 0,
            }
            if resumed and previous_error:
                record["previousError"] = previous_error
            atomic(path, record)
            index_add(root, session, key)
            return {"ok": True, "eventKey": key, "idempotent": False, "resumed": resumed,
                    "interrupted": interrupted,
                    "binding": compact_binding(record, root),
                    "note": ("interrupt observed during arm; event is suspended, re-arm explicitly with "
                             "--resume" if interrupted else
                             "armed for the next Stop of this Codex session; exit 0 is never acceptance PASS")}
        finally:
            os.close(binding_lock)
    finally:
        os.close(admission)


# ---------------------------------------------------------------------------
# status / ack
# ---------------------------------------------------------------------------

def cmd_status(args) -> dict:
    root = handoff_root()
    selected = []
    invalid = []
    if args.event_key:
        key = str(args.event_key)
        if not EVENT_KEY_RE.fullmatch(key):
            raise ValueError("event-key must be a 64-character lowercase hex digest")
        raw = read_json(binding_path(root, key), None)
        binding, reason = validate_binding(raw, key)
        if binding is None:
            raise ValueError(f"handoff event {key} is invalid or unknown: {reason}")
        selected = [binding]
    else:
        session = args.session_id or os.environ.get("CODEX_THREAD_ID")
        if session:
            session = validate_session_id(session)
        if args.repo:
            args.repo = str(canonical_root(Path(args.repo)))
        if args.task:
            args.task = require_task_arg(args.task)
        if args.round is not None and int(args.round) < 1:
            raise ValueError("round must be a positive integer")
        if not session and not args.all and not args.repo and not args.task and args.round is None:
            raise ValueError("provide --event-key, --all, or at least one selector "
                             "(--session-id/CODEX_THREAD_ID, --repo, --task, --round)")
        directory = bindings_dir(root)
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                key = path.stem
                raw = read_json(path, None)
                binding, reason = validate_binding(raw, key)
                if binding is None:
                    if args.all:
                        invalid.append({"eventKey": key, "state": "invalid", "error": bounded(reason, 300)})
                    continue
                if session and binding.get("sessionId") != session:
                    continue
                if args.repo and binding.get("repo") != args.repo:
                    continue
                if args.task and binding.get("task") != args.task:
                    continue
                if args.round is not None and binding.get("round") != int(args.round):
                    continue
                selected.append(binding)
    selected.sort(key=lambda item: (item.get("armedAt") or 0, str(item.get("eventKey"))))
    invalid.sort(key=lambda item: item["eventKey"])
    output = [compact_binding(item, root) for item in selected] + invalid
    return {"ok": True, "count": len(output), "bindings": output,
            "note": "status is bounded recovery evidence only; it never delivers, re-arms or accepts"}


def cmd_ack(args) -> dict:
    root = handoff_root()
    session = args.session_id or os.environ.get("CODEX_THREAD_ID")
    if not session:
        raise ValueError("ack requires a resolved session: pass --session-id or set CODEX_THREAD_ID")
    session = validate_session_id(session)
    if args.event_key:
        key = str(args.event_key)
        if not EVENT_KEY_RE.fullmatch(key):
            raise ValueError("event-key must be a 64-character lowercase hex digest")
        if args.repo:
            args.repo = str(canonical_root(Path(args.repo)))
        if args.task:
            args.task = require_task_arg(args.task)
        if args.round is not None and int(args.round) < 1:
            raise ValueError("round must be a positive integer")
    else:
        if not (args.repo and args.task and args.round is not None):
            raise ValueError("ack needs --event-key or --repo --task --round together with "
                             "--session-id/CODEX_THREAD_ID")
        repo = canonical_root(Path(args.repo))
        task = require_task_arg(args.task)
        round_number = int(args.round)
        if round_number < 1:
            raise ValueError("round must be a positive integer")
        key = event_key(session, repo, task, round_number)
        args = argparse.Namespace(event_key=key, repo=str(repo), task=task, round=round_number,
                                  session_id=session, note=args.note)
    fd = lock_fd(binding_lock_path(root, key), blocking=True, timeout=10)
    try:
        path = binding_path(root, key)
        raw = read_json(path, None)
        binding, reason = validate_binding(raw, key)
        if binding is None:
            raise ValueError(f"handoff event {key} is invalid or unknown: {reason}")
        if binding.get("sessionId") != session:
            raise ValueError("ack session does not match the stored event session; "
                             "pass the exact --session-id of the event owner")
        if args.repo and binding.get("repo") != args.repo:
            raise ValueError(f"event identity mismatch: repo is {binding.get('repo')!r}")
        if args.task and binding.get("task") != args.task:
            raise ValueError(f"event identity mismatch: task is {binding.get('task')!r}")
        if args.round is not None and binding.get("round") != int(args.round):
            raise ValueError(f"event identity mismatch: round is {binding.get('round')!r}")
        if args.event_key and binding.get("eventKey") != str(args.event_key):
            raise ValueError("stored binding does not match the requested event key")
        already = binding.get("state") == "acked"
        if not already:
            if binding.get("state") != "delivered":
                raise ValueError(f"handoff state {binding.get('state')!r} is not delivered; ack only after "
                                 "delivery (an armed or recovery event is never suppressed by ack)")
            binding["ackedFromState"] = binding.get("state")
            binding["state"] = "acked"
            binding["ackedAt"] = time.time()
            if args.note:
                binding["ackNote"] = bounded(args.note, 500)
            atomic(path, binding)
        return {"ok": True, "alreadyAcked": already, "eventKey": key, "ackAt": binding.get("ackedAt"),
                "binding": compact_binding(binding, root),
                "note": "acknowledgement marks result collection only; it is never acceptance"}
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# release (migrate a waiting 0.2 hook without cancelling Pi)
# ---------------------------------------------------------------------------

def cmd_release(args) -> dict:
    """Owner-scoped, idempotent release of one armed waiting event.

    The old 0.2 Stop hook holds ``state == 'armed'`` and blocks until terminal or
    its (possibly legacy 14400-second) wait deadline. Release performs a locked
    compare-and-set to ``suspended`` with the explicit reason
    ``released for bounded tool waiting`` and a bumped generation, so a running
    0.2 hook observes the generation/state change on its next poll and exits its
    wait. Pi keeps running, no session interrupt marker is written, no delivery
    or acceptance is forged, and delivered/acked/terminal records are preserved.
    """
    root = handoff_root()
    key = str(args.event_key)
    if not EVENT_KEY_RE.fullmatch(key):
        raise ValueError("event-key must be a 64-character lowercase hex digest")
    session = args.session_id or os.environ.get("CODEX_THREAD_ID")
    if not session:
        raise ValueError("release requires the event owner session: pass --session-id or set "
                         "CODEX_THREAD_ID")
    session = validate_session_id(session)
    fd = lock_fd(binding_lock_path(root, key), blocking=True, timeout=10)
    try:
        path = binding_path(root, key)
        raw = read_json(path, None)
        binding, reason = validate_binding(raw, key)
        if binding is None:
            raise ValueError(f"handoff event {key} is invalid or unknown: {reason}")
        if binding.get("sessionId") != session:
            raise ValueError("release session does not match the stored event owner; pass the exact "
                             "--session-id of the event owner")
        state = binding.get("state")
        if state in ("delivered", "acked") or binding.get("deliveredAt") or binding.get("ackedAt"):
            raise ValueError(f"handoff event {key} is {state!r}; terminal delivered/acked evidence must "
                             "not be erased by release")
        if state == "suspended" and binding.get("releaseReason") == RELEASE_REASON:
            return {"ok": True, "alreadyReleased": True, "eventKey": key,
                    "binding": compact_binding(binding, root),
                    "note": "this event was already released for bounded tool waiting; no duplicate write"}
        if state != "armed":
            raise ValueError(f"handoff event {key} is {state!r}; release only migrates an armed waiting "
                             "hook (recovery states keep their explicit --resume requirement)")
        now = time.time()
        binding.update(state="suspended", suspendedAt=now, lastCheckedAt=now,
                       lastError=RELEASE_REASON, releaseReason=RELEASE_REASON, releasedAt=now,
                       generation=binding_generation(binding) + 1)
        if args.note:
            binding["releaseNote"] = bounded(args.note, 500)
        atomic(path, binding)
        return {"ok": True, "alreadyReleased": False, "eventKey": key,
                "binding": compact_binding(binding, root),
                "note": "released for bounded tool waiting; Pi keeps running, a 0.2 hook sees the "
                        "generation/state change and exits its wait, and explicit --resume is required "
                        "to re-arm"}
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# hook handling
# ---------------------------------------------------------------------------

def binding_needs_recovery(binding: dict) -> bool:
    state = binding.get("state")
    if state in ("delivered", "suspended", "stale", "expired", "needs_recovery"):
        return True
    if state == "armed" and (binding.get("lastWaitExpiredAt") or binding.get("lastError")):
        return True
    return False


def _queue_recovery_lines(session: str) -> list:
    """Bounded cli-queue/monitor recovery evidence for one routed thread."""
    try:
        from pi_board import route_summary
        summary = route_summary(session)
    except Exception:  # noqa: BLE001 - recovery must never crash the hook
        return []
    if not summary:
        return []
    lines = []
    if summary.get("paused"):
        lines.append("route pause is active after an interrupt; explicit resume is required "
                     "(nothing resumes automatically)")
    lines.extend(summary.get("lines") or [])
    return [bounded(line, 400) for line in lines[:10]]


def handle_recovery(event_name: str, session: str) -> dict:
    root = handoff_root()
    items = []
    invalid = []
    for key, raw in session_binding_records(root, session):
        if raw is None:
            invalid.append((key, "binding file is unreadable JSON"))
            continue
        binding, reason = validate_binding(raw, key)
        if binding is None:
            invalid.append((key, reason))
            continue
        if binding.get("sessionId") == session and binding_needs_recovery(binding):
            items.append(binding)
    queue_lines = _queue_recovery_lines(session)
    if not items and not invalid and not queue_lines:
        return {}
    items.sort(key=lambda item: (item.get("armedAt") or 0, str(item.get("eventKey"))))
    lines = ["Codex-Pi handoff recovery (no automatic continuation was generated):"]
    for item in items[:8]:
        detail = compact_binding(item, root)
        commands = detail.get("commands") or {}
        lines.append(f"- event {detail.get('eventKey')} task={detail.get('task')} "
                     f"round={detail.get('round')} state={detail.get('state')} "
                     f"live={((detail.get('liveRoundState') or {}).get('roundState'))} "
                     f"ack={commands.get('ack', 'n/a')}")
        if item.get("lastError"):
            lines.append(f"  last_error={bounded(item.get('lastError'), 200)}")
        if item.get("state") in ("suspended", "expired", "needs_recovery"):
            lines.append("  explicit re-arm with --resume is required; nothing resumes automatically")
    for key, reason in invalid[:4]:
        lines.append(f"- event {key} state=invalid reason={bounded(reason, 200)}")
    if queue_lines:
        lines.append("Codex-Pi cli-queue transport (read-only recovery evidence):")
        lines.extend(queue_lines)
    return {"hookSpecificOutput": {"hookEventName": event_name,
                                   "additionalContext": bounded("\n".join(lines), MAX_MESSAGE_BYTES)}}


def handle_interrupt(session: str, event: dict) -> dict:
    root = handoff_root()
    # The marker is written first and is authoritative: a waiting Stop notices it
    # even when a per-binding lock is contended. The generation counter lets an
    # explicit resume that starts after this interrupt proceed.
    previous = session_interrupt_generation(root, session)
    generation = previous + 1
    atomic(session_marker_path(root, session),
           {"schemaVersion": SCHEMA_VERSION, "sessionId": session, "suspendedAt": time.time(),
            "generation": generation, "turnId": bounded(event.get("turn_id") or "", 200)})
    # Persist the same interruption for the opt-in cli-queue route: only an
    # explicit resume may clear it, and normal prompts/SessionStart never do.
    try:
        from pi_board import pause_route
        pause_route(session, "user interrupted this Codex session")
    except Exception:  # noqa: BLE001 - the handoff path must never crash on this
        pass
    deadline = time.monotonic() + INTERRUPT_BUDGET_SECONDS
    for key, raw in session_binding_records(root, session):
        if time.monotonic() >= deadline:
            break
        if not EVENT_KEY_RE.fullmatch(key):
            continue
        binding, _reason = validate_binding(raw, key)
        if binding is None or binding.get("sessionId") != session or binding.get("state") != "armed":
            continue
        if observed_interrupt_generation(binding) >= generation:
            # The binding was armed/resumed after this interrupt was observed.
            continue
        try:
            fd = lock_fd(binding_lock_path(root, key), blocking=False)
        except LockHeld:
            continue
        try:
            current = read_json(binding_path(root, key), None)
            record, _ = validate_binding(current, key)
            if record is not None and record.get("sessionId") == session and record.get("state") == "armed" \
                    and observed_interrupt_generation(record) < generation:
                record["state"] = "suspended"
                record["suspendedAt"] = time.time()
                record["lastCheckedAt"] = time.time()
                record["lastError"] = "user interrupted this Codex session"
                atomic(binding_path(root, key), record)
        finally:
            os.close(fd)
    return {}


def resolve_candidate(root: Path, binding: dict):
    """Validate frozen task/round identity for one armed binding (no writes).

    Returns (repo, task, task_dir, round_dir, problem_state, problem_message).
    """
    key = binding["eventKey"]
    repo = binding_repo(binding)
    task = binding.get("task")
    round_number = binding.get("round")

    def fail(state, message):
        return None, None, None, None, state, f"codex-pi handoff {key}: {message}; no continuation generated"

    if repo is None or not repo.is_dir():
        return fail("needs_recovery", "repository path is unavailable")
    if not isinstance(task, str) or not TASK_RE.fullmatch(task):
        return fail("needs_recovery", "task identity is invalid")
    try:
        common = git_common_dir(repo)
        task_dir = task_dir_for(common, task)
    except (ValueError, OSError) as exc:
        return fail("needs_recovery", f"task identity is invalid ({exc})")
    recorded_common = binding.get("commonDir")
    if isinstance(recorded_common, str) and recorded_common.strip() \
            and Path(recorded_common).expanduser().resolve() != common:
        return fail("needs_recovery", "git common dir no longer matches the binding")
    frozen = read_json(task_dir / "task.json", None)
    if not isinstance(frozen, dict):
        return fail("needs_recovery", "frozen task evidence is unreadable")
    if frozen.get("task") != task:
        return fail("needs_recovery", "frozen task identity mismatch")
    frozen_repo = frozen.get("repo")
    if not isinstance(frozen_repo, str) or Path(frozen_repo).expanduser().resolve() != repo:
        return fail("needs_recovery", "frozen repository identity mismatch")
    frozen_common = frozen.get("commonDir")
    if isinstance(frozen_common, str) and frozen_common.strip() \
            and Path(frozen_common).expanduser().resolve() != common:
        return fail("needs_recovery", "frozen git common dir mismatch")
    try:
        require_allowed_model(frozen.get("model"), "handoff frozen task")
    except ValueError as exc:
        return fail("needs_recovery", f"frozen model is not permitted ({exc})")
    numbers = [number for number, _ in list_rounds(task_dir)]
    if round_number not in numbers:
        return fail("stale", f"round {round_number} evidence is missing")
    if round_number != numbers[-1]:
        return fail("stale", f"round {round_number} was superseded by round {numbers[-1]}")
    round_dir = task_dir / "rounds" / str(round_number)
    state = read_json(round_dir / "round.state.json", None)
    if not isinstance(state, dict):
        return fail("needs_recovery", "round state evidence is unreadable")
    return repo, task, task_dir, round_dir, None, None


def deliver_terminal(root: Path, binding: dict, repo: Path, task: str, task_dir: Path,
                     round_number: int, raw_state: str, expected_generation: int):
    key = binding["eventKey"]
    # Explicit ownership rule: a cli-queue board task is notified by the board
    # queue, never by the legacy Stop handoff. Unrelated legacy tasks are
    # untouched and keep their original behavior.
    try:
        from pi_board import board_file_for_common, read_board
        board_file = board_file_for_common(git_common_dir(repo))
        if board_file.is_file():
            board, _problem = read_board(board_file)
            card = (board or {}).get("cards", {}).get(task)
            if isinstance(card, dict) and card.get("transport") == "cli-queue":
                mark_binding_state(root, key, "suspended",
                                   error="board cli-queue owns notifications for this task",
                                   expect_generation=expected_generation, expect_armed=True)
                return {"systemMessage": bounded(
                    f"codex-pi handoff {key}: task {task} is registered with the cli-queue "
                    "transport; the board queue owns notification and no legacy continuation "
                    "was generated")}
    except (ValueError, OSError, LockHeld):
        pass
    try:
        result = build_result(str(repo), task, round_number)
        require_allowed_model(result.get("model"), "handoff frozen task")
    except (ValueError, OSError, LockHeld) as exc:
        mark_binding_state(root, key, "needs_recovery", error=f"compact result validation failed: {exc}",
                           expect_generation=expected_generation, expect_armed=True)
        return {"systemMessage": bounded(f"codex-pi handoff {key}: compact result validation failed "
                                         f"({exc}); no continuation generated. Inspect with "
                                         + status_command(key))}
    if result.get("activeWorker") or result.get("supervisorAlive"):
        mark_binding_state(root, key, "needs_recovery",
                           error="terminal record but worker/supervisor ownership is still alive",
                           expect_generation=expected_generation, expect_armed=True)
        return {"systemMessage": bounded(f"codex-pi handoff {key}: terminal record but worker ownership "
                                         "is still held; outcome is not released and no continuation was "
                                         "generated. Inspect with " + status_command(key))}
    if lock_is_held(task_dir / ".task.lock") or lock_is_held(task_dir / ".supervisor.lock"):
        mark_binding_state(root, key, "needs_recovery",
                           error="terminal record but task/supervisor lock is still held",
                           expect_generation=expected_generation, expect_armed=True)
        return {"systemMessage": bounded(f"codex-pi handoff {key}: terminal record but ownership is still "
                                         "held; no continuation was generated. Inspect with "
                                         + status_command(key))}
    if result.get("task") != task or result.get("round") != round_number \
            or Path(str(result.get("repo") or ".")).expanduser().resolve() != repo \
            or result.get("state") != raw_state:
        mark_binding_state(root, key, "needs_recovery", error="compact result identity/state mismatch",
                           expect_generation=expected_generation, expect_armed=True)
        return {"systemMessage": bounded(f"codex-pi handoff {key}: compact result identity/state mismatch; "
                                         "no continuation generated. Inspect with " + status_command(key))}
    terminal = {"state": result.get("state"), "exitCode": result.get("exitCode"),
                "timedOut": bool(result.get("timedOut")), "cancelled": bool(result.get("cancelled")),
                "endedAt": time.time()}
    now = time.time()
    try:
        fd = lock_fd(binding_lock_path(root, key), blocking=True, timeout=5)
    except LockHeld:
        return None
    try:
        current = read_json(binding_path(root, key), None)
        record, _reason = validate_binding(current, key)
        if record is None or record.get("state") != "armed" \
                or record.get("sessionId") != binding.get("sessionId") \
                or binding_generation(record) != expected_generation:
            return None
        if interrupted_since(root, record.get("sessionId"), record):
            return None
        record.update(state="delivered", deliveredAt=now, delivery=terminal,
                      attempts=int(record.get("attempts") or 0) + 1, lastCheckedAt=now, lastError=None)
        atomic(binding_path(root, key), record)
        delivered = record
    finally:
        os.close(fd)
    return {"decision": "block", "reason": build_reason(delivered, repo, task, round_number)}


def build_reason(binding: dict, repo: Path, task: str, round_number: int) -> str:
    delivery = binding.get("delivery") or {}
    lines = [
        "Codex-Pi handoff is ready for main-session review (execution evidence, not acceptance).",
        f"event={binding.get('eventKey')}",
        f"repo={repo}",
        f"task={task}",
        f"round={round_number}",
        f"terminal_state={delivery.get('state')}",
        f"exit_code={delivery.get('exitCode')}",
        f"timed_out={bool(delivery.get('timedOut'))}",
        f"cancelled={bool(delivery.get('cancelled'))}",
        "Pi exit 0 means the process completed execution only; this hook never proves acceptance.",
        "Collect bounded evidence: " + collect_command(repo, task, round_number),
        "Acknowledge this exact event after collecting: " + ack_command(binding.get("eventKey")),
        "This event was delivered once; do not re-notify and do not change the pinned model.",
    ]
    return bounded("\n".join(lines), MAX_REASON_BYTES)


def _report(key: str, message: str) -> dict:
    return {"systemMessage": bounded(message + " Inspect with " + status_command(key))}


def handle_stop(session: str) -> dict:
    """One quick pass: deliver an already-terminal exact round or leave pending armed.

    This never sleeps, polls or waits for an active Pi task regardless of a
    legacy ``waitSeconds=14400`` on the record. Pending events stay armed for a
    later Stop; genuinely unknown ownership becomes ``needs_recovery`` exactly
    as before, and delivered/acked records are never demoted.
    """
    root = handoff_root()
    reports = []
    deliverables = []
    for key, raw in session_binding_records(root, session):
        if raw is None:
            reports.append({"systemMessage": bounded(
                f"codex-pi handoff {key}: binding evidence is corrupt or missing; no continuation generated")})
            continue
        binding, reason = validate_binding(raw, key)
        if binding is None:
            mark_binding_state(root, key, "needs_recovery", error=f"invalid binding: {reason}",
                               expect_armed=True)
            reports.append({"systemMessage": bounded(
                f"codex-pi handoff {key}: invalid binding ({reason}); no continuation generated")})
            continue
        if binding.get("sessionId") != session or binding.get("state") != "armed":
            continue
        if interrupted_since(root, session, binding):
            mark_binding_state(root, key, "suspended", error="user interrupted this Codex session",
                               expect_generation=binding_generation(binding), expect_armed=True)
            continue
        generation = binding_generation(binding)
        repo, task, task_dir, round_dir, problem_state, problem = resolve_candidate(root, binding)
        if problem:
            mark_binding_state(root, key, problem_state, error=problem,
                               expect_generation=generation, expect_armed=True)
            reports.append(_report(key, problem))
            continue
        numbers = [number for number, _ in list_rounds(task_dir)]
        if not numbers or numbers[-1] != binding.get("round"):
            latest = numbers[-1] if numbers else None
            mark_binding_state(root, key, "stale", error=f"round superseded by {latest}",
                               expect_generation=generation, expect_armed=True)
            reports.append(_report(key, f"codex-pi handoff {key}: round was superseded by {latest}; "
                                        "no continuation generated"))
            continue
        state = read_json(round_dir / "round.state.json", None)
        raw_state = state.get("state") if isinstance(state, dict) else None
        if raw_state in TERMINAL_STATES:
            held = lock_is_held(task_dir / ".task.lock") or lock_is_held(task_dir / ".supervisor.lock")
            if held:
                reports.append({"systemMessage": bounded(
                    f"codex-pi handoff {key}: round {binding.get('round')} is terminal but worker "
                    "ownership is still held; no continuation was generated and the event stays "
                    "armed for the next Stop. Inspect with " + status_command(key))})
                continue
            deliverables.append((binding, repo, task, task_dir, raw_state, generation))
            continue
        if raw_state == "unknown":
            mark_binding_state(root, key, "needs_recovery", error="round ownership state is unknown",
                               expect_generation=generation, expect_armed=True)
            reports.append(_report(key, f"codex-pi handoff {key}: round ownership is unknown/orphaned "
                                        "(state unknown); no continuation generated"))
            continue
        supervised = lock_is_held(task_dir / ".supervisor.lock")
        if raw_state in ACTIVE_STATES and not supervised:
            mark_binding_state(root, key, "needs_recovery",
                               error=f"supervisor lease missing while round state was {raw_state!r}",
                               expect_generation=generation, expect_armed=True)
            reports.append(_report(key, f"codex-pi handoff {key}: supervisor ownership is unknown "
                                        f"(state={raw_state!r}); outcome is not completed and no "
                                        "continuation was generated"))
            continue
        # Active, starting or otherwise non-terminal: keep the event armed and
        # return immediately. Bounded pi_task.py wait/status calls are the
        # supported way to observe progress; this hook never waits for Pi.
    for binding, repo, task, task_dir, raw_state, generation in deliverables:
        outcome = deliver_terminal(root, binding, repo, task, task_dir, binding["round"],
                                   raw_state, generation)
        if outcome is not None and outcome.get("decision") == "block":
            return outcome
        if outcome is not None:
            reports.append(outcome)
    return reports[0] if reports else {}


def dispatch_hook(event: dict) -> dict:
    name = event.get("hook_event_name")
    session = validate_session_id(event.get("session_id"))
    if name == "Stop":
        return handle_stop(session)
    if name == "Interrupt":
        return handle_interrupt(session, event)
    if name in ("SessionStart", "UserPromptSubmit"):
        return handle_recovery(name, session)
    return {}


def cmd_hook(_args) -> int:
    raw = sys.stdin.read(MAX_EVENT_BYTES)
    try:
        event = json.loads(raw)
    except ValueError:
        print(json.dumps({"systemMessage": "codex-pi handoff: hook input was not valid JSON; "
                                           "no continuation generated"}))
        return 0
    if not isinstance(event, dict):
        print(json.dumps({"systemMessage": "codex-pi handoff: hook input was not a JSON object; "
                                           "no continuation generated"}))
        return 0
    try:
        output = dispatch_hook(event)
    except Exception as exc:  # never crash the host hook; fail safe and bounded
        output = {"systemMessage": bounded(f"codex-pi handoff failed safely: {exc}; "
                                           "no continuation generated")}
    print(json.dumps(output, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi_handoff.py",
        description="Synchronous opt-in Stop handoff between an existing Codex session and an "
                    "existing Pi task round. Never invokes the Codex CLI or a model.")
    sub = parser.add_subparsers(dest="command", required=True)

    arm = sub.add_parser("arm", help="link the latest round of a repo/task to a Codex session")
    arm.add_argument("--repo", required=True)
    arm.add_argument("--task", required=True)
    arm.add_argument("--round", type=int, required=True)
    arm.add_argument("--session-id", help="Codex session id (default: CODEX_THREAD_ID)")
    arm.add_argument("--wait-seconds", type=float, default=None,
                     help="legacy compatibility only: any positive value is rejected because the Stop "
                          "hook performs one quick check and never waits for an active Pi task")
    arm.add_argument("--resume", action="store_true",
                     help="explicitly re-arm a suspended, expired or needs_recovery event")
    arm.set_defaults(func=cmd_arm)

    status = sub.add_parser("status", help="bounded binding/round recovery evidence")
    status.add_argument("--event-key", help="exact 64-hex event key")
    status.add_argument("--session-id", help="Codex session id (default: CODEX_THREAD_ID)")
    status.add_argument("--repo")
    status.add_argument("--task")
    status.add_argument("--round", type=int)
    status.add_argument("--all", action="store_true", help="list every binding, including invalid records")
    status.set_defaults(func=cmd_status)

    ack = sub.add_parser("ack", help="acknowledge result collection for one exact delivered event")
    ack.add_argument("--event-key", help="exact 64-hex event key")
    ack.add_argument("--session-id", help="Codex session id (default: CODEX_THREAD_ID)")
    ack.add_argument("--repo")
    ack.add_argument("--task")
    ack.add_argument("--round", type=int)
    ack.add_argument("--note")
    ack.set_defaults(func=cmd_ack)

    release = sub.add_parser("release", help="migrate one armed waiting hook without cancelling Pi")
    release.add_argument("--event-key", required=True, help="exact 64-hex event key")
    release.add_argument("--session-id", help="event owner session id (default: CODEX_THREAD_ID)")
    release.add_argument("--note", help="optional bounded release note")
    release.set_defaults(func=cmd_release)

    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook.set_defaults(func=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "hook":
        return cmd_hook(args)
    try:
        result = args.func(args)
    except (ValueError, LockHeld, OSError) as exc:
        print(f"pi_handoff: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
