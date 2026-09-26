#!/usr/bin/env python3
"""Opt-in structured board + CLI-queue transport for Codex-Pi coordination.

A repository stores one bounded snapshot at ``<git-common-dir>/codex-pi/board.json``.
The board is a projection over existing immutable task evidence (task.json,
round.state.json, round.checks receipts); it never replaces PLAN/briefs/receipts
and it never proves acceptance.

Roles are explicit commands, not a permission framework:
  * ``register``  -- main: bind an existing task to an owning desktop thread
    UUID, title/goal/brief refs and (explicit opt-in) the ``cli-queue``
    transport; offline boards remain fully usable without CLI use;
  * ``refresh``   -- Pi/runner: project real bounded status into the card and
    publish deduplicated attention events;
  * ``dispatch``  -- live supervisor: send one bounded packet of new actionable
    events through the verified CLI queue command;
  * ``decide``    -- main only: handle one exact event with an explicit
    accepted/rejected/changes_requested/resolved decision bound to an exact
    commit resolved in the registered repository;
  * ``pause``/``resume`` -- explicit persisted control state;
  * ``rearm``     -- explicit requeue after a lost/interrupted owner turn;
  * ``show``/``packet`` -- bounded compact reads for the selected thread/task.

Delivery state lives in ``board.queue.json`` and is separate from immutable
events and main decisions. The CLI queue has no caller-provided idempotency
key: a timeout or crash after send is recorded as uncertain and requires an
explicit rearm, never a blind exactly-once claim, endless retry or second model.

This module never invokes a Codex model: ``queue`` only enqueues a message for
the exact existing thread. No exec/resume/fork/app-server, MCP, daemon or
private IPC/database integration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from pi_task import (ACTIVE_STATES, TERMINAL_STATES, TASK_RE, LockHeld, atomic,  # noqa: E402
                     build_status, canonical_root, git_common_dir, lock_fd, read_json,
                     require_allowed_model, require_task_arg, task_dir_for, terminate)

SCHEMA_VERSION = 1
BOARD_DIR = "codex-pi"
BOARD_FILE = "board.json"
BOARD_LOCK = "board.lock"
MONITOR_LOG = "board-monitor.log"
MONITOR_FILE = "board.monitor.json"
MONITOR_LOCK = "board.monitor.lock"
QUEUE_FILE = "board.queue.json"
QUEUE_LOCK = "board.queue.lock"
ROUTE_DIR = "routes"
ROUTE_SCHEMA_VERSION = 1
MAX_BOARD_BYTES = 262_144
MAX_MONITOR_LOG_BYTES = 65_536
MAX_MONITOR_BYTES = 65_536
MAX_QUEUE_BYTES = 131_072
MAX_HANDLED_EVENTS = 20
MAX_HANDLED_IDS = 1000
MAX_PENDING_DISPLAY = 50
MAX_SUMMARY = 300
MAX_NOTE = 300
MAX_MONITORS = 200
MAX_QUEUE_TASKS = 200
MAX_QUEUE_FAILURES = 10
REFRESH_INTERVAL_SECONDS = 15.0
MONITOR_LEASE_SECONDS = 90.0
GIT_TIMEOUT_SECONDS = 10.0
DISPATCH_TIMEOUT_SECONDS = 20.0
MAX_TRANSPORT_RETRIES = 2
MAX_CLI_OUTPUT_BYTES = 4096
QUEUE_STALE_INFLIGHT_SECONDS = 120.0
MAX_PACKET_CHARS = 3500
MAX_PACKET_EVENTS = 3
DEFAULT_CODEX_BIN = "codex"
TRANSPORT_OFFLINE = "offline"
TRANSPORT_CLI_QUEUE = "cli-queue"
TRANSPORTS = (TRANSPORT_OFFLINE, TRANSPORT_CLI_QUEUE)
EVENT_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
FULL_OID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
THREAD_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                       r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
DECISIONS = {"accept": "accepted", "reject": "rejected",
             "changes_requested": "changes_requested", "resolve": "resolved"}
REVIEW_KINDS = ("review_required",)
QUEUE_LIMITATION = ("the CLI queue has no caller-provided idempotency key; a timeout or crash "
                    "after send is recorded as uncertain and requires explicit rearm")


class BoardOverflow(ValueError):
    """The bounded board snapshot cannot hold the pending work."""


class QueueOverflow(ValueError):
    """The bounded queue snapshot cannot hold the delivery state."""


# ---------------------------------------------------------------------------
# small bounded helpers
# ---------------------------------------------------------------------------

def _text(value, limit: int) -> str:
    text = str(value).replace("\x00", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _read_bounded_json(path, limit: int):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if len(raw) > limit:
        return None, "oversized"
    try:
        return json.loads(raw.decode("utf-8")), None
    except ValueError:
        return None, "invalid"


def _trim_lines(path: Path, keep_bytes: int) -> None:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > keep_bytes * 2:
                stream.seek(size - keep_bytes * 2)
            raw = stream.read()
    except OSError:
        return
    tail = raw[-keep_bytes:]
    cut = tail.find(b"\n")
    if cut >= 0:
        tail = tail[cut + 1:]
    try:
        path.write_bytes(tail)
    except OSError:
        pass


def handoff_root() -> Path:
    override = os.environ.get("CODEX_PI_HANDOFF_ROOT")
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root
    home = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
    return home / "codex-pi" / "handoffs"


def thread_key(thread: str) -> str:
    return hashlib.sha256(str(thread).encode("utf-8")).hexdigest()


def _validate_thread(value, required: bool = True):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError("an exact owner desktop thread UUID is required for cli-queue")
        return None
    value = str(value).strip()
    if not THREAD_RE.fullmatch(value):
        raise ValueError("owner thread must be an exact UUID (8-4-4-4-12 hex); "
                         "fuzzy or mismatched owners are never routed")
    return value.lower()


def _validate_transport(value) -> str:
    if value not in TRANSPORTS:
        raise ValueError(f"transport must be one of {', '.join(TRANSPORTS)}")
    return value


def board_file_for_common(common) -> Path:
    return Path(common) / BOARD_DIR / BOARD_FILE


def board_file_for_repo(repo):
    root = canonical_root(Path(repo))
    common = git_common_dir(root)
    return root, common, board_file_for_common(common)


def monitor_lease_seconds() -> float:
    raw = os.environ.get("CODEX_PI_MONITOR_LEASE_SECONDS")
    try:
        value = float(raw) if raw else MONITOR_LEASE_SECONDS
    except (TypeError, ValueError):
        value = MONITOR_LEASE_SECONDS
    return value if 1.0 <= value <= 86400 else MONITOR_LEASE_SECONDS


def validate_board(data) -> str | None:
    if not isinstance(data, dict):
        return "board is not a JSON object"
    if data.get("schemaVersion") != SCHEMA_VERSION:
        return f"board schemaVersion must be {SCHEMA_VERSION}"
    revision = data.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return "board revision is invalid"
    cards = data.get("cards")
    if not isinstance(cards, dict):
        return "board cards is not an object"
    for task_id, card in cards.items():
        if not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id):
            return "board contains an unsafe task id"
        if not isinstance(card, dict):
            return f"card {task_id} is not an object"
        if card.get("taskId") != task_id:
            return f"card {task_id} identity mismatch"
        if not isinstance(card.get("events", []), list):
            return f"card {task_id} events are not a list"
    return None


def read_board(path):
    data, problem = _read_bounded_json(path, MAX_BOARD_BYTES)
    if problem is not None:
        return None, problem
    problem = validate_board(data)
    if problem:
        return None, problem
    return data, None


def _serialized(board) -> bytes:
    return (json.dumps(board, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _shrink_handled(board) -> None:
    """Aggressive handled-history pruning only; never touches unhandled work."""
    for card in (board.get("cards") or {}).values():
        if not isinstance(card, dict):
            continue
        card["events"] = [event for event in card.get("events", [])
                          if isinstance(event, dict) and not event.get("handled")]
        handled = card.get("handled")
        if isinstance(handled, dict) and len(handled) > MAX_HANDLED_IDS:
            ordered = sorted(handled.items(), key=lambda item: (item[1] or {}).get("at") or 0)
            card["handled"] = dict(ordered[-MAX_HANDLED_IDS:])


def _write_board(board_file, board) -> None:
    """Refuse a write that would exceed the bounded snapshot; never drop pending."""
    if len(_serialized(board)) > MAX_BOARD_BYTES:
        _shrink_handled(board)
        if len(_serialized(board)) > MAX_BOARD_BYTES:
            raise BoardOverflow(
                f"board snapshot would exceed MAX_BOARD_BYTES={MAX_BOARD_BYTES}; refusing the "
                f"write instead of silently dropping unhandled events or decisions: {board_file}")
    atomic(board_file, board)


# ---------------------------------------------------------------------------
# monitor lease/freshness (separate from the semantic board revision)
# ---------------------------------------------------------------------------

def monitor_paths(board_file):
    directory = Path(board_file).parent
    return directory / MONITOR_FILE, directory / MONITOR_LOCK


def read_monitors(board_file):
    path, _lock = monitor_paths(board_file)
    if not path.exists():
        return None, "missing"
    data, problem = _read_bounded_json(path, MAX_MONITOR_BYTES)
    if problem is not None:
        return None, problem
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 \
            or not isinstance(data.get("tasks"), dict):
        return None, "invalid"
    return data, None


def monitor_for(monitors, task_id):
    if not isinstance(monitors, dict):
        return None
    record = (monitors.get("tasks") or {}).get(task_id)
    return record if isinstance(record, dict) else None


def write_monitor_record(board_file, task_id: str, record: dict, now: float) -> bool:
    """Persist one bounded monitor lease entry without touching board revision."""
    path, lock = monitor_paths(board_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = lock_fd(lock, blocking=False)
    except (LockHeld, OSError):
        return False
    try:
        data, problem = _read_bounded_json(path, MAX_MONITOR_BYTES)
        if problem is not None or not isinstance(data, dict):
            data = {"schemaVersion": 1, "tasks": {}}
        tasks = data.setdefault("tasks", {})
        entry = dict(record)
        entry["task"] = task_id
        entry["refreshedAt"] = now
        entry["updatedAt"] = now
        tasks[task_id] = entry
        if len(tasks) > MAX_MONITORS:
            ordered = sorted(tasks.items(), key=lambda item: (item[1] or {}).get("updatedAt") or 0)
            data["tasks"] = dict(ordered[-MAX_MONITORS:])
        data["schemaVersion"] = 1
        if len(_serialized(data)) > MAX_MONITOR_BYTES:
            return False
        atomic(path, data)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def record_monitor_error(task: dict, message: str) -> None:
    """Bounded monitor-error evidence: log plus a visible unhealthy lease record."""
    common_raw = task.get("commonDir") if isinstance(task, dict) else None
    if not isinstance(common_raw, str) or not common_raw.strip():
        return
    directory = Path(common_raw) / BOARD_DIR
    path = directory / MONITOR_LOG
    task_id = task.get("task") if isinstance(task, dict) else None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"schemaVersion": 1, "at": time.time(), "task": task_id,
                           "error": _text(message, 300)}, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
        if path.stat().st_size > MAX_MONITOR_LOG_BYTES:
            _trim_lines(path, MAX_MONITOR_LOG_BYTES // 2)
    except OSError:
        pass
    try:
        board_file = board_file_for_common(Path(common_raw))
        monitors, _problem = read_monitors(board_file)
        previous = monitor_for(monitors, task_id)
        record = dict(previous) if isinstance(previous, dict) else {}
        record.update({"task": task_id, "healthy": False, "error": _text(message, 300),
                       "source": "monitor", "pid": os.getpid()})
        write_monitor_record(board_file, task_id, record, time.time())
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# queue delivery state (separate from events and decisions)
# ---------------------------------------------------------------------------

def queue_paths(board_file):
    directory = Path(board_file).parent
    return directory / QUEUE_FILE, directory / QUEUE_LOCK


def read_queue(board_file):
    path, _lock = queue_paths(board_file)
    if not path.exists():
        return {"schemaVersion": 1, "tasks": {}}, None
    data, problem = _read_bounded_json(path, MAX_QUEUE_BYTES)
    if problem is not None:
        return None, problem
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 \
            or not isinstance(data.get("tasks"), dict):
        return None, "invalid"
    return data, None


def _queue_entry(queue: dict, task_id: str) -> dict:
    entry = queue.setdefault("tasks", {}).setdefault(
        task_id, {"claims": {}, "failures": [], "lastStatus": "idle"})
    if not isinstance(entry.get("claims"), dict):
        entry["claims"] = {}
    if not isinstance(entry.get("failures"), list):
        entry["failures"] = []
    return entry


def _write_queue(board_file, queue) -> None:
    tasks = queue.get("tasks") or {}
    if len(tasks) > MAX_QUEUE_TASKS:
        ordered = sorted(tasks.items(),
                         key=lambda item: (item[1] or {}).get("updatedAt") or 0)
        queue["tasks"] = dict(ordered[-MAX_QUEUE_TASKS:])
    if len(_serialized(queue)) > MAX_QUEUE_BYTES:
        raise QueueOverflow(
            f"queue snapshot would exceed MAX_QUEUE_BYTES={MAX_QUEUE_BYTES}; refusing the write "
            "instead of silently dropping delivery state")
    atomic(board_file.parent / QUEUE_FILE, queue)


def _normalize_claims(entry: dict, now: float) -> dict:
    claims = entry.setdefault("claims", {})
    for event_id, claim in list(claims.items()):
        if not isinstance(claim, dict):
            claims[event_id] = {"status": "uncertain", "at": now,
                                "lastError": "corrupt claim record"}
            continue
        if claim.get("status") == "inflight":
            at = claim.get("at")
            if isinstance(at, bool) or not isinstance(at, (int, float)) \
                    or now - at > QUEUE_STALE_INFLIGHT_SECONDS:
                claim["status"] = "uncertain"
                claim["at"] = now
                claim["lastError"] = ("inflight claim expired without a confirmed queue result; "
                                      "explicit rearm required")
    return claims


def _claim_queue(board_file, task_id: str, event_ids, now: float, packet_id: str):
    """Nonblocking claim of the exact events included in one packet.

    Returns the claimed ids, ``[]`` when nothing was claimable, or ``None`` when
    the short lock was held (the caller must retry on a later tick).
    """
    path, lock = queue_paths(board_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = lock_fd(lock, blocking=False)
    except (LockHeld, OSError):
        return None
    try:
        queue, problem = read_queue(board_file)
        if queue is None:
            return None
        entry = _queue_entry(queue, task_id)
        claims = _normalize_claims(entry, now)
        claimed = []
        for event_id in event_ids:
            claim = claims.get(event_id)
            previous_attempts = int(claim.get("attempts") or 0) if isinstance(claim, dict) else 0
            if isinstance(claim, dict):
                status = claim.get("status")
                if status in ("queued", "uncertain", "inflight"):
                    continue
                if status == "failed" and previous_attempts >= MAX_TRANSPORT_RETRIES:
                    continue
            claims[event_id] = {"status": "inflight", "at": now,
                                "attempts": previous_attempts + 1, "packetId": packet_id}
            claimed.append(event_id)
        entry["updatedAt"] = now
        _write_queue(board_file, queue)
        return claimed
    finally:
        os.close(fd)


def _finish_queue(board_file, task_id: str, event_ids, result: dict, now: float,
                  packet_id: str) -> None:
    try:
        fd = lock_fd(queue_paths(board_file)[1], blocking=True, timeout=5)
    except (LockHeld, OSError):
        return
    try:
        queue, problem = read_queue(board_file)
        if queue is None:
            return
        entry = _queue_entry(queue, task_id)
        claims = entry.setdefault("claims", {})
        status = result.get("status", "failed")
        for event_id in event_ids:
            claim = claims.get(event_id) if isinstance(claims.get(event_id), dict) else {}
            claim.update({"status": status, "at": now,
                          "attempts": int(claim.get("attempts") or 0),
                          "packetId": packet_id,
                          "lastError": _text(result.get("error"), 300) if result.get("error") else None,
                          "exitCode": result.get("exitCode")})
            claims[event_id] = claim
        entry["lastStatus"] = status
        entry["lastDispatch"] = {
            "at": now, "packetId": packet_id, "status": status,
            "eventIds": [event_id for event_id in event_ids if isinstance(event_id, str)],
            "exitCode": result.get("exitCode"), "timedOut": bool(result.get("timedOut")),
            "outputSha256": result.get("outputSha256"),
            "outputExcerpt": result.get("outputExcerpt"),
            "argv0": result.get("argv0"),
        }
        if status in ("failed", "uncertain"):
            entry.setdefault("failures", []).append({
                "at": now, "status": status, "packetId": packet_id,
                "exitCode": result.get("exitCode"),
                "error": _text(result.get("error") or status, 300),
                "eventIds": [event_id for event_id in event_ids if isinstance(event_id, str)][:10]})
            entry["failures"] = entry["failures"][-MAX_QUEUE_FAILURES:]
        entry["updatedAt"] = now
        _write_queue(board_file, queue)
    finally:
        os.close(fd)


def _clear_queue_claims(board_file, task_id: str, event_id=None) -> dict:
    try:
        fd = lock_fd(queue_paths(board_file)[1], blocking=True, timeout=10)
    except (LockHeld, OSError) as exc:
        raise ValueError(f"queue lock unavailable: {exc}") from None
    try:
        queue, problem = read_queue(board_file)
        if queue is None:
            raise ValueError(f"queue state is {problem}")
        entry = _queue_entry(queue, task_id)
        claims = entry.setdefault("claims", {})
        if event_id is not None:
            removed = 1 if claims.pop(event_id, None) is not None else 0
        else:
            removed = len(claims)
            claims.clear()
        entry["lastStatus"] = "rearmed"
        entry["updatedAt"] = time.time()
        _write_queue(board_file, queue)
        return {"ok": True, "taskId": task_id, "clearedClaims": removed,
                "eventId": event_id, "note": "explicit rearm; events remain unhandled and "
                                             "will dispatch once on the next tick"}
    finally:
        os.close(fd)


def queue_view(board_file, task_id: str) -> dict:
    queue, problem = read_queue(board_file)
    if queue is None:
        return {"status": f"unknown ({problem})", "limitations": [QUEUE_LIMITATION]}
    entry = (queue.get("tasks") or {}).get(task_id)
    if not isinstance(entry, dict):
        return {"status": "idle", "queued": 0, "inflight": 0, "uncertain": 0, "failed": 0,
                "lastDispatch": None, "failures": [], "limitations": [QUEUE_LIMITATION]}
    claims = entry.get("claims") if isinstance(entry.get("claims"), dict) else {}
    counts = {"queued": 0, "inflight": 0, "uncertain": 0, "failed": 0}
    now = time.time()
    for claim in claims.values():
        if not isinstance(claim, dict):
            continue
        status = claim.get("status")
        if status == "inflight":
            at = claim.get("at")
            if isinstance(at, bool) or not isinstance(at, (int, float)) \
                    or now - at > QUEUE_STALE_INFLIGHT_SECONDS:
                status = "uncertain"  # a crashed in-flight claim is never auto-resent
        if status in counts:
            counts[status] += 1
    return {
        "status": entry.get("lastStatus") or "idle",
        "queued": counts["queued"], "inflight": counts["inflight"],
        "uncertain": counts["uncertain"], "failed": counts["failed"],
        "totalClaims": len(claims),
        "lastDispatch": entry.get("lastDispatch"),
        "failures": (entry.get("failures") or [])[-3:],
        "limitations": [QUEUE_LIMITATION],
    }


# ---------------------------------------------------------------------------
# card / event model
# ---------------------------------------------------------------------------

def _default_codex() -> dict:
    return {"review": "pending", "reviewedHead": None, "decidedAt": None, "lastEventId": None,
            "lastDecision": None, "lastDecisionAt": None, "history": []}


def _new_card(task_id, thread, title, goal, brief_ref, plan_ref, repo, common, worktree,
              transport, codex_bin, now) -> dict:
    return {
        "taskId": task_id, "ownerThread": thread, "codexTaskId": thread,
        "title": _text(title or task_id, 200), "goal": _text(goal or "", 600),
        "planRef": plan_ref, "briefRef": brief_ref,
        "repo": str(repo), "commonDir": str(common), "worktree": worktree,
        "transport": transport, "codexBin": codex_bin,
        "createdAt": now, "updatedAt": now,
        "paused": False, "pausedAt": None, "pauseNote": None,
        "pi": {"round": None, "state": None, "stage": None, "updatedAt": None},
        "evidence": {}, "check": None, "events": [], "handled": {}, "overflow": None,
        "nextSeq": 1, "attention": None, "codex": _default_codex(),
    }


def event_identity(task_id: str, round_number, kind: str, fingerprint: str) -> str:
    raw = "\x00".join((task_id, str(round_number), kind, str(fingerprint)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def find_event(card: dict, event_id: str):
    for event in card.get("events", []):
        if isinstance(event, dict) and event.get("id") == event_id:
            return event
    return None


def pending_events(card: dict) -> list:
    return [event for event in card.get("events", [])
            if isinstance(event, dict) and not event.get("handled")]


def _prune_events(card: dict) -> None:
    """Prune handled history only; unhandled events are never discarded."""
    events = [event for event in card.get("events", []) if isinstance(event, dict)]
    handled_map = card.setdefault("handled", {})
    handled = [event for event in events if event.get("handled")]
    for event in handled:
        handled_map.setdefault(event.get("id"), {
            "at": event.get("handledAt") or 0, "decision": event.get("decision"),
            "reviewedHead": event.get("reviewedHead"), "round": event.get("round")})
    unhandled = [event for event in events if not event.get("handled")]
    keep_handled = handled[-MAX_HANDLED_EVENTS:]
    card["events"] = sorted(unhandled + keep_handled, key=lambda event: event.get("seq") or 0)
    if len(handled_map) > MAX_HANDLED_IDS:
        ordered = sorted(handled_map.items(), key=lambda item: (item[1] or {}).get("at") or 0)
        card["handled"] = dict(ordered[-MAX_HANDLED_IDS:])


def _update_overflow(card: dict, now: float) -> None:
    count = len(pending_events(card))
    if count > MAX_PENDING_DISPLAY:
        card["overflow"] = {"active": True, "pendingCount": count, "updatedAt": now,
                            "note": "pending events exceed the display budget; the board retains "
                                    "all of them and requires explicit decisions"}
    elif isinstance(card.get("overflow"), dict) and card["overflow"].get("active"):
        card["overflow"] = {"active": False, "pendingCount": count, "resolvedAt": now}


def add_event(card: dict, kind: str, round_number, fingerprint: str, summary: str,
              candidate: dict, evidence: dict, question: str, now: float):
    """Idempotent publication: one immutable identity per task/round/kind/fingerprint."""
    identity = event_identity(card["taskId"], round_number, kind, fingerprint)
    if find_event(card, identity) is not None:
        return None
    if identity in (card.get("handled") or {}):
        return None
    seq = int(card.get("nextSeq") or 1)
    event = {
        "id": identity, "seq": seq, "round": round_number, "kind": kind,
        "fingerprint": _text(fingerprint, 200), "summary": _text(summary, MAX_SUMMARY),
        "question": _text(question, MAX_SUMMARY),
        "candidate": candidate, "evidence": evidence, "createdAt": now,
        "handled": False, "handledAt": None, "decision": None, "reviewedHead": None,
        "note": None,
    }
    card.setdefault("events", []).append(event)
    card["nextSeq"] = seq + 1
    card["attention"] = {"seq": seq, "kind": kind, "eventId": identity,
                         "summary": event["summary"], "updatedAt": now}
    codex = card.setdefault("codex", _default_codex())
    codex["review"] = "pending"
    _prune_events(card)
    _update_overflow(card, now)
    return event


def derive_stage(state, running) -> str:
    if state == "starting":
        return "starting"
    if state == "running":
        return "checking" if running is not None else "implementing"
    if state == "completed":
        return "review"
    if state == "failed":
        return "repair"
    if state == "timed_out":
        return "timed_out"
    if state == "cancelled":
        return "cancelled"
    return "unknown"


def _receipt_ref(checks: dict, latest):
    if not isinstance(latest, dict):
        return None
    checks_dir = checks.get("dir")
    name = latest.get("receipt")
    if isinstance(checks_dir, str) and isinstance(name, str):
        return str(Path(checks_dir) / name)
    return None


def project_status(card: dict, status: dict, now: float) -> bool:
    """Projection only (round/state/stage/evidence/check); never an event."""
    changed = False
    pi = card.setdefault("pi", {})
    new_pi = {
        "round": status.get("round"),
        "state": status.get("state"),
        "stage": derive_stage(status.get("state"), (status.get("checks") or {}).get("running")),
        "updatedAt": now,
    }
    if any(pi.get(key) != new_pi[key] for key in ("round", "state", "stage")):
        changed = True
    pi.update(new_pi)

    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    latest = receipts.get("latest")
    evidence_dir = status.get("evidence") or {}
    new_evidence = {
        "candidateHead": status.get("endHead") or status.get("startHead"),
        "worktree": status.get("worktree"),
        "taskDir": evidence_dir.get("taskDir"),
        "roundDir": evidence_dir.get("roundDir"),
        "briefRef": evidence_dir.get("brief"),
        "checksRef": checks.get("dir"),
        "stateRef": evidence_dir.get("state"),
        "receiptRef": _receipt_ref(checks, latest),
        "latestCheckId": latest.get("id") if isinstance(latest, dict) else None,
    }
    if card.get("evidence") != new_evidence:
        changed = True
    card["evidence"] = new_evidence

    running = checks.get("running")
    new_check = None if not isinstance(running, dict) else {
        "id": running.get("id"), "marker": running.get("marker"),
        "pidAlive": running.get("pidAlive"), "startedAt": running.get("startedAt"),
        "deadlineAt": running.get("deadlineAt"), "resourceLimit": running.get("resourceLimit"),
    }
    if card.get("check") != new_check:
        changed = True
    card["check"] = new_check
    card["updatedAt"] = now
    return changed


def project_events(card: dict, status: dict, now: float) -> list:
    """Actionable facts only: terminal, verified timeout/resource, unknown ownership."""
    added = []
    state = status.get("state")
    recorded = status.get("recordedState")
    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    latest = receipts.get("latest")
    guard = checks.get("resourceGuard") or {}
    evidence_dir = status.get("evidence") or {}
    head = status.get("endHead") or status.get("startHead")
    candidate = {"round": status.get("round"), "head": head, "state": state}
    base_evidence = {"briefRef": evidence_dir.get("brief"), "checksRef": checks.get("dir"),
                     "stateRef": evidence_dir.get("state")}

    def add(kind, fingerprint, summary, question, extra=None):
        evidence = dict(base_evidence)
        if extra:
            evidence.update(extra)
        event = add_event(card, kind, status.get("round"), fingerprint, summary,
                          candidate, evidence, question, now)
        if event is not None:
            added.append(event)

    if status.get("timedOut"):
        add("task_timeout", f"exit={status.get('exitCode')}",
            f"round {status.get('round')} wrapper timed out (exit {status.get('exitCode')})",
            "Decide whether to repair, cancel or extend the deadline; the round state is exact evidence.",
            {"receiptRef": _receipt_ref(checks, latest)})
    timeout_receipts = [item for item in (receipts.get("failedRecent") or [])
                        if isinstance(item, dict) and item.get("timedOut")]
    if not timeout_receipts and isinstance(latest, dict) and latest.get("timedOut"):
        timeout_receipts = [latest]
    for item in timeout_receipts:
        add("check_timeout", f"{item.get('receipt')}",
            f"check {item.get('id')!r} timed out",
            "Decide whether to repair the check or cancel; the receipt is the exact evidence.",
            {"receiptRef": _receipt_ref(checks, item)})
    for breach in guard.get("breaches") or []:
        if not isinstance(breach, dict):
            continue
        name = breach.get("name") or breach.get("source")
        add("resource_breach", f"{name}:{breach.get('path')}:{breach.get('maxBytes')}",
            f"resource budget breached under {breach.get('path')} "
            f"(at least {breach.get('observedBytes')} bytes > cap {breach.get('maxBytes')})",
            "Decide whether to stop, widen the declared budget or repair; guard evidence is exact.",
            {"guardPath": breach.get("path"), "observedBytes": breach.get("observedBytes"),
             "maxBytes": breach.get("maxBytes"), "source": breach.get("source"),
             "breachBasis": breach.get("breachBasis")})
    if recorded in TERMINAL_STATES and state != "unknown":
        add("review_required", f"{state}:{head}:{status.get('exitCode')}",
            f"round {status.get('round')} reached terminal state {state}",
            "Review the candidate and exact check receipts; accept or request changes. "
            "Pi exit 0 is execution evidence only.",
            {"candidateHead": head, "receiptRef": _receipt_ref(checks, latest)})
    if state == "unknown" and recorded in ACTIVE_STATES:
        add("ownership_unknown", f"{recorded}",
            f"recorded active state {recorded} without a live supervisor lease",
            "Inspect ownership and decide cleanup; do not assume progress.",
            {"recordedState": recorded})
    ownership = status.get("ownership") or {}
    if recorded in TERMINAL_STATES and ownership.get("activeWorker") \
            and not ownership.get("supervisorAlive"):
        add("ownership_lingering", f"{recorded}",
            "terminal record but a worker lock is still held",
            "Inspect the lingering owned worker before reuse.",
            {"recordedState": recorded})
    return added


def refresh_with_status(board_file, task_id: str, status: dict, now=None, block: bool = True,
                        source: str = "cli"):
    """Short-lock projection refresh; no board write when nothing meaningful changed."""
    now = time.time() if now is None else now
    board_file = Path(board_file)
    if not board_file.is_file():
        return {"ok": True, "refreshed": False, "reason": "no board"}
    try:
        fd = lock_fd(board_file.with_name(BOARD_LOCK), blocking=block,
                     timeout=5.0 if block else 0.0)
    except LockHeld:
        write_monitor_record(board_file, task_id, {
            "task": task_id, "healthy": False, "error": "board lock was held during refresh",
            "source": source, "state": status.get("state"), "round": status.get("round")}, now)
        return {"ok": True, "refreshed": False, "reason": "board lock held"}
    try:
        board, problem = read_board(board_file)
        if board is None:
            raise ValueError(f"board state is {problem}: {board_file}")
        card = board["cards"].get(task_id)
        if not isinstance(card, dict):
            return {"ok": True, "refreshed": False, "reason": "task is not registered"}
        changed = project_status(card, status, now)
        added = project_events(card, status, now)
        _update_overflow(card, now)
        changed = changed or bool(added)
        if changed:
            board["revision"] = int(board.get("revision") or 0) + 1
            board["updatedAt"] = now
            _write_board(board_file, board)
        write_monitor_record(board_file, task_id, {
            "task": task_id, "healthy": True, "error": None, "source": source,
            "state": status.get("state"), "round": status.get("round"),
            "boardRevision": board.get("revision"), "pid": os.getpid()}, now)
        return {"ok": True, "refreshed": bool(changed), "revision": board.get("revision"),
                "taskId": task_id, "pi": dict(card.get("pi") or {}),
                "newEvents": [event["id"] for event in added],
                "pendingEvents": len(pending_events(card)),
                "overflow": card.get("overflow"),
                "card": compact_card(card)}
    finally:
        os.close(fd)


def refresh_registered_task(task: dict, now=None, block: bool = True, source: str = "supervisor"):
    """Cheap registration pre-check before any status work."""
    common_raw = task.get("commonDir") if isinstance(task, dict) else None
    task_id = task.get("task") if isinstance(task, dict) else None
    if not isinstance(common_raw, str) or not common_raw.strip() or not isinstance(task_id, str):
        return {"ok": True, "refreshed": False, "reason": "task has no commonDir"}
    board_file = board_file_for_common(Path(common_raw))
    if not board_file.is_file():
        return {"ok": True, "refreshed": False, "reason": "no board"}
    board, problem = read_board(board_file)
    if board is None:
        raise ValueError(f"board state is {problem}: {board_file}")
    if not isinstance((board.get("cards") or {}).get(task_id), dict):
        return {"ok": True, "refreshed": False, "reason": "task is not registered"}
    status = build_status(task["repo"], task_id)
    return refresh_with_status(board_file, task_id, status, now, block, source=source)


# ---------------------------------------------------------------------------
# route registration / pause (short recovery evidence)
# ---------------------------------------------------------------------------

def route_paths(thread: str):
    directory = handoff_root() / ROUTE_DIR
    key = thread_key(thread)
    return directory / f"{key}.json", directory / f"{key}.paused.json"


def register_route(thread: str, board_file, task_ids, transport: str, now: float) -> dict:
    path, _pause = route_paths(thread)
    path.parent.mkdir(parents=True, exist_ok=True)
    route = {"schemaVersion": ROUTE_SCHEMA_VERSION, "thread": thread,
             "boardPath": str(board_file), "taskIds": list(task_ids),
             "transport": transport, "createdAt": now, "updatedAt": now}
    atomic(path, route)
    return route


def read_route(thread: str):
    path, _pause = route_paths(thread)
    if not path.exists():
        return None, "missing"
    data, problem = _read_bounded_json(path, 16_384)
    if problem is not None:
        return None, problem
    if not isinstance(data, dict) or data.get("schemaVersion") != ROUTE_SCHEMA_VERSION \
            or data.get("thread") != thread or not isinstance(data.get("taskIds"), list):
        return None, "invalid"
    return data, None


def pause_route(thread: str, reason: str = "user interrupted this Codex session", now=None) -> dict:
    now = time.time() if now is None else now
    thread = _validate_thread(thread)
    path, pause = route_paths(thread)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic(pause, {"schemaVersion": 1, "thread": thread, "pausedAt": now,
                   "reason": _text(reason, 200), "source": "interrupt"})
    return {"ok": True, "thread": thread, "pausedAt": now}


def route_paused(thread: str):
    _path, pause = route_paths(thread)
    if not pause.exists():
        return False, None
    data, problem = _read_bounded_json(pause, 4096)
    if problem is not None:
        return False, problem
    if not isinstance(data, dict) or data.get("thread") != thread:
        return False, "invalid"
    return True, None


def resume_route(thread: str) -> dict:
    thread = _validate_thread(thread)
    _path, pause = route_paths(thread)
    try:
        pause.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ValueError(f"could not clear route pause {pause}: {exc}") from None
    return {"ok": True, "thread": thread, "routePauseCleared": True}


# ---------------------------------------------------------------------------
# dispatch (CLI queue transport)
# ---------------------------------------------------------------------------

def _run_queue_cli(argv, timeout: float) -> dict:
    """Run the exact queue argv with shell=False, bounded output and owned cleanup."""
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as exc:
        return {"status": "failed", "exitCode": None, "timedOut": False,
                "error": f"queue command could not start: {exc}"}
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate(child)
        return {"status": "uncertain", "exitCode": None, "timedOut": True,
                "error": "queue command timed out after send; delivery is uncertain "
                         "and requires explicit rearm"}
    except KeyboardInterrupt:
        terminate(child)
        return {"status": "uncertain", "exitCode": None, "timedOut": False,
                "error": "interrupted during send; delivery is uncertain and requires explicit rearm"}
    output = ((out or "") + (err or ""))[:MAX_CLI_OUTPUT_BYTES]
    result = {"exitCode": child.returncode, "timedOut": False,
              "outputSha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
              "outputExcerpt": output[:1000], "argv0": argv[0]}
    if child.returncode == 0:
        result["status"] = "queued"
    else:
        result["status"] = "failed"
        result["error"] = f"queue command exited {child.returncode}"
    return result


def build_packet(card: dict, events, limit: int = MAX_PACKET_EVENTS):
    """Build a bounded runtime-handoff packet; return ``(text, included_events)``."""
    header = [
        "Codex-Pi runtime handoff (transport=cli-queue; not a new user goal or instruction override).",
        "Preserve the latest user pause/instructions. Exit 0 is queue delivery only, never acceptance.",
    ]
    title = _text(card.get("title") or card.get("taskId") or "", 120)
    goal = _text(card.get("goal") or "", 160)
    lines = [f"task={card.get('taskId')} title={title}"]
    if goal:
        lines.append(f"goal={goal}")
    included = []
    for event in events:
        if len(included) >= limit:
            break
        block = [
            f"round={event.get('round')} event={event.get('id')} kind={event.get('kind')}",
            f"summary={event.get('summary')}",
            f"question={event.get('question')}",
            f"candidate_head={(event.get('candidate') or {}).get('head') or 'unknown'}",
        ]
        evidence = event.get("evidence") or {}
        for label, key in (("brief", "briefRef"), ("checks", "checksRef"),
                           ("receipt", "receiptRef"), ("state", "stateRef")):
            if evidence.get(key):
                block.append(f"{label}={evidence.get(key)}")
        block.append("decide=" + _decide_hint(card.get("repo"), card.get("taskId"), event))
        candidate = "\n".join(header + lines + block)
        if len(candidate) > MAX_PACKET_CHARS:
            break
        lines.extend(block)
        included.append(event)
    if not included:
        return "", []
    text = "\n".join(header + lines)
    remaining = len(events) - len(included)
    if remaining > 0:
        note = (f"... plus {remaining} pending event(s) kept for the next dispatch")
        if len(text) + len(note) + 1 <= MAX_PACKET_CHARS:
            text = text + "\n" + note
    return text, included


def dispatch_task(board_file, task_id: str, now=None, timeout=None, cli_runner=None) -> dict:
    """One bounded queue dispatch; never raises for transport failure."""
    now = time.time() if now is None else now
    timeout = DISPATCH_TIMEOUT_SECONDS if timeout is None else float(timeout)
    board, problem = read_board(board_file)
    if board is None:
        return {"ok": False, "dispatched": False, "status": "unknown",
                "error": f"board state is {problem}: {board_file}"}
    card = (board.get("cards") or {}).get(task_id)
    if not isinstance(card, dict):
        return {"ok": True, "dispatched": False, "reason": "task is not registered"}
    if card.get("transport") != TRANSPORT_CLI_QUEUE:
        return {"ok": True, "dispatched": False, "reason": "transport is not cli-queue"}
    if card.get("paused"):
        return {"ok": True, "dispatched": False, "reason": "task paused"}
    thread = card.get("ownerThread")
    if not isinstance(thread, str) or not THREAD_RE.fullmatch(thread):
        return {"ok": False, "dispatched": False, "status": "invalid-owner",
                "error": "card has no exact owner thread UUID; refusing to guess"}
    paused, pause_problem = route_paused(thread)
    if pause_problem is not None:
        return {"ok": False, "dispatched": False, "status": "unknown",
                "error": f"route pause state is {pause_problem}"}
    if paused:
        return {"ok": True, "dispatched": False, "reason": "session paused by interrupt"}
    queue, qproblem = read_queue(board_file)
    if queue is None:
        return {"ok": False, "dispatched": False, "status": "unknown",
                "error": f"queue state is {qproblem}: {board_file}"}
    entry = _queue_entry(queue, task_id)
    claims = _normalize_claims(entry, now)
    eligible = []
    for event in pending_events(card):
        claim = claims.get(event.get("id"))
        if not isinstance(claim, dict):
            eligible.append(event)
            continue
        status = claim.get("status")
        if status == "failed" and int(claim.get("attempts") or 0) < MAX_TRANSPORT_RETRIES:
            eligible.append(event)
        # queued/inflight/uncertain/failed-final are not automatically resent
    if not eligible:
        return {"ok": True, "dispatched": False, "reason": "no dispatchable events",
                "queue": queue_view(board_file, task_id)}
    text, included = build_packet(card, eligible)
    if not included:
        return {"ok": True, "dispatched": False, "reason": "packet did not fit"}
    packet_id = uuid.uuid4().hex
    requested = [event["id"] for event in included]
    claimed = _claim_queue(board_file, task_id, requested, now, packet_id)
    if claimed is None:
        return {"ok": True, "dispatched": False, "reason": "queue claim lock held; retry next tick"}
    if not claimed:
        return {"ok": True, "dispatched": False, "reason": "events already claimed or queued"}
    if set(claimed) != set(requested):
        included = [event for event in included if event["id"] in claimed]
        text, included = build_packet(card, included)
        if not included:
            return {"ok": True, "dispatched": False, "reason": "claim changed; retry next tick"}
    argv = [card.get("codexBin") or DEFAULT_CODEX_BIN, "--disable", "daemon_auto_start", "queue",
            "--thread", thread, "--message", text]
    result = (cli_runner or _run_queue_cli)(argv, timeout)
    _finish_queue(board_file, task_id, [event["id"] for event in included], result, now, packet_id)
    status = result.get("status", "failed")
    return {"ok": status == "queued", "dispatched": True, "taskId": task_id,
            "thread": thread, "packetId": packet_id, "status": status,
            "eventIds": [event["id"] for event in included],
            "exitCode": result.get("exitCode"), "timedOut": bool(result.get("timedOut")),
            "error": result.get("error"),
            "receipt": {"argv0": result.get("argv0"), "exitCode": result.get("exitCode"),
                        "timedOut": bool(result.get("timedOut")),
                        "outputSha256": result.get("outputSha256"),
                        "outputExcerpt": result.get("outputExcerpt")},
            "queue": queue_view(board_file, task_id),
            "limitations": [QUEUE_LIMITATION]}


def supervisor_tick(task: dict):
    """Refresh a registered task and dispatch new actionable events via cli-queue."""
    try:
        refresh = refresh_registered_task(task, block=False, source="supervisor")
    except Exception as exc:  # noqa: BLE001 - monitor errors must not kill Pi
        try:
            record_monitor_error(task, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "refreshed": False, "reason": "monitor error"}
    dispatch = None
    common_raw = task.get("commonDir") if isinstance(task, dict) else None
    if isinstance(common_raw, str) and common_raw.strip():
        board_file = board_file_for_common(Path(common_raw))
        if board_file.is_file():
            try:
                dispatch = dispatch_task(board_file, task.get("task"))
            except Exception as exc:  # noqa: BLE001
                try:
                    record_monitor_error(task, f"dispatch error: {type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass
                dispatch = {"ok": False, "dispatched": False, "error": str(exc)}
    return {"ok": True, "refresh": refresh, "dispatch": dispatch}


def route_summary(thread: str) -> dict | None:
    """Bounded recovery evidence for one registered thread; None when not routed."""
    route, problem = read_route(thread)
    if route is None:
        return None if problem == "missing" else {"thread": thread, "lines": [
            f"route state is {problem}"], "paused": False, "problem": problem}
    board_file = Path(str(route.get("boardPath", "")))
    lines = []
    board, board_problem = read_board(board_file)
    if board is None:
        lines.append(f"board state is {board_problem}")
    else:
        monitors, monitor_problem = read_monitors(board_file)
        lease = monitor_lease_seconds()
        now = time.time()
        problems = []
        for task_id in (route.get("taskIds") or [])[:5]:
            card = (board.get("cards") or {}).get(task_id)
            if not isinstance(card, dict):
                problems.append(f"task={task_id} state=not-registered")
                continue
            state = (card.get("pi") or {}).get("state")
            view = queue_view(board_file, task_id)
            record = monitor_for(monitors, task_id)
            if monitors is None:
                monitor = f"unknown ({monitor_problem})"
            elif not isinstance(record, dict):
                monitor = "missing"
            elif record.get("error"):
                monitor = f"error ({_text(record.get('error'), 120)})"
            else:
                refreshed = record.get("refreshedAt")
                if isinstance(refreshed, bool) or not isinstance(refreshed, (int, float)):
                    monitor = "corrupt"
                elif now - refreshed > lease:
                    monitor = f"stale ({int(now - refreshed)}s)"
                else:
                    monitor = "fresh"
            interesting = (bool(card.get("paused")) or monitor != "fresh"
                           or int(view.get("uncertain") or 0) > 0
                           or int(view.get("failed") or 0) > 0)
            if interesting:
                problems.append(
                    f"task={task_id} state={state} paused={bool(card.get('paused'))} "
                    f"pending={len(pending_events(card))} queued={view.get('queued')} "
                    f"uncertain={view.get('uncertain')} failed={view.get('failed')} "
                    f"monitor={monitor}")
                for failure in view.get("failures") or []:
                    problems.append(
                        f"  transport {failure.get('status')} at {failure.get('at')}: "
                        f"{_text(failure.get('error'), 120)} "
                        f"(rearm: python3 {Path(__file__).resolve()} rearm --repo "
                        f"{card.get('repo')} --task {task_id})")
        lines.extend(problems)
    paused, pause_problem = route_paused(thread)
    if pause_problem is not None:
        lines.append(f"route pause state is {pause_problem}")
    return {"thread": thread, "lines": lines[:12], "paused": bool(paused),
            "limitations": [QUEUE_LIMITATION], "note":
                "an in-process monitor cannot report its own SIGKILL; stale evidence is exposed "
                "only on the next recovery interaction, not detected automatically"}


# ---------------------------------------------------------------------------
# compact read views
# ---------------------------------------------------------------------------

def compact_event(event: dict) -> dict:
    return {"id": event.get("id"), "seq": event.get("seq"), "round": event.get("round"),
            "kind": event.get("kind"), "summary": event.get("summary"),
            "question": event.get("question"), "candidate": event.get("candidate"),
            "evidence": event.get("evidence"), "createdAt": event.get("createdAt"),
            "handled": bool(event.get("handled")), "decision": event.get("decision"),
            "reviewedHead": event.get("reviewedHead")}


def compact_card(card: dict, max_events: int = 5) -> dict:
    events = [event for event in card.get("events", []) if isinstance(event, dict)]
    pending = [event for event in events if not event.get("handled")]
    handled = [event for event in events if event.get("handled")]
    return {
        "taskId": card.get("taskId"), "ownerThread": card.get("ownerThread"),
        "title": card.get("title"), "goal": card.get("goal"),
        "briefRef": card.get("briefRef"), "planRef": card.get("planRef"),
        "repo": card.get("repo"), "worktree": card.get("worktree"),
        "transport": card.get("transport"), "codexBin": card.get("codexBin"),
        "paused": bool(card.get("paused")), "pausedAt": card.get("pausedAt"),
        "pauseNote": card.get("pauseNote"), "pi": card.get("pi"),
        "evidence": card.get("evidence"), "check": card.get("check"),
        "attention": card.get("attention"), "codex": card.get("codex"),
        "pendingEvents": [compact_event(event) for event in pending[:max_events]],
        "pendingCount": len(pending),
        "handledCount": len(card.get("handled") or {}),
        "overflow": card.get("overflow"),
        "recentHandled": [compact_event(event) for event in handled[-2:]],
    }


def select_cards(board: dict, thread=None, task_id=None, include_all=False) -> list:
    cards = board.get("cards") or {}
    if task_id is not None:
        card = cards.get(task_id)
        return [card] if isinstance(card, dict) else []
    if thread:
        return [card for card in cards.values()
                if isinstance(card, dict) and card.get("ownerThread") == thread]
    if include_all:
        return [card for card in cards.values() if isinstance(card, dict)][:50]
    raise ValueError("provide --task, --thread or --all")


def _monitor_view(board_file, task_ids) -> dict:
    monitors, problem = read_monitors(board_file)
    lease = monitor_lease_seconds()
    now = time.time()
    view = {}
    for task_id in task_ids:
        if monitors is None:
            view[task_id] = {"status": f"unknown ({problem})", "leaseSeconds": lease}
            continue
        record = monitor_for(monitors, task_id)
        if not isinstance(record, dict):
            view[task_id] = {"status": "missing", "leaseSeconds": lease}
            continue
        refreshed = record.get("refreshedAt")
        entry = {"status": "healthy", "refreshedAt": refreshed,
                 "ageSeconds": int(now - refreshed) if isinstance(refreshed, (int, float))
                 and not isinstance(refreshed, bool) else None,
                 "source": record.get("source"), "error": record.get("error"),
                 "healthy": bool(record.get("healthy")), "leaseSeconds": lease}
        if entry["error"]:
            entry["status"] = "error"
        elif entry["ageSeconds"] is not None and entry["ageSeconds"] > lease:
            entry["status"] = "stale"
        view[task_id] = entry
    return view


def _decide_hint(repo, task_id, event: dict) -> str:
    head = (event.get("candidate") or {}).get("head")
    if event.get("kind") in REVIEW_KINDS and isinstance(head, str) and FULL_OID_RE.fullmatch(head):
        decision = "accept|reject|changes_requested"
        suffix = f" --reviewed-head {head}"
    else:
        decision = "resolve|reject"
        suffix = ""
    return (f'python3 {shlex.quote(str(Path(__file__).resolve()))} decide '
            f'--repo {shlex.quote(str(repo))} --task {shlex.quote(str(task_id))} '
            f'--event-id {event.get("id")} --decision {decision}{suffix}')


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------

def register_task(repo, task, thread=None, transport=TRANSPORT_OFFLINE, codex_bin=None,
                  title=None, goal=None, brief_ref=None, plan_ref=None, codex_task_id=None,
                  now=None) -> dict:
    now = time.time() if now is None else now
    root = canonical_root(Path(repo))
    common = git_common_dir(root)
    task_id = require_task_arg(task)
    task_dir = task_dir_for(common, task_id)
    if not task_dir.is_dir():
        raise ValueError(f"unknown task {task_id!r}; no evidence at {task_dir}")
    frozen = read_json(task_dir / "task.json", None)
    if not isinstance(frozen, dict) or frozen.get("task") != task_id:
        raise ValueError(f"task {task_id!r} has no readable task.json; inspect {task_dir}")
    require_allowed_model(frozen.get("model"), "board registration")
    transport = _validate_transport(transport)
    owner_thread = _validate_thread(thread, required=(transport == TRANSPORT_CLI_QUEUE))
    resolved_bin = None
    if codex_bin:
        candidate = Path(str(codex_bin)).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError(f"--codex-bin is not an executable file: {candidate}")
        resolved_bin = str(candidate.resolve())
    elif transport == TRANSPORT_CLI_QUEUE:
        resolved_bin = DEFAULT_CODEX_BIN
    board_file = board_file_for_common(common)
    board_file.parent.mkdir(parents=True, exist_ok=True)
    fd = lock_fd(board_file.with_name(BOARD_LOCK), blocking=True, timeout=10)
    try:
        board = None
        if board_file.exists():
            board, problem = read_board(board_file)
            if board is None:
                raise ValueError(f"existing board is {problem}: {board_file}")
        if board is None:
            board = {"schemaVersion": SCHEMA_VERSION, "revision": 0,
                     "createdAt": now, "updatedAt": now, "cards": {}}
        cards = board.setdefault("cards", {})
        card = cards.get(task_id)
        if not isinstance(card, dict):
            card = _new_card(task_id, owner_thread, title or task_id, goal or "", brief_ref,
                             plan_ref, root, common, frozen.get("worktree"), transport,
                             resolved_bin, now)
            cards[task_id] = card
        else:
            card["ownerThread"] = owner_thread or card.get("ownerThread")
            card["codexTaskId"] = codex_task_id or card.get("codexTaskId") \
                or owner_thread or task_id
            card["transport"] = transport
            card["codexBin"] = resolved_bin if transport == TRANSPORT_CLI_QUEUE \
                else card.get("codexBin")
            if title:
                card["title"] = _text(title, 200)
            if goal:
                card["goal"] = _text(goal, 600)
            if brief_ref:
                card["briefRef"] = _text(brief_ref, 400)
            if plan_ref:
                card["planRef"] = _text(plan_ref, 400)
            card["repo"] = str(root)
            card["commonDir"] = str(common)
            card["worktree"] = frozen.get("worktree")
            card["updatedAt"] = now
        board["revision"] = int(board.get("revision") or 0) + 1
        board["updatedAt"] = now
        _write_board(board_file, board)
    finally:
        os.close(fd)
    route = None
    if transport == TRANSPORT_CLI_QUEUE:
        route = register_route(owner_thread, board_file, [task_id], transport, now)
    try:
        refresh_with_status(board_file, task_id, build_status(str(root), task_id), now,
                            block=True, source="register")
    except (ValueError, OSError, LockHeld):
        pass
    dispatch = None
    if transport == TRANSPORT_CLI_QUEUE:
        dispatch = dispatch_task(board_file, task_id, now=now)
    queue = queue_view(board_file, task_id)
    return {
        "ok": True, "taskId": task_id, "ownerThread": owner_thread,
        "mode": transport, "transport": transport, "codexBin": card.get("codexBin"),
        "boardPath": str(board_file), "queuePath": str(queue_paths(board_file)[0]),
        "routePath": str(route_paths(owner_thread)[0]) if route else None,
        "revision": board.get("revision"),
        "queueArgvTemplate": ([card.get("codexBin") or DEFAULT_CODEX_BIN, "--disable",
                               "daemon_auto_start", "queue", "--thread", owner_thread,
                               "--message", "<bounded runtime-handoff packet>"]
                              if transport == TRANSPORT_CLI_QUEUE else None),
        "refreshDispatch": dispatch,
        "queue": queue,
        "testCommands": [
            f"python3 {shlex.quote(str(Path(__file__).resolve()))} refresh --repo "
            f"{shlex.quote(str(root))} --task {task_id}",
            f"python3 {shlex.quote(str(Path(__file__).resolve()))} dispatch --repo "
            f"{shlex.quote(str(root))} --task {task_id}",
        ],
        "limitations": [QUEUE_LIMITATION],
        "note": "registration refreshes and dispatches an already-terminal task; offline mode never "
                "invokes Codex. Reading or delivering an event does not handle or accept it.",
    }


def _resolve_commit(worktree, object_id):
    """Exact full object id resolved as a commit in the registered worktree."""
    if not isinstance(object_id, str) or not FULL_OID_RE.fullmatch(object_id):
        return None, "must be a full 40- or 64-hex object id"
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "--quiet",
             f"{object_id}^{{commit}}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not be resolved with bounded git: {exc}"
    if proc.returncode != 0:
        return None, "is not a commit object in the registered repository/worktree"
    resolved = proc.stdout.strip().lower()
    if not FULL_OID_RE.fullmatch(resolved):
        return None, "git returned an unexpected object id"
    return resolved, None


def decide(repo, task, event_id, decision, reviewed_head=None, note=None, now=None) -> dict:
    now = time.time() if now is None else now
    event_id = str(event_id)
    if not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("event id must be a 64-character lowercase hex digest")
    mapped = DECISIONS.get(decision)
    if mapped is None:
        raise ValueError("decision must be accept, reject, changes_requested or resolve")
    _root, _common, board_file = board_file_for_repo(repo)
    task_id = require_task_arg(task)
    fd = lock_fd(board_file.with_name(BOARD_LOCK), blocking=True, timeout=10)
    try:
        board, problem = read_board(board_file)
        if board is None:
            raise ValueError(f"board state is {problem}: {board_file}")
        card = board["cards"].get(task_id)
        if not isinstance(card, dict):
            raise ValueError(f"task {task_id!r} is not registered on this board")
        event = find_event(card, event_id)
        history = (card.get("handled") or {}).get(event_id) if event is None else None
        if event is None and not isinstance(history, dict):
            raise ValueError(f"unknown event {event_id} for task {task_id!r}")
        if event is None:
            same = history.get("decision") == mapped and (
                mapped != "accepted" or history.get("reviewedHead") == reviewed_head)
            if same:
                return {"ok": True, "idempotent": True, "taskId": task_id, "eventId": event_id,
                        "decision": mapped, "revision": board.get("revision")}
            raise ValueError(f"event {event_id} was already decided as "
                             f"{history.get('decision')!r}; refusing to overwrite or replay it "
                             "with a different decision/reviewed head")
        kind = event.get("kind")
        if event.get("handled"):
            same = event.get("decision") == mapped and (
                mapped != "accepted" or event.get("reviewedHead") == reviewed_head)
            if same:
                return {"ok": True, "idempotent": True, "taskId": task_id, "eventId": event_id,
                        "decision": mapped, "revision": board.get("revision")}
            if mapped == "accepted" and event.get("reviewedHead") != reviewed_head:
                raise ValueError("conflicting replay: this event was already accepted with a "
                                 "different reviewed head")
            raise ValueError(f"event {event_id} is already handled as "
                             f"{event.get('decision')!r}; refusing to overwrite a decision")
        if mapped == "accepted":
            if kind not in REVIEW_KINDS:
                raise ValueError(f"event kind {kind!r} is a fault/observation; "
                                 "use --decision resolve, not accept")
            event_head = (event.get("candidate") or {}).get("head")
            worktree = card.get("worktree") or _root
            resolved_reviewed, problem = _resolve_commit(worktree, reviewed_head)
            if problem is not None:
                raise ValueError(f"reviewed head {reviewed_head!r} {problem}")
            resolved_event, problem = _resolve_commit(worktree, event_head)
            if problem is not None:
                raise ValueError(f"event candidate head {event_head!r} {problem}")
            if resolved_reviewed != resolved_event:
                raise ValueError("reviewed head does not resolve to the event candidate commit")
            reviewed_head = resolved_reviewed
        elif reviewed_head is not None and not FULL_OID_RE.fullmatch(str(reviewed_head)):
            raise ValueError("--reviewed-head must be a full 40- or 64-hex object id")
        latest_review = None
        for item in card.get("events", []):
            if isinstance(item, dict) and item.get("kind") in REVIEW_KINDS \
                    and not item.get("handled"):
                if latest_review is None or (item.get("seq") or 0) > (latest_review.get("seq") or 0):
                    latest_review = item
        card_round = (card.get("pi") or {}).get("round")
        is_current = (kind in REVIEW_KINDS and latest_review is not None
                      and latest_review.get("id") == event_id
                      and event.get("round") == card_round)
        event.update(handled=True, handledAt=now, decision=mapped,
                     reviewedHead=str(reviewed_head) if reviewed_head else None,
                     note=_text(note, MAX_NOTE) if note else None)
        card.setdefault("handled", {})[event_id] = {
            "at": now, "decision": mapped,
            "reviewedHead": str(reviewed_head) if reviewed_head else None,
            "round": event.get("round")}
        _prune_events(card)
        _update_overflow(card, now)
        codex = card.setdefault("codex", _default_codex())
        codex["lastEventId"] = event_id
        codex["lastDecision"] = mapped
        codex["lastDecisionAt"] = now
        if is_current:
            codex["review"] = mapped
            if mapped == "accepted":
                codex["reviewedHead"] = str(reviewed_head)
            codex["decidedAt"] = now
        else:
            history_entry = {"eventId": event_id, "decision": mapped, "at": now,
                             "round": event.get("round"),
                             "reviewedHead": str(reviewed_head) if reviewed_head else None}
            codex["history"] = (codex.get("history") or [])[-19:] + [history_entry]
        board["revision"] = int(board.get("revision") or 0) + 1
        board["updatedAt"] = now
        _write_board(board_file, board)
        return {"ok": True, "idempotent": False, "taskId": task_id, "eventId": event_id,
                "decision": mapped, "reviewedHead": reviewed_head, "aggregate": is_current,
                "revision": board["revision"], "pendingCount": len(pending_events(card)),
                "note": "decision binds this exact event/candidate/round; accepted is explicit "
                        "main review of the current review event, never exit 0"}
    finally:
        os.close(fd)


def set_paused(repo, task, paused: bool, note=None, now=None) -> dict:
    now = time.time() if now is None else now
    _root, _common, board_file = board_file_for_repo(repo)
    task_id = require_task_arg(task)
    fd = lock_fd(board_file.with_name(BOARD_LOCK), blocking=True, timeout=10)
    try:
        board, problem = read_board(board_file)
        if board is None:
            raise ValueError(f"board state is {problem}: {board_file}")
        card = board["cards"].get(task_id)
        if not isinstance(card, dict):
            raise ValueError(f"task {task_id!r} is not registered on this board")
        card["paused"] = bool(paused)
        card["pausedAt"] = now if paused else None
        card["pauseNote"] = _text(note, MAX_NOTE) if note else None
        card["updatedAt"] = now
        board["revision"] = int(board.get("revision") or 0) + 1
        board["updatedAt"] = now
        _write_board(board_file, board)
        return {"ok": True, "taskId": task_id, "paused": bool(paused),
                "revision": board["revision"],
                "note": ("paused: queue dispatch stops and progress events do not resume work"
                         if paused else "resumed explicitly; pending events and decisions are unchanged")}
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_register(args) -> dict:
    return register_task(args.repo, args.task, thread=args.thread, transport=args.transport,
                         codex_bin=args.codex_bin, title=args.title, goal=args.goal,
                         brief_ref=args.brief_ref, plan_ref=args.plan_ref,
                         codex_task_id=args.codex_task_id)


def cmd_rearm(args) -> dict:
    _root, _common, board_file = board_file_for_repo(args.repo)
    task_id = require_task_arg(args.task)
    return _clear_queue_claims(board_file, task_id, event_id=args.event_id)


def cmd_refresh(args) -> dict:
    root, _common, board_file = board_file_for_repo(args.repo)
    task_id = require_task_arg(args.task)
    return refresh_with_status(board_file, task_id, build_status(str(root), task_id),
                               block=True, source="cli")


def cmd_dispatch(args) -> dict:
    _root, _common, board_file = board_file_for_repo(args.repo)
    task_id = require_task_arg(args.task)
    return dispatch_task(board_file, task_id, timeout=args.timeout)


def cmd_show(args) -> dict:
    _root, _common, board_file = board_file_for_repo(args.repo)
    board, problem = read_board(board_file)
    if board is None:
        raise ValueError(f"board state is {problem}: {board_file}")
    thread = _validate_thread(args.thread, required=False) if args.thread else None
    cards = select_cards(board, thread=thread, task_id=args.task, include_all=args.all)
    task_ids = [card.get("taskId") for card in cards]
    return {"ok": True, "revision": board.get("revision"), "count": len(cards),
            "cards": [compact_card(card) for card in cards],
            "monitor": _monitor_view(board_file, task_ids),
            "queue": {task_id: queue_view(board_file, task_id) for task_id in task_ids},
            "note": "compact projection only; no raw logs and no entire history"}


def cmd_packet(args) -> dict:
    _root, _common, board_file = board_file_for_repo(args.repo)
    task_id = require_task_arg(args.task)
    board, problem = read_board(board_file)
    if board is None:
        raise ValueError(f"board state is {problem}: {board_file}")
    card = board["cards"].get(task_id)
    if not isinstance(card, dict):
        raise ValueError(f"task {task_id!r} is not registered on this board")
    events = pending_events(card)
    if args.event_id:
        events = [event for event in events if event.get("id") == args.event_id]
    if not events:
        return {"ok": True, "taskId": task_id, "pendingCount": 0,
                "transport": card.get("transport"),
                "monitor": _monitor_view(board_file, [task_id]).get(task_id),
                "queue": queue_view(board_file, task_id),
                "note": "no unhandled events for this task"}
    text, included = build_packet(card, events)
    return {"ok": True, "taskId": task_id, "revision": board.get("revision"),
            "title": card.get("title"), "goal": card.get("goal"),
            "transport": card.get("transport"), "ownerThread": card.get("ownerThread"),
            "pendingCount": len(events), "overflow": card.get("overflow"),
            "monitor": _monitor_view(board_file, [task_id]).get(task_id),
            "queue": queue_view(board_file, task_id),
            "includedEventIds": [event["id"] for event in included],
            "packet": text,
            "events": [compact_event(event) for event in events[:5]],
            "limitations": [QUEUE_LIMITATION],
            "note": "reading/delivering does not handle or accept; use decide"}


def cmd_decide(args) -> dict:
    return decide(args.repo, args.task, args.event_id, args.decision,
                  reviewed_head=args.reviewed_head, note=args.note)


def cmd_pause(args) -> dict:
    return set_paused(args.repo, args.task, True, note=args.note)


def cmd_resume(args) -> dict:
    result = set_paused(args.repo, args.task, False, note=args.note)
    thread = args.thread or os.environ.get("CODEX_THREAD_ID")
    if thread:
        result["routePause"] = resume_route(thread)
    return result


def cmd_recover(args) -> dict:
    thread = _validate_thread(args.thread or os.environ.get("CODEX_THREAD_ID"))
    summary = route_summary(thread)
    if summary is None:
        raise ValueError(f"no queue route is registered for thread {thread}")
    return {"ok": True, **summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi_board.py",
        description="Opt-in structured Codex-Pi board projection with the verified CLI queue "
                    "transport. Never invokes a Codex model or agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="bind an existing task to a thread and transport")
    register.add_argument("--repo", required=True)
    register.add_argument("--task", required=True)
    register.add_argument("--transport", choices=TRANSPORTS, default=TRANSPORT_OFFLINE)
    register.add_argument("--thread", help="exact owner desktop thread UUID (required for cli-queue)")
    register.add_argument("--codex-bin", help="resolved executable for queue commands")
    register.add_argument("--title")
    register.add_argument("--goal")
    register.add_argument("--brief-ref")
    register.add_argument("--plan-ref")
    register.add_argument("--codex-task-id")
    register.set_defaults(func=cmd_register)

    rearm = sub.add_parser("rearm", help="explicitly requeue after a lost/interrupted owner turn")
    rearm.add_argument("--repo", required=True)
    rearm.add_argument("--task", required=True)
    rearm.add_argument("--event-id")
    rearm.set_defaults(func=cmd_rearm)

    refresh = sub.add_parser("refresh", help="one-shot bounded card projection refresh")
    refresh.add_argument("--repo", required=True)
    refresh.add_argument("--task", required=True)
    refresh.set_defaults(func=cmd_refresh)

    dispatch = sub.add_parser("dispatch", help="one bounded cli-queue dispatch of new events")
    dispatch.add_argument("--repo", required=True)
    dispatch.add_argument("--task", required=True)
    dispatch.add_argument("--timeout", type=float)
    dispatch.set_defaults(func=cmd_dispatch)

    show = sub.add_parser("show", help="bounded compact cards for one thread or task")
    show.add_argument("--repo", required=True)
    show.add_argument("--thread")
    show.add_argument("--task")
    show.add_argument("--all", action="store_true")
    show.set_defaults(func=cmd_show)

    packet = sub.add_parser("packet", help="bounded runtime-handoff packet for one task")
    packet.add_argument("--repo", required=True)
    packet.add_argument("--task", required=True)
    packet.add_argument("--event-id")
    packet.set_defaults(func=cmd_packet)

    decide = sub.add_parser("decide", help="handle one exact event with an explicit decision")
    decide.add_argument("--repo", required=True)
    decide.add_argument("--task", required=True)
    decide.add_argument("--event-id", required=True)
    decide.add_argument("--decision", required=True, choices=sorted(DECISIONS))
    decide.add_argument("--reviewed-head")
    decide.add_argument("--note")
    decide.set_defaults(func=cmd_decide)

    pause = sub.add_parser("pause", help="persist explicit pause; queue dispatch stops")
    pause.add_argument("--repo", required=True)
    pause.add_argument("--task", required=True)
    pause.add_argument("--note")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="explicitly resume a card and/or route pause")
    resume.add_argument("--repo", required=True)
    resume.add_argument("--task", required=True)
    resume.add_argument("--thread", help="also clear an interrupt pause for this thread")
    resume.add_argument("--note")
    resume.set_defaults(func=cmd_resume)

    recover = sub.add_parser("recover", help="bounded recovery evidence for one thread route")
    recover.add_argument("--thread")
    recover.set_defaults(func=cmd_recover)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.func(args)
    except (ValueError, LockHeld, OSError) as exc:
        print(f"pi_board: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
