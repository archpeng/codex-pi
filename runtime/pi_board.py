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
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from pi_task import (ACTIVE_STATES, TERMINAL_STATES, TASK_RE, LockHeld, atomic,  # noqa: E402
                     build_status, canonical_root, git_common_dir, lock_fd, lock_is_held,
                     normalize_candidate, probe_worktree_head, read_json,
                     require_allowed_model, require_task_arg, task_dir_for, terminate)

from pi_takeover import FAILURE_KINDS, normalize_review_limit, review_policy

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
MAX_PROGRESS_NOTIFICATIONS = 2
PROGRESS_NOTIFY_INTERVAL_SECONDS = 600.0
PROGRESS_ANOMALY_SECONDS = 180.0
MAX_NOTIFY_MILESTONES = 50
MAX_NOTIFY_ABNORMAL = 50
PROGRESS_EVENT_KIND = "progress_update"
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
QUEUE_LIMITATION = ("the CLI queue has no caller-provided idempotency key; a timeout, crash or "
                    "nonzero exit after spawn is recorded as uncertain and requires explicit "
                    "rearm; explicit recovery of an uncertain delivery may duplicate it and is "
                    "never an exactly-once guarantee")


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
    """Finalize a queue result only while this packet still owns each claim."""
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
        status = result.get("status", "uncertain")
        finalized = []
        skipped = []
        for event_id in event_ids:
            claim = claims.get(event_id)
            if not isinstance(claim, dict) or claim.get("packetId") != packet_id \
                    or claim.get("status") != "inflight":
                # A recovery/rearm or newer packet owns this event now; the late
                # result must never overwrite the newer claim.
                skipped.append(event_id)
                continue
            claim.update({"status": status, "at": now,
                          "lastError": _text(result.get("error"), 300) if result.get("error")
                                       else None,
                          "exitCode": result.get("exitCode"),
                          "notStarted": bool(result.get("notStarted"))})
            claims[event_id] = claim
            finalized.append(event_id)
        if finalized:
            entry["lastStatus"] = status
            entry["lastDispatch"] = {
                "at": now, "packetId": packet_id, "status": status,
                "eventIds": [event_id for event_id in finalized if isinstance(event_id, str)],
                "exitCode": result.get("exitCode"), "timedOut": bool(result.get("timedOut")),
                "notStarted": bool(result.get("notStarted")),
                "outputSha256": result.get("outputSha256"),
                "outputExcerpt": result.get("outputExcerpt"),
                "argv0": result.get("argv0"),
            }
            if status in ("failed", "uncertain"):
                entry.setdefault("failures", []).append({
                    "at": now, "status": status, "packetId": packet_id,
                    "exitCode": result.get("exitCode"),
                    "error": _text(result.get("error") or status, 300),
                    "eventIds": [event_id for event_id in finalized
                                 if isinstance(event_id, str)][:10]})
                entry["failures"] = entry["failures"][-MAX_QUEUE_FAILURES:]
        if skipped:
            entry["lastSkippedFinalize"] = {
                "at": now, "packetId": packet_id,
                "eventIds": [event_id for event_id in skipped if isinstance(event_id, str)][:10],
                "note": "late queue result did not own the current claim; newer state was preserved"}
        entry["updatedAt"] = now
        _write_queue(board_file, queue)
    finally:
        os.close(fd)


def _clear_queue_claims(board_file, task_id: str, event_id=None, now=None) -> dict:
    """Explicit recovery rearm. Live inflight claims are never cleared; stale
    inflight becomes uncertain and needs a further explicit recovery call.
    """
    now = time.time() if now is None else now
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
        cleared, refused, marked_uncertain = [], [], []
        targets = [event_id] if event_id is not None else list(claims)
        for target in targets:
            claim = claims.get(target)
            if claim is None:
                continue
            if not isinstance(claim, dict):
                claims[target] = {"status": "uncertain", "at": now,
                                  "lastError": "corrupt claim record"}
                marked_uncertain.append(target)
                continue
            if claim.get("status") == "inflight":
                at = claim.get("at")
                if isinstance(at, bool) or not isinstance(at, (int, float)) \
                        or now - at > QUEUE_STALE_INFLIGHT_SECONDS:
                    claim.update(status="uncertain", at=now,
                                 lastError="stale inflight claim; explicit recovery required "
                                           "before requeue")
                    marked_uncertain.append(target)
                else:
                    refused.append(target)
                continue
            claims.pop(target, None)
            cleared.append(target)
        if event_id is not None and event_id in refused:
            raise ValueError("refusing to clear a live inflight claim; wait for the result or for "
                             "the claim to age into uncertain, then rearm explicitly")
        if cleared:
            entry["lastStatus"] = "rearmed"
        entry["updatedAt"] = now
        _write_queue(board_file, queue)
        return {"ok": not refused, "taskId": task_id, "clearedClaims": len(cleared),
                "refusedInflight": refused, "markedUncertain": marked_uncertain,
                "eventId": event_id,
                "note": "live inflight claims are never cleared; stale inflight becomes uncertain "
                        "and needs a further explicit recovery; recovery may duplicate an "
                        "uncertain delivery and is never exactly-once"}
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
              transport, codex_bin, now, review_pin=None) -> dict:
    card = {
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
    if isinstance(review_pin, dict):
        card["reviewPolicyPin"] = dict(review_pin)
    return card


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


def _review_episode(card: dict) -> int:
    """Durable ready-episode number for review-event identities.

    The counter only advances when a pending review event is invalidated. It is
    stored on the card so a recovered ready state on the SAME round/candidate
    publishes exactly one new event while unchanged refreshes stay idempotent.
    """
    value = card.get("reviewEpisode")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10 ** 9:
        return value
    return 0


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


def _phase_projection(card: dict, status: dict, now: float):
    """Stable phase card projection; returns (phase_dict_or_None, changed)."""
    phase = status.get("phase")
    if not isinstance(phase, dict) or not phase.get("phaseId"):
        return None, bool(card.get("phase"))
    readiness = phase.get("readiness") or {}
    auto = phase.get("autoContinue") or {}
    candidate = _phase_candidate_head(status)
    scope_value = readiness.get("scope")
    if isinstance(scope_value, dict):
        scope_value = scope_value.get("status")
    existing = card.get("phase") if isinstance(card.get("phase"), dict) else {}
    phase_evidence = phase.get("evidence") if isinstance(phase.get("evidence"), dict) else {}
    resource_evidence = phase_evidence.get("resource")
    same_identity = (existing.get("phaseId") == phase.get("phaseId")
                     and existing.get("contractHash") == phase.get("contractSha256"))
    if same_identity and existing.get("status") == "accepted" \
            and existing.get("acceptedHead") == candidate:
        state = "accepted"
    elif same_identity and existing.get("status") == "changes_requested" \
            and existing.get("candidate") == candidate:
        state = "changes_requested"
    elif same_identity and existing.get("status") == "rejected" \
            and existing.get("candidate") == candidate:
        state = "rejected"
    elif readiness.get("status") == "ready" and status.get("state") == "completed":
        state = "review_ready"
    elif auto.get("status") == "started":
        state = "continuing"
    elif status.get("state") in ACTIVE_STATES:
        state = "executing"
    else:
        state = "blocked"
    new_phase = {
        "phaseId": phase.get("phaseId"), "contractHash": phase.get("contractSha256"),
        "contractRef": phase.get("contractRef"), "baselineCommit": phase.get("baselineCommit"),
        "candidate": candidate, "status": state,
        "acceptedHead": existing.get("acceptedHead") if same_identity else None,
        "acceptedAt": existing.get("acceptedAt") if same_identity else None,
        "reviewEventId": existing.get("reviewEventId") if same_identity else None,
        "decidedEventId": existing.get("decidedEventId") if same_identity else None,
        "lastDecision": existing.get("lastDecision") if same_identity else None,
        "readiness": {"status": readiness.get("status"),
                      "coverage": readiness.get("coverage"),
                      "writerFree": readiness.get("writerFree"),
                      "execution": (readiness.get("execution") or {}).get("status"),
                      "resource": resource_evidence,
                      "gaps": [{"id": item.get("id"), "status": item.get("status"),
                                "reason": item.get("reason")}
                               for item in (readiness.get("gaps") or [])[:10]],
                      "scope": scope_value,
                      "generatedAt": readiness.get("generatedAt")},
        "budget": phase.get("budget") or {},
        "autoContinue": auto or None,
        "updatedAt": now,
    }
    stable_keys = ("phaseId", "contractHash", "contractRef", "baselineCommit", "candidate",
                   "status", "acceptedHead", "acceptedAt", "reviewEventId", "decidedEventId")
    changed = any(existing.get(key) != new_phase.get(key) for key in stable_keys)
    if not changed:
        old_readiness = existing.get("readiness") or {}
        new_readiness = new_phase.get("readiness") or {}
        if (old_readiness.get("status"), old_readiness.get("coverage"), old_readiness.get("scope"),
                (old_readiness.get("execution") or {}), (old_readiness.get("resource") or {}).get("status")) \
                != (new_readiness.get("status"), new_readiness.get("coverage"),
                    new_readiness.get("scope"), (new_readiness.get("execution") or {}),
                    (new_readiness.get("resource") or {}).get("status")):
            changed = True
        old_auto = existing.get("autoContinue") or {}
        new_auto = new_phase.get("autoContinue") or {}
        if (old_auto.get("status"), old_auto.get("reason"), old_auto.get("round")) \
                != (new_auto.get("status"), new_auto.get("reason"), new_auto.get("round")):
            changed = True
    return new_phase, changed


def project_status(card: dict, status: dict, now: float) -> bool:
    """Projection only (round/state/stage/evidence/check/progress/phase); never an event."""
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
        "candidateHead": _phase_candidate_head(status),
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

    progress = status.get("progress")
    new_progress = None if not isinstance(progress, dict) else {
        "activity": progress.get("activity"), "step": progress.get("step"),
        "completedCriteria": progress.get("completedCriteria") or [],
        "next": progress.get("next"), "blocker": progress.get("blocker"),
        "updatedAt": progress.get("updatedAt"),
        "reportedBy": progress.get("reportedBy", "pi"), "verified": False,
    }
    if card.get("progress") != new_progress:
        changed = True
    card["progress"] = new_progress

    new_phase, phase_changed = _phase_projection(card, status, now)
    if phase_changed:
        changed = True
    if new_phase is None:
        card.pop("phase", None)
    else:
        card["phase"] = new_phase
    card["updatedAt"] = now
    return changed


def _phase_event_stale(card: dict, event: dict) -> bool:
    """Mechanical staleness for phase-bound events (never an acceptance decision)."""
    if not event.get("phaseId"):
        return False
    phase = card.get("phase") if isinstance(card.get("phase"), dict) else {}
    if event.get("phaseId") != phase.get("phaseId"):
        return True
    if event.get("contractHash") != phase.get("contractHash"):
        return True
    if event.get("round") != (card.get("pi") or {}).get("round"):
        return True
    candidate = (event.get("candidate") or {}).get("head")
    if candidate is not None and candidate != phase.get("candidate"):
        return True
    return False


def _mark_superseded(card: dict, event: dict, now: float) -> None:
    event.update(handled=True, handledAt=now, decision="superseded",
                 note="mechanical supersession: the phase candidate/contract advanced or newer "
                      "facts arrived; this is not an acceptance decision")
    card.setdefault("handled", {})[event["id"]] = {
        "at": now, "decision": "superseded", "reviewedHead": None,
        "round": event.get("round"), "superseded": True}


def _supersede_stale_phase_events(card: dict, now: float) -> int:
    count = 0
    for event in pending_events(card):
        if event.get("phaseId") and _phase_event_stale(card, event):
            _mark_superseded(card, event, now)
            count += 1
    if count:
        _prune_events(card)
        _update_overflow(card, now)
    return count


def _supersede_phase_kinds(card: dict, now: float, kinds, keep_event_id=None,
                           keep_fingerprint=None) -> int:
    count = 0
    for event in pending_events(card):
        if not event.get("phaseId") or event.get("kind") not in kinds:
            continue
        if keep_event_id and event.get("id") == keep_event_id:
            continue
        if keep_fingerprint and event.get("fingerprint") == keep_fingerprint:
            continue
        _mark_superseded(card, event, now)
        count += 1
    if count:
        _prune_events(card)
        _update_overflow(card, now)
    return count


def _phase_blocked_reason(readiness: dict, auto: dict, status: dict) -> str:
    resource = readiness.get("resource") or {}
    resource_status = resource.get("status")
    if resource_status == "breached":
        return "resource_breached"
    if resource_status == "escalated":
        return "resource_unknown"
    auto_status = (auto or {}).get("status")
    if auto_status == "exhausted":
        return "auto_continue_used"
    if auto_status in ("unknown", "claimed"):
        return "auto_continue_unknown"
    if auto_status == "blocked":
        return (auto or {}).get("reason") or "auto_continue_unknown"
    execution = readiness.get("execution") or {}
    execution_status = execution.get("status")
    if execution_status == "failed":
        return "round_execution_failed"
    if execution_status == "unknown":
        return "round_execution_unknown"
    if execution_status == "not_terminal":
        return "round_not_terminal"
    last = (status.get("phase") or {}).get("lastDecision") or {}
    if last.get("reason") and last.get("reason") != "ready":
        # The script's post-round classification (for example a pause or a
        # budget stop) is authoritative for that round's delivery gap. A stale
        # "ready" is not a blocked reason once the current receipts disagree.
        return str(last["reason"])
    scope = readiness.get("scope")
    if isinstance(scope, dict):
        scope = scope.get("status")
    if scope == "violation":
        return "scope_violation"
    if resource_status == "unknown":
        return "resource_unknown"
    items = readiness.get("items")
    if not isinstance(items, list):
        evidence = (status.get("phase") or {}).get("evidence")
        items = evidence.get("items") if isinstance(evidence, dict) else None
    statuses = {item.get("status") for item in items or [] if isinstance(item, dict)}
    if statuses & {"failed", "skipped", "unknown"}:
        return "required_check_failed"
    budget = readiness.get("budget") or {}
    if budget.get("exhausted"):
        return "budget_exhausted"
    if status.get("state") != "completed":
        return f"round_{status.get('state')}"
    if readiness.get("status") == "unknown":
        return "readiness_unknown"
    return "delivery_gap"


def _project_phase_events(card: dict, status: dict, now: float) -> list:
    """Phase-aware classification: local failures stay local; only real decisions escalate."""
    added = []
    state = status.get("state")
    recorded = status.get("recordedState")
    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    latest = receipts.get("latest")
    evidence_dir = status.get("evidence") or {}
    phase = status.get("phase") or {}
    readiness = phase.get("readiness") or {}
    phase_evidence = phase.get("evidence") if isinstance(phase.get("evidence"), dict) else {}
    resource_evidence = phase_evidence.get("resource")
    auto = phase.get("autoContinue") or {}
    phase_id = phase.get("phaseId")
    contract_hash = phase.get("contractHash") or phase.get("contractSha256")
    candidate = _phase_candidate_head(status)
    base_evidence = {"briefRef": evidence_dir.get("brief"), "checksRef": checks.get("dir"),
                     "stateRef": evidence_dir.get("state"), "phaseRef": phase.get("contractRef")}

    def add(kind, fingerprint, summary, question, extra=None):
        evidence = dict(base_evidence)
        if extra:
            evidence.update(extra)
        event = add_event(card, kind, status.get("round"), fingerprint, summary,
                          {"round": status.get("round"), "head": candidate, "state": state},
                          evidence, question, now)
        if event is not None:
            event["phaseId"] = phase_id
            event["contractHash"] = contract_hash
            event["coverage"] = readiness.get("coverage")
            added.append(event)
        return event

    if status.get("timedOut"):
        add("task_timeout",
            f"phase:{phase_id}:contract:{contract_hash}:timeout:{status.get('exitCode')}",
            f"round {status.get('round')} wrapper timed out (exit {status.get('exitCode')})",
            "Decide whether to repair, cancel or extend the deadline; the round state is exact "
            "evidence.",
            {"receiptRef": _receipt_ref(checks, latest)})
    if state in TERMINAL_STATES:
        if readiness.get("status") == "ready" and state == "completed":
            _supersede_phase_kinds(card, now, ("phase_blocked",))
            event = add(
                "review_required",
                f"phase:{phase_id}:contract:{contract_hash}:candidate:{candidate}:"
                f"ready:{_review_episode(card)}",
                f"phase {phase_id} round {status.get('round')} is delivery-ready: "
                f"{readiness.get('coverage', {}).get('covered')}/"
                f"{readiness.get('coverage', {}).get('required')} acceptance items covered",
                "Review the exact candidate and receipts, then decide accept or changes_requested with "
                "the phase identity bound. Exit 0 and readiness are execution evidence only, never "
                "acceptance.",
                {"candidateHead": candidate, "coverage": readiness.get("coverage"),
                 "phaseId": phase_id, "contractHash": contract_hash})
            if event is not None:
                card["phase"] = dict(card.get("phase") or {}, reviewEventId=event["id"])
        elif auto.get("status") == "started":
            # Local same-phase continuation owns the terminal round; no GPT wake-up.
            pass
        else:
            reason = _phase_blocked_reason(readiness, auto, status)
            fingerprint = (f"phase:{phase_id}:contract:{contract_hash}:candidate:{candidate}:"
                           f"blocked:{reason}:{auto.get('status') or 'none'}")
            # A review event from a previous, now-invalid state must not remain
            # current: the same snapshot gate owns both sides. Superseding a
            # pending review advances the durable ready episode so a later
            # recovery on this same round/candidate can publish exactly one new
            # review event instead of being rejected by the old identity.
            superseded_reviews = _supersede_phase_kinds(card, now, ("review_required",))
            if superseded_reviews:
                card["reviewEpisode"] = _review_episode(card) + 1
            _supersede_phase_kinds(card, now, ("phase_blocked",), keep_fingerprint=fingerprint)
            add("phase_blocked", fingerprint,
                f"phase {phase_id} round {status.get('round')} is not delivery-ready ({reason})",
                "Decide whether to continue repairs in the same Pi session (changes_requested), "
                "escalate for main design help, or explicitly accept a partial result. "
                "Missing, failed, skipped or unknown evidence is never ready.",
                {"candidateHead": candidate, "reason": reason,
                 "coverage": readiness.get("coverage"), "gaps": readiness.get("gaps"),
                 "resource": resource_evidence or None,
                 "autoContinue": auto or None})
    ownership = status.get("ownership") or {}
    if state == "unknown" and recorded in ACTIVE_STATES:
        add("ownership_unknown", f"phase:{phase_id}:{recorded}",
            f"recorded active state {recorded} without a live supervisor lease",
            "Inspect ownership and decide cleanup; do not assume progress.",
            {"recordedState": recorded})
    if recorded in TERMINAL_STATES and ownership.get("activeWorker") \
            and not ownership.get("supervisorAlive"):
        add("ownership_lingering", f"phase:{phase_id}:{recorded}",
            "terminal record but a worker lock is still held",
            "Inspect the lingering owned worker before reuse.",
            {"recordedState": recorded})
    return added


def _phase_progress_budget_seconds() -> float:
    raw = os.environ.get("CODEX_PI_PROGRESS_NOTIFY_SECONDS")
    try:
        value = float(raw) if raw else PROGRESS_NOTIFY_INTERVAL_SECONDS
    except (TypeError, ValueError):
        value = PROGRESS_NOTIFY_INTERVAL_SECONDS
    return value if 0.0 <= value <= 86400.0 else PROGRESS_NOTIFY_INTERVAL_SECONDS


def _phase_anomaly_seconds() -> float:
    raw = os.environ.get("CODEX_PI_PROGRESS_ANOMALY_SECONDS")
    try:
        value = float(raw) if raw else PROGRESS_ANOMALY_SECONDS
    except (TypeError, ValueError):
        value = PROGRESS_ANOMALY_SECONDS
    return value if 0.0 <= value <= 86400.0 else PROGRESS_ANOMALY_SECONDS


def _phase_candidate_head(status: dict):
    """One candidate-identity accessor over the normalized candidate block.

    The rule lives in ``pi_task.normalize_candidate``; the board never derives a
    second candidate. A missing block normalizes the raw status with the same
    function, so fail-closed behavior is identical everywhere.
    """
    candidate = status.get("candidate")
    if not isinstance(candidate, dict):
        candidate = normalize_candidate(status)
    if candidate.get("status") != "known":
        return None
    head = candidate.get("head")
    return head if isinstance(head, str) and head else None


def _snapshot_items(status: dict) -> list:
    """Per-item evidence grades from the normalized phase snapshot."""
    phase = status.get("phase")
    if not isinstance(phase, dict):
        return []
    evidence = phase.get("evidence")
    if not isinstance(evidence, dict):
        return []
    return [item for item in evidence.get("items") or [] if isinstance(item, dict)]


def _verified_items(status: dict) -> list:
    return [item for item in _snapshot_items(status) if item.get("status") == "covered"]


def _failed_items(status: dict) -> list:
    return [item for item in _snapshot_items(status) if item.get("status") == "failed"]


def progress_milestones(status: dict) -> list:
    """Stable milestone candidates (key, summary, evidence) for one beat.

    Only verified snapshot items and meaningful check/repair progress with
    evidence refs qualify. Plain "implementing" narration is deliberately not a
    milestone and never queues.
    """
    phase = status.get("phase") or {}
    contract = phase.get("contractSha256")
    candidate = _phase_candidate_head(status)
    if not contract or candidate is None:
        return []
    result = []
    verified = _verified_items(status)
    if verified and not _failed_items(status):
        item = verified[0]
        check_id = item.get("checkId") or item.get("id")
        result.append((
            f"first_result|{contract}|{candidate}|{check_id}",
            f"verified check {check_id!r} passed on the current candidate",
            {"factSource": "verified_receipt", "checkId": check_id,
             "receiptRef": item.get("receiptRef"), "logVerified": True,
             "evidenceLevel": item.get("evidenceLevel")}))
    progress = status.get("progress") or {}
    if isinstance(progress, dict) and progress.get("round") == status.get("round") \
            and progress.get("activity") in ("checking", "repairing") \
            and (progress.get("completedCriteria") or progress.get("evidenceRefs")):
        result.append((
            f"check_{progress.get('activity')}|{contract}|{candidate}",
            f"pi reports {progress.get('activity')} progress with evidence references",
            {"factSource": "pi_self_report_unverified",
             "activity": progress.get("activity"),
             "completedCriteria": progress.get("completedCriteria") or [],
             "evidenceRefs": progress.get("evidenceRefs") or [],
             "step": progress.get("step"), "next": progress.get("next")}))
    return result


def _repair_progress_after(status: dict, since: float) -> bool:
    progress = status.get("progress") or {}
    updated = progress.get("updatedAt")
    if not isinstance(updated, (int, float)) or isinstance(updated, bool) or updated <= since:
        return False
    return (progress.get("activity") == "repairing"
            and bool(progress.get("completedCriteria") or progress.get("evidenceRefs")))


def _notify_ledger(card: dict, phase_id: str, contract: str, now: float) -> dict:
    store = card.get("notify")
    if not isinstance(store, dict):
        store = {"schemaVersion": 1, "phases": {}}
        card["notify"] = store
    phases = store.setdefault("phases", {})
    entry = phases.get(phase_id)
    if not isinstance(entry, dict):
        entry = {}
    entry.update({"phaseId": phase_id, "contractSha256": contract,
                  "count": int(entry.get("count") or 0),
                  "milestones": entry.get("milestones") if isinstance(entry.get("milestones"), dict) else {},
                  "abnormal": entry.get("abnormal") if isinstance(entry.get("abnormal"), dict) else {},
                  "updatedAt": now})
    phases[phase_id] = entry
    return entry


def _anomaly_milestone(entry: dict, status: dict, now: float, anomaly_seconds: float):
    """First sustained, unresolved applicable failure -> one bounded echo."""
    changed = False
    result = None
    contract = entry.get("contractSha256")
    candidate = _phase_candidate_head(status)
    for item in _failed_items(status):
        check_id = item.get("checkId") or item.get("id")
        key = f"abnormal|{contract}|{candidate}|{check_id}"
        record = entry["abnormal"].get(key)
        if not isinstance(record, dict):
            record = {"firstSeenAt": now, "notifiedAt": None}
            entry["abnormal"][key] = record
            changed = True
        if record.get("notifiedAt"):
            continue
        first = record.get("firstSeenAt")
        if not isinstance(first, (int, float)) or isinstance(first, bool) \
                or now - first < anomaly_seconds:
            continue
        if _repair_progress_after(status, first):
            continue
        if result is None:
            result = (key,
                      f"check {check_id!r} has an unresolved failure observed for "
                      f"{int(now - first)}s; pi is expected to be repairing",
                      {"factSource": "observed_receipt", "checkId": check_id,
                       "reason": "sustained_anomaly",
                       "receiptRef": item.get("receiptRef"), "logRef": item.get("logRef"),
                       "itemReason": item.get("reason"),
                       "evidenceLevel": item.get("evidenceLevel")})
    if len(entry["abnormal"]) > MAX_NOTIFY_ABNORMAL:
        ordered = sorted(entry["abnormal"].items(), key=lambda kv: (kv[1] or {}).get("firstSeenAt") or 0)
        entry["abnormal"] = dict(ordered[-MAX_NOTIFY_ABNORMAL:])
    return result, changed


def maybe_publish_progress(card: dict, status: dict, now=None, interval=None,
                           anomaly_seconds=None,
                           max_notifications: int = MAX_PROGRESS_NOTIFICATIONS) -> dict:
    """Publish at most two bounded phase progress echoes; never a decision event.

    Returns ``{"changed": bool, "created": event|None, "merged": event|None}``.
    The quota and the last-notification time live on the persisted card and are
    keyed by phaseId, so rounds, supervisor restarts and repeated refreshes do
    not reset them. Pause, route pause, offline transport and a candidate
    without phase identity fail closed (nothing is queued).
    """
    now = time.time() if now is None else now
    interval = _phase_progress_budget_seconds() if interval is None else float(interval)
    anomaly_seconds = _phase_anomaly_seconds() if anomaly_seconds is None else float(anomaly_seconds)
    phase = status.get("phase")
    if not isinstance(phase, dict) or not phase.get("phaseId"):
        return {"changed": False, "created": None, "merged": None, "reason": "no phase"}
    if card.get("transport") != TRANSPORT_CLI_QUEUE:
        return {"changed": False, "created": None, "merged": None,
                "reason": "transport is not cli-queue"}
    if status.get("state") not in ACTIVE_STATES:
        # Terminal delivery is owned by the review/blocked event path, which is
        # never suppressed by the progress quota; no duplicate progress echo.
        return {"changed": False, "created": None, "merged": None,
                "reason": "no active round"}
    if card.get("paused"):
        return {"changed": False, "created": None, "merged": None, "reason": "paused"}
    thread = card.get("ownerThread")
    if isinstance(thread, str):
        route_is_paused, problem = route_paused(thread)
        if problem is not None or route_is_paused:
            return {"changed": False, "created": None, "merged": None,
                    "reason": f"route pause {problem or 'active'}"}
    contract = phase.get("contractSha256")
    candidate = _phase_candidate_head(status)
    if not contract or candidate is None:
        return {"changed": False, "created": None, "merged": None,
                "reason": "phase contract or candidate unknown"}
    entry = _notify_ledger(card, phase["phaseId"], contract, now)
    anomaly, changed = _anomaly_milestone(entry, status, now, anomaly_seconds)
    milestones = progress_milestones(status)
    chosen = next(((key, summary, evidence) for key, summary, evidence in milestones
                   if key not in entry["milestones"]), None)
    if chosen is None and anomaly is not None and anomaly[0] not in entry["milestones"]:
        chosen = anomaly
    if chosen is None:
        return {"changed": changed, "created": None, "merged": None}
    key, summary, evidence = chosen
    evidence_all = {"briefRef": (status.get("evidence") or {}).get("brief"),
                    "checksRef": (status.get("checks") or {}).get("dir"),
                    "stateRef": (status.get("evidence") or {}).get("state"),
                    **evidence}
    if int(entry.get("count") or 0) >= max_notifications:
        entry["milestones"][key] = {"at": now, "suppressed": "quota"}
        entry["updatedAt"] = now
        return {"changed": True, "created": None, "merged": None,
                "reason": "notification quota exhausted"}
    fingerprint = (f"phase:{phase['phaseId']}:contract:{contract}:candidate:{candidate}:"
                   f"progress:{key}")
    last = entry.get("lastAt")
    if isinstance(last, (int, float)) and not isinstance(last, bool) and now - last < interval:
        merged = None
        pending = [event for event in pending_events(card)
                   if event.get("kind") == PROGRESS_EVENT_KIND
                   and event.get("phaseId") == phase["phaseId"]]
        if pending:
            merged = pending[-1]
            merged["summary"] = _text(summary, MAX_SUMMARY)
            merged["fingerprint"] = _text(fingerprint, 200)
            merged["evidence"] = evidence_all
            merged["candidate"] = {"round": status.get("round"), "head": candidate,
                                   "state": status.get("state")}
            merged["updatedAt"] = now
        entry["milestones"][key] = {"at": now, "merged": bool(merged),
                                    "suppressedByInterval": True}
        entry["updatedAt"] = now
        return {"changed": True, "created": None, "merged": merged,
                "reason": "merged within the notification interval"}
    _supersede_phase_kinds(card, now, (PROGRESS_EVENT_KIND,), keep_fingerprint=fingerprint)
    event = add_event(card, PROGRESS_EVENT_KIND, status.get("round"), fingerprint, summary,
                      {"round": status.get("round"), "head": candidate,
                       "state": status.get("state")},
                      evidence_all,
                      "No decision is required; this is a bounded progress echo. Re-read the "
                      "short board for the latest facts and continue.", now)
    if event is None:
        entry["milestones"][key] = {"at": now, "deduplicated": True}
        entry["updatedAt"] = now
        return {"changed": True, "created": None, "merged": None, "reason": "identity dedup"}
    event["phaseId"] = phase["phaseId"]
    event["contractHash"] = contract
    event["factSource"] = evidence.get("factSource")
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["lastAt"] = now
    entry["milestones"][key] = {"at": now, "eventId": event["id"]}
    if anomaly is not None and anomaly[0] == key:
        entry["abnormal"][key]["notifiedAt"] = now
    if len(entry["milestones"]) > MAX_NOTIFY_MILESTONES:
        ordered = sorted(entry["milestones"].items(), key=lambda kv: (kv[1] or {}).get("at") or 0)
        entry["milestones"] = dict(ordered[-MAX_NOTIFY_MILESTONES:])
    entry["updatedAt"] = now
    return {"changed": True, "created": event, "merged": None}


def _notify_view(card: dict):
    store = card.get("notify")
    if not isinstance(store, dict):
        return None
    phases = store.get("phases") or {}
    return {phase_id: {"count": entry.get("count"), "lastAt": entry.get("lastAt"),
                       "milestones": len(entry.get("milestones") or {}),
                       "abnormal": len(entry.get("abnormal") or {})}
            for phase_id, entry in list(phases.items())[:5] if isinstance(entry, dict)}


def project_events(card: dict, status: dict, now: float) -> list:
    """Actionable facts only; phase contracts suppress local/self-repairable check events."""
    phase = status.get("phase")
    if isinstance(phase, dict) and phase.get("phaseId"):
        return _project_phase_events(card, status, now)
    return _project_legacy_events(card, status, now)


def _project_legacy_events(card: dict, status: dict, now: float) -> list:
    """Legacy (no phase contract) actionable facts, unchanged from 0.4."""
    added = []
    state = status.get("state")
    recorded = status.get("recordedState")
    checks = status.get("checks") or {}
    receipts = checks.get("receipts") or {}
    latest = receipts.get("latest")
    guard = checks.get("resourceGuard") or {}
    evidence_dir = status.get("evidence") or {}
    head = _phase_candidate_head(status)
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
        if isinstance(card.get("phase"), dict) and _supersede_stale_phase_events(card, now):
            changed = True
        added = project_events(card, status, now)
        progress = maybe_publish_progress(card, status, now)
        if progress.get("created") is not None:
            added.append(progress["created"])
        if progress.get("changed"):
            changed = True
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

def _read_bounded_stream(stream, limit: int) -> bytes:
    """Read at most ``limit`` bytes from a child pipe, draining the rest."""
    data = bytearray()
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if len(data) < limit:
                data.extend(chunk[: limit - len(data)])
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
    return bytes(data)


def _packet_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _run_queue_cli(argv, timeout: float) -> dict:
    """Run the exact queue argv with shell=False, bounded live capture and cleanup.

    Only a proven not-started child (Popen OSError) is retryable. Any spawned
    child that times out, is interrupted, dies or exits nonzero is uncertain:
    it may already have enqueued the message and is never auto-resent.
    """
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return {"status": "failed", "notStarted": True, "exitCode": None, "timedOut": False,
                "error": f"queue command could not start: {exc}"}
    results = {}

    def reader(stream, key):
        results[key] = _read_bounded_stream(stream, MAX_CLI_OUTPUT_BYTES)

    readers = [threading.Thread(target=reader, args=(child.stdout, "stdout"), daemon=True),
               threading.Thread(target=reader, args=(child.stderr, "stderr"), daemon=True)]
    for thread in readers:
        thread.start()
    timed_out = False
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate(child)
    except KeyboardInterrupt:
        timed_out = True
        terminate(child)
    for thread in readers:
        thread.join(timeout=2.0)
    output = (results.get("stdout", b"") + results.get("stderr", b""))[:MAX_CLI_OUTPUT_BYTES]
    result = {"exitCode": child.returncode, "timedOut": timed_out,
              "outputSha256": hashlib.sha256(output).hexdigest(),
              "outputExcerpt": output.decode("utf-8", errors="replace")[:1000],
              "argv0": argv[0]}
    if timed_out:
        result.update(status="uncertain",
                      error="queue command timed out or was interrupted after being spawned; "
                            "delivery is uncertain and requires explicit rearm")
    elif child.returncode == 0:
        result["status"] = "queued"
    else:
        # A spawned process that exits nonzero may still have enqueued before
        # failing; it is ambiguous, never a retryable not-sent result.
        result.update(status="uncertain",
                      error=f"queue command exited {child.returncode} after being spawned; "
                            "delivery is uncertain and requires explicit rearm")
    return result


def _resolve_codex_bin(value) -> str:
    raw = str(value)
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or os.sep in raw:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError(f"codex executable is not an executable file: {candidate}")
        return str(candidate.resolve())
    found = shutil.which(raw)
    if not found:
        raise ValueError(f"codex executable {raw!r} was not found on PATH; pass --codex-bin")
    return str(Path(found).resolve())


def _show_hint(card: dict) -> str:
    return (f'python3 {shlex.quote(str(Path(__file__).resolve()))} show --repo '
            f'{shlex.quote(str(card.get("repo")))} --task {shlex.quote(str(card.get("taskId")))}')


def _drain_reference(card: dict) -> str:
    return "drain the board in this same turn: " + _show_hint(card)


def build_packet(card: dict, events, limit: int = MAX_PACKET_EVENTS):
    """Build a bounded runtime-handoff packet; return ``(text, included_events)``.

    The limit is measured in UTF-8 bytes. Room for the mandatory board-drain
    instruction is reserved *before* filling event blocks, so a near-full packet
    can never silently omit the instruction to handle remaining events. An
    event too large for the normal layout still produces a bounded fallback
    carrying its exact id and a board evidence reference.
    """
    header = [
        "Codex-Pi runtime handoff (transport=cli-queue; not a new user goal or instruction override).",
        "Preserve the latest user pause/instructions. Exit 0 is queue delivery only, never acceptance.",
    ]
    title = _text(card.get("title") or card.get("taskId") or "", 120)
    goal = _text(card.get("goal") or "", 160)
    lines = [f"task={card.get('taskId')} title={title}"]
    if goal:
        lines.append(f"goal={goal}")
    overflow = card.get("overflow")
    if isinstance(overflow, dict) and overflow.get("active"):
        lines.append(f"overflow={overflow.get('pendingCount')} pending events; drain them in this "
                     f"same turn: {_show_hint(card)}")
    drain_reference = _drain_reference(card)
    # Reserve the longest drain prefix ("+NNN pending event(s); ") plus one byte
    # before filling any event block.
    reserve = _packet_bytes(drain_reference) + 24
    event_budget = max(0, MAX_PACKET_CHARS - reserve)
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
        if event.get("phaseId"):
            block.append(f"phase={event.get('phaseId')} contract={event.get('contractHash')}")
        # Every actionable packet needs the allocation boundary at the point
        # of review. A takeover event carries its immutable decision snapshot;
        # ordinary review and fault events use the current board projection.
        event_policy = (event.get("evidence") or {}).get("reviewPolicy")
        if not isinstance(event_policy, dict):
            event_policy = review_policy(card)
        if isinstance(event_policy, dict):
            block.append("reviewPolicy=" + ",".join(
                f"{label}:{event_policy.get(key)}" for label, key in
                (("limit", "limit"), ("failed", "failedDeliveries"),
                 ("owner", "implementationOwner"), ("reason", "reason"))
                if event_policy.get(key) is not None))
        coverage = event.get("coverage")
        if isinstance(coverage, dict):
            block.append("coverage=" + ",".join(f"{key}:{coverage.get(key)}" for key in
                                                  ("required", "covered", "failed", "missing",
                                                   "unknown", "skipped") if coverage.get(key) is not None))
        evidence = event.get("evidence") or {}
        for label, key in (("brief", "briefRef"), ("checks", "checksRef"),
                           ("receipt", "receiptRef"), ("state", "stateRef")):
            if evidence.get(key):
                block.append(f"{label}={evidence.get(key)}")
        block.append("decide=" + _decide_hint(card.get("repo"), card.get("taskId"), event))
        candidate = "\n".join(header + lines + block)
        if _packet_bytes(candidate) > event_budget:
            break
        lines.extend(block)
        included.append(event)
    remaining = len(events) - len(included)
    if not included and events:
        event = events[0]
        board_ref = str(Path(str(card.get("commonDir") or card.get("repo") or ""))
                        / BOARD_DIR / BOARD_FILE)
        pieces = [
            f"event={event.get('id')} kind={event.get('kind')} round={event.get('round')}",
            f"board_ref={board_ref}",
        ]
        if remaining > 1:
            pieces.append(f"+{remaining - 1} pending event(s); {drain_reference}")
        pieces.extend([
            "oversized packet fallback; read this board evidence in the same turn: "
            + _show_hint(card),
            f"task={card.get('taskId')} title={title}",
            f"summary={_text(event.get('summary'), 200)}",
        ])
        text = "\n".join(pieces)
        if _packet_bytes(text) > MAX_PACKET_CHARS:
            # Even under a tiny cap keep the exact event id and a board ref.
            minimal = f"event={event.get('id')} board_ref={board_ref}"
            encoded = minimal.encode("utf-8")
            text = encoded[:MAX_PACKET_CHARS].decode("utf-8", "ignore") \
                if len(encoded) > MAX_PACKET_CHARS else minimal
        return text, [event]
    text = "\n".join(header + lines)
    if remaining > 0:
        # Guaranteed to fit: event blocks were filled below the reserved budget.
        text = text + "\n" + f"+{remaining} pending event(s); {drain_reference}"
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
        if event.get("phaseId") and _phase_event_stale(card, event):
            # Mechanical staleness: never dispatch evidence for an older phase/candidate.
            continue
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
    try:
        argv[0] = _resolve_codex_bin(argv[0])
    except ValueError as exc:
        return {"ok": False, "dispatched": False, "status": "invalid-binary", "error": str(exc)}
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
            "phaseId": event.get("phaseId"), "contractHash": event.get("contractHash"),
            "coverage": event.get("coverage"),
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
        "phase": card.get("phase"), "progress": card.get("progress"),
        "reviewPolicy": review_policy(card),
        "evidence": card.get("evidence"), "check": card.get("check"),
        "progressNotify": _notify_view(card),
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
    phase = f" --phase {event.get('phaseId')} --contract-hash {event.get('contractHash')}" \
        if event.get("phaseId") else ""
    if event.get("kind") in REVIEW_KINDS and isinstance(head, str) and FULL_OID_RE.fullmatch(head):
        decision = "accept|reject|changes_requested"
        suffix = f" --reviewed-head {head}{phase}"
    else:
        decision = "resolve|reject|changes_requested"
        suffix = phase
    return (f'python3 {shlex.quote(str(Path(__file__).resolve()))} decide '
            f'--repo {shlex.quote(str(repo))} --task {shlex.quote(str(task_id))} '
            f'--event-id {event.get("id")} --decision {decision}{suffix}')


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------

def _task_review_pin(frozen: dict) -> dict | None:
    """The dispatch-time quality-failure pin frozen in task.json, if valid."""
    raw = frozen.get("reviewPolicy") if isinstance(frozen, dict) else None
    if not isinstance(raw, dict):
        return None
    limit = normalize_review_limit(raw.get("qualityFailureLimit"))
    if limit is None:
        return None
    pinned_at = raw.get("pinnedAt")
    if not isinstance(pinned_at, (int, float)) or isinstance(pinned_at, bool):
        pinned_at = frozen.get("createdAt")
    return {"schemaVersion": 1, "qualityFailureLimit": limit, "pinnedAt": pinned_at,
            "pinnedBy": _text(raw.get("pinnedBy") or "start", 60)}


def _adopt_task_review_pin(card: dict, pin: dict | None) -> None:
    """Adopt the creation-time pin once, before any delivery failure exists.

    A card that already carries a pin is never overwritten, so re-registration,
    contract edits or later config changes cannot raise or reset the limit.
    Legacy cards without a pin stay on the legacy default.
    """
    if not isinstance(pin, dict) or isinstance(card.get("reviewPolicyPin"), dict):
        return
    policy = review_policy(card)
    if policy["failedDeliveries"] == 0 and not policy["takeoverRequired"]:
        card["reviewPolicyPin"] = dict(pin)


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
    review_pin = _task_review_pin(frozen)
    transport = _validate_transport(transport)
    owner_thread = _validate_thread(thread, required=(transport == TRANSPORT_CLI_QUEUE))
    resolved_bin = None
    if codex_bin:
        resolved_bin = _resolve_codex_bin(codex_bin)
    elif transport == TRANSPORT_CLI_QUEUE:
        resolved_bin = _resolve_codex_bin(DEFAULT_CODEX_BIN)
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
                             resolved_bin, now, review_pin=review_pin)
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
            _adopt_task_review_pin(card, review_pin)
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


def decide(repo, task, event_id, decision, reviewed_head=None, note=None, phase=None,
           contract_hash=None, now=None, failure_kind=None) -> dict:
    now = time.time() if now is None else now
    event_id = str(event_id)
    if not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("event id must be a 64-character lowercase hex digest")
    mapped = DECISIONS.get(decision)
    if mapped is None:
        raise ValueError("decision must be accept, reject, changes_requested or resolve")
    if failure_kind is not None and (failure_kind not in FAILURE_KINDS or mapped not in ("rejected", "changes_requested")):
        raise ValueError("--failure-kind applies only to reject/changes_requested: quality or external")
    if failure_kind == "external" and not (isinstance(note, str) and note.strip()):
        raise ValueError("external blockers require --note with the missing input/authority and unlock condition")
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
        phase_info = card.get("phase") if isinstance(card.get("phase"), dict) else {}
        event = find_event(card, event_id)
        history = (card.get("handled") or {}).get(event_id) if event is None else None
        if event is None and not isinstance(history, dict):
            raise ValueError(f"unknown event {event_id} for task {task_id!r}")
        effective_kind = failure_kind or ("quality" if mapped in ("rejected", "changes_requested") else None)
        prior_decision = event if event is not None else history
        if prior_decision.get("handled", event is None) and failure_kind is not None and prior_decision.get("failureKind", "quality") != failure_kind:
            raise ValueError("conflicting replay: failure classification is immutable")
        if event is None:
            if history.get("phaseId") and mapped == "accepted" \
                    and (phase != history.get("phaseId")
                         or contract_hash != history.get("contractHash")):
                raise ValueError("accept replay for a phase event must bind --phase and "
                                 "--contract-hash to the exact contract revision")
            same = history.get("decision") == mapped and (
                mapped != "accepted" or history.get("reviewedHead") == reviewed_head)
            if same:
                return {"ok": True, "idempotent": True, "taskId": task_id, "eventId": event_id,
                        "decision": mapped, "revision": board.get("revision")}
            raise ValueError(f"event {event_id} was already decided as "
                             f"{history.get('decision')!r}; refusing to overwrite or replay it "
                             "with a different decision/reviewed head")
        kind = event.get("kind")
        event_phase = event.get("phaseId")
        if event.get("handled"):
            if event_phase and mapped == "accepted" and (phase != event_phase
                                                         or contract_hash != event.get("contractHash")):
                raise ValueError("accept replay for a phase event must bind --phase and "
                                 "--contract-hash to the exact contract revision")
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
        if event_phase or event.get("contractHash"):
            # Phase events are only meaningful for the exact installed contract and
            # current candidate/round. Stale evidence can never be accepted or sent
            # back as if it were the current delivery.
            if mapped == "accepted" and (phase != event_phase
                                         or contract_hash != event.get("contractHash")):
                raise ValueError("accept for a phase event must bind --phase and --contract-hash "
                                 "to the exact contract revision")
            if event_phase != phase_info.get("phaseId") \
                    or event.get("contractHash") != phase_info.get("contractHash"):
                raise ValueError(f"stale phase event: the current phase is "
                                 f"{phase_info.get('phaseId')!r}/"
                                 f"{phase_info.get('contractHash')!r}; refresh the board and use "
                                 "the current review event")
            card_round = (card.get("pi") or {}).get("round")
            if event.get("round") != card_round:
                raise ValueError(f"stale phase event: round {event.get('round')} is not the current "
                                 f"round {card_round}")
            if (event.get("candidate") or {}).get("head") != phase_info.get("candidate"):
                raise ValueError("stale phase event: its candidate is no longer the current phase "
                                 "candidate")
        if mapped == "accepted":
            if kind not in REVIEW_KINDS:
                raise ValueError(f"event kind {kind!r} is a fault/observation; "
                                 "use --decision resolve, not accept")
            if event_phase:
                # Re-confirm the live task state instead of trusting a possibly
                # stale board projection: round, contract, candidate, readiness,
                # worktree HEAD and writer-free must all still agree.
                live_root = card.get("repo") or card.get("worktree") or _root
                try:
                    live = build_status(str(live_root), task_id)
                except Exception as exc:  # noqa: BLE001 - fail closed on unknown live state
                    raise ValueError(f"cannot re-confirm the live phase state: "
                                     f"{type(exc).__name__}: {exc}") from None
                live_phase = live.get("phase") if isinstance(live.get("phase"), dict) else {}
                live_candidate = live.get("candidate") \
                    if isinstance(live.get("candidate"), dict) else {}
                live_readiness = live_phase.get("readiness") \
                    if isinstance(live_phase.get("readiness"), dict) else {}
                live_contract = live_phase.get("contractSha256")
                live_head = live_candidate.get("head") \
                    if live_candidate.get("status") == "known" else None
                event_head = (event.get("candidate") or {}).get("head")
                problems = []
                if live.get("round") != event.get("round"):
                    problems.append(f"live round {live.get('round')} != event round "
                                    f"{event.get('round')}")
                if live_contract != event.get("contractHash"):
                    problems.append("the installed contract revision changed")
                if live_candidate.get("status") != "known":
                    problems.append(f"the live candidate is unknown "
                                    f"({live_candidate.get('reason')})")
                elif live_head != event_head:
                    problems.append("the live candidate no longer matches the event candidate")
                if phase_info.get("candidate") not in (None, live_head):
                    problems.append("the stored board candidate is stale")
                if phase_info.get("contractHash") != live_contract:
                    problems.append("the stored board contract revision is stale")
                if live_readiness.get("status") != "ready":
                    problems.append(f"live readiness is {live_readiness.get('status')!r}: "
                                    f"{live_readiness.get('reason')}")
                worktree_probe = card.get("worktree") or _root
                head_now, head_problem = probe_worktree_head(Path(str(worktree_probe)))
                if head_problem is not None:
                    problems.append(f"worktree HEAD is unknown: {head_problem}")
                elif head_now != event_head:
                    problems.append("the worktree HEAD no longer matches the reviewed candidate")
                evidence_dir = task_dir_for(_common, task_id)
                if lock_is_held(evidence_dir / ".task.lock") \
                        or lock_is_held(evidence_dir / ".supervisor.lock"):
                    problems.append("a worker or supervisor lock is still held")
                if problems:
                    raise ValueError("refusing a stale phase accept: " + "; ".join(problems))
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
        event.update(handled=True, handledAt=now, decision=mapped, failureKind=effective_kind,
                     reviewedHead=str(reviewed_head) if reviewed_head else None,
                     note=_text(note, MAX_NOTE) if note else None)
        card.setdefault("handled", {})[event_id] = {
            "at": now, "decision": mapped,
            "reviewedHead": str(reviewed_head) if reviewed_head else None,
            "round": event.get("round"), "phaseId": event.get("phaseId"),
            "contractHash": event.get("contractHash"), "eventKind": kind,
            "failureKind": effective_kind}
        if event_phase:
            phase_card = card.setdefault("phase", {})
            phase_card["decidedEventId"] = event_id
            phase_card["lastDecision"] = mapped
            phase_card["lastDecisionAt"] = now
            if mapped == "accepted":
                phase_card["status"] = "accepted"
                phase_card["acceptedHead"] = str(reviewed_head) if reviewed_head else None
                phase_card["acceptedAt"] = now
                phase_card["reviewEventId"] = event_id
            elif mapped == "changes_requested":
                phase_card["status"] = "changes_requested"
            elif mapped == "rejected":
                phase_card["status"] = "rejected"
            phase_card["updatedAt"] = now
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
                             "reviewedHead": str(reviewed_head) if reviewed_head else None,
                             "phaseId": event.get("phaseId"),
                             "contractHash": event.get("contractHash")}
            codex["history"] = (codex.get("history") or [])[-19:] + [history_entry]
        policy = review_policy(card)
        if policy["takeoverRequired"]:
            existing_latch = codex.get("takeover") \
                if isinstance(codex.get("takeover"), dict) else None
            if isinstance(existing_latch, dict) and existing_latch.get("required"):
                existing_latch.update({"required": True, "scope": policy["scope"],
                                       "outcome": policy.get("outcome"),
                                       "limit": policy["limit"],
                                       "failedReports": policy["failedReports"]})
            else:
                # First reach of the pinned limit: keep the exact time so a later
                # acceptance can be proven to have happened after the latch.
                codex["takeover"] = {"required": True, "at": now, "scope": policy["scope"],
                                     "outcome": policy.get("outcome"),
                                     "limit": policy["limit"],
                                     "failedReports": policy["failedReports"]}
            takeover = add_event(card, "codex_takeover_required", card_round,
                                 "quality-failure-limit-reached",
                                 f"Codex must take over implementation after {policy['limit']} "
                                 f"failed reviewed "
                                 f"deliver{'y' if policy['limit'] == 1 else 'ies'}",
                                 event.get("candidate") or {}, {"reviewPolicy": policy},
                                 policy["instruction"], now)
            if takeover is not None:
                takeover["phaseId"] = event_phase
                takeover["contractHash"] = event.get("contractHash")
        board["revision"] = int(board.get("revision") or 0) + 1
        board["updatedAt"] = now
        _write_board(board_file, board)
        return {"ok": True, "idempotent": False, "taskId": task_id, "eventId": event_id,
                "decision": mapped, "reviewedHead": reviewed_head, "aggregate": is_current,
                "reviewPolicy": policy,
                "phaseId": event_phase, "contractHash": event.get("contractHash"),
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
                  reviewed_head=args.reviewed_head, note=args.note,
                  phase=args.phase, contract_hash=args.contract_hash,
                  failure_kind=args.failure_kind)


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
    decide.add_argument("--phase", help="required with accept for a phase-bound event; the exact "
                                          "phase id")
    decide.add_argument("--contract-hash", help="required with accept for a phase-bound event; the "
                                                 "exact contract revision hash")
    decide.add_argument("--failure-kind", choices=FAILURE_KINDS,
                        help="negative delivery decision: quality (default) counts toward the "
                             "pinned task limit; external requires an explanatory note and "
                             "does not count")
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
