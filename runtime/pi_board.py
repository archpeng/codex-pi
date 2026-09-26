#!/usr/bin/env python3
"""Opt-in structured board for Codex-Pi coordination.

A repository stores one bounded snapshot at ``<git-common-dir>/codex-pi/board.json``.
The board is a projection over existing immutable task evidence (task.json,
round.state.json, round.checks receipts); it never replaces PLAN/briefs/receipts
and it never proves acceptance.

Roles are explicit commands, not a permission framework:
  * ``register``  -- main: bind an existing task to its owning Codex session and
    write the opt-in automation gate record;
  * ``refresh``   -- Pi/runner (and hook recovery): project real bounded status
    into the card and publish deduplicated attention events;
  * ``decide``    -- main only: handle one exact event and record an explicit
    accepted/rejected/changes_requested decision bound to its candidate;
  * ``pause``/``resume`` -- main: explicit persisted control state;
  * ``show``/``packet`` -- bounded compact reads for the selected owner;
  * ``gate``      -- pre-model UserPromptSubmit filter for one exact registered
    automatic tick.

Reading or delivering an event does not handle or accept it. ``accepted`` is
only ever written by an explicit ``decide --decision accept``; a zero exit code
is never acceptance. Corrupt, missing or oversized state is reported as unknown,
never as "unchanged".

This module never invokes the Codex CLI, starts a model or spawns a watcher.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import sys
import time
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from pi_task import (ACTIVE_STATES, TERMINAL_STATES, TASK_RE, LockHeld, atomic,  # noqa: E402
                     build_status, canonical_root, git_common_dir, lock_fd, read_json,
                     require_allowed_model, require_task_arg, task_dir_for)

SCHEMA_VERSION = 1
BOARD_DIR = "codex-pi"
BOARD_FILE = "board.json"
BOARD_LOCK = "board.lock"
MONITOR_LOG = "board-monitor.log"
MAX_BOARD_BYTES = 262_144
MAX_MONITOR_LOG_BYTES = 65_536
MAX_EVENTS_PER_CARD = 50
MAX_SUMMARY = 300
MAX_NOTE = 300
REFRESH_INTERVAL_SECONDS = 15.0
EVENT_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
HEAD_RE = re.compile(r"[0-9a-fA-F]{7,64}\Z")
SESSION_RE = re.compile(r"[^\x00-\x1f\x7f]{1,200}\Z")
AUTOMATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}\Z")
DECISIONS = {"accept": "accepted", "reject": "rejected", "changes_requested": "changes_requested"}
GATE_DIR = "gates"
GATE_SCHEMA_VERSION = 1
MAX_GATE_BYTES = 32_768
AUDIT_FILE = "gate-audit.jsonl"
MAX_AUDIT_BYTES = 262_144
DELIVERED_LIMIT = 100
STOP_MODES = ("continue", "block")
INSTRUCTIONS_TEMPLATE = "codex-pi-board-tick {nonce}"
MAX_PACKET_CHARS = 3500

# Only this exact envelope shape is eligible for the local pre-model gate. Any
# other text (human text containing "heartbeat", quoted markup, extra
# instructions, other ids or nonces) passes through unchanged.
HEARTBEAT_RE = re.compile(
    r"\A<heartbeat>\n"
    r"  <automation_id>([A-Za-z0-9][A-Za-z0-9._:-]{0,99})</automation_id>\n"
    r"  <current_time_iso>([^<>\n]{1,64})</current_time_iso>\n"
    r"  <instructions>\n([^\n]{1,200})\n"
    r"  </instructions>\n</heartbeat>\n?\Z")


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
            if size > MAX_AUDIT_BYTES:
                stream.seek(size - MAX_AUDIT_BYTES)
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


def session_key(session: str) -> str:
    return hashlib.sha256(str(session).encode("utf-8")).hexdigest()


def _validate_session(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session id must be a non-empty string")
    value = value.strip()
    if not SESSION_RE.fullmatch(value):
        raise ValueError("session id must be at most 200 printable characters")
    return value


def _validate_automation(value) -> str:
    if not isinstance(value, str) or not AUTOMATION_ID_RE.fullmatch(value):
        raise ValueError("automation id must be 1-100 chars of letters, digits, '.', '_', ':', '-'")
    return value


def board_file_for_common(common) -> Path:
    return Path(common) / BOARD_DIR / BOARD_FILE


def board_file_for_repo(repo):
    root = canonical_root(Path(repo))
    common = git_common_dir(root)
    return root, common, board_file_for_common(common)


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


# ---------------------------------------------------------------------------
# card / event model
# ---------------------------------------------------------------------------

def _default_codex() -> dict:
    return {"review": "pending", "reviewedHead": None, "decidedAt": None, "lastEventId": None}


def _new_card(task_id, codex_task_id, owner_session, title, goal, brief_ref, plan_ref,
              repo, common, worktree, now) -> dict:
    return {
        "taskId": task_id, "codexTaskId": codex_task_id, "ownerSession": owner_session,
        "title": _text(title or task_id, 200), "goal": _text(goal or "", 600),
        "planRef": plan_ref, "briefRef": brief_ref,
        "repo": str(repo), "commonDir": str(common), "worktree": worktree,
        "createdAt": now, "updatedAt": now,
        "paused": False, "pausedAt": None, "pauseNote": None,
        "pi": {"round": None, "state": None, "stage": None, "updatedAt": None},
        "evidence": {}, "check": None, "events": [], "nextSeq": 1,
        "attention": None, "codex": _default_codex(), "monitorError": None,
    }


def event_identity(task_id: str, round_number, kind: str, fingerprint: str) -> str:
    raw = "\x00".join((task_id, str(round_number), kind, str(fingerprint)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def find_event(card: dict, event_id: str):
    for event in card.get("events", []):
        if isinstance(event, dict) and event.get("id") == event_id:
            return event
    return None


def _prune_events(card: dict) -> None:
    events = [event for event in card.get("events", []) if isinstance(event, dict)]
    if len(events) <= MAX_EVENTS_PER_CARD:
        card["events"] = events
        return
    unhandled = [event for event in events if not event.get("handled")]
    handled = [event for event in events if event.get("handled")]
    room = max(0, MAX_EVENTS_PER_CARD - len(unhandled))
    kept = unhandled[-MAX_EVENTS_PER_CARD:] + (handled[-room:] if room else [])
    kept.sort(key=lambda event: event.get("seq") or 0)
    card["events"] = kept


def add_event(card: dict, kind: str, round_number, fingerprint: str, summary: str,
              candidate: dict, evidence: dict, question: str, now: float):
    """Idempotent publication: one immutable identity per task/round/kind/fingerprint."""
    identity = event_identity(card["taskId"], round_number, kind, fingerprint)
    if find_event(card, identity) is not None:
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
    codex["review"] = "pending"  # new unhandled work; accepted is never inferred
    _prune_events(card)
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
    """Actionable facts only: terminal, verified timeout/resource, unknown ownership.

    A plain failed test is deliberately not an event: its evidence stays in the
    check receipts so Pi can repair within its brief.
    """
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
        add("resource_breach",
            f"{name}:{breach.get('observedBytes')}:{breach.get('maxBytes')}",
            f"resource budget breached: {breach.get('observedBytes')} > {breach.get('maxBytes')} "
            f"under {breach.get('path')}",
            "Decide whether to stop, widen the declared budget or repair; guard evidence is exact.",
            {"guardPath": breach.get("path"), "observedBytes": breach.get("observedBytes"),
             "maxBytes": breach.get("maxBytes")})
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


def refresh_with_status(board_file, task_id: str, status: dict, now=None, block: bool = True):
    """Short-lock projection refresh; no write when nothing meaningful changed."""
    now = time.time() if now is None else now
    board_file = Path(board_file)
    if not board_file.is_file():
        return {"ok": True, "refreshed": False, "reason": "no board"}
    try:
        fd = lock_fd(board_file.with_name(BOARD_LOCK), blocking=block,
                     timeout=5.0 if block else 0.0)
    except LockHeld:
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
        changed = changed or bool(added)
        if changed:
            board["revision"] = int(board.get("revision") or 0) + 1
            board["updatedAt"] = now
            atomic(board_file, board)
        return {"ok": True, "refreshed": bool(changed), "revision": board.get("revision"),
                "taskId": task_id, "pi": dict(card.get("pi") or {}),
                "newEvents": [event["id"] for event in added],
                "pendingEvents": sum(1 for event in card.get("events", [])
                                     if isinstance(event, dict) and not event.get("handled")),
                "card": compact_card(card)}
    finally:
        os.close(fd)


def refresh_registered_task(task: dict, now=None, block: bool = True):
    common_raw = task.get("commonDir") if isinstance(task, dict) else None
    if not isinstance(common_raw, str) or not common_raw.strip():
        return {"ok": True, "refreshed": False, "reason": "task has no commonDir"}
    board_file = board_file_for_common(Path(common_raw))
    if not board_file.is_file():
        return {"ok": True, "refreshed": False, "reason": "no board"}
    status = build_status(task["repo"], task["task"])
    return refresh_with_status(board_file, task["task"], status, now, block)


def record_monitor_error(task: dict, message: str) -> None:
    """Bounded monitor-error evidence; never kills Pi and never raises."""
    common_raw = task.get("commonDir") if isinstance(task, dict) else None
    if not isinstance(common_raw, str) or not common_raw.strip():
        return
    directory = Path(common_raw) / BOARD_DIR
    path = directory / MONITOR_LOG
    try:
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"schemaVersion": 1, "at": time.time(),
                           "task": task.get("task") if isinstance(task, dict) else None,
                           "error": _text(message, 300)}, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
        if path.stat().st_size > MAX_MONITOR_LOG_BYTES:
            _trim_lines(path, MAX_MONITOR_LOG_BYTES // 2)
    except OSError:
        pass


def refresh_supervisor(task: dict):
    """Bounded best-effort refresh called from the existing Pi supervisor loop."""
    try:
        return refresh_registered_task(task, block=False)
    except Exception as exc:  # noqa: BLE001 - monitor errors must not kill Pi
        try:
            record_monitor_error(task, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "refreshed": False, "reason": "monitor error"}


# ---------------------------------------------------------------------------
# registration and gate
# ---------------------------------------------------------------------------

def gate_paths(session: str):
    directory = handoff_root() / GATE_DIR
    return directory / f"{session_key(session)}.json", directory / f"{session_key(session)}.lock"


def read_gate(session: str):
    path, _lock = gate_paths(session)
    data, problem = _read_bounded_json(path, MAX_GATE_BYTES)
    if problem is not None:
        return None, problem
    if (not isinstance(data, dict) or data.get("schemaVersion") != GATE_SCHEMA_VERSION
            or data.get("sessionId") != session
            or not isinstance(data.get("delivered"), dict)):
        return None, "invalid"
    return data, None


def register_gate(session: str, automation_id: str, board_file: Path, task_ids, nonce: str,
                  instructions: str, now: float) -> dict:
    path, lock = gate_paths(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = lock_fd(lock, blocking=True, timeout=10)
    try:
        existing, _problem = _read_bounded_json(path, MAX_GATE_BYTES)
        existing = existing if isinstance(existing, dict) else {}
        existing_delivered = existing.get("delivered") if isinstance(existing.get("delivered"), dict) else {}
        record = {
            "schemaVersion": GATE_SCHEMA_VERSION, "sessionId": session,
            "sessionHash": session_key(session), "automationId": automation_id,
            "nonce": nonce, "instructions": instructions,
            "boardPath": str(board_file), "taskIds": list(task_ids),
            "createdAt": existing.get("createdAt") or now, "updatedAt": now,
            # Re-registering with the same nonce preserves delivery state; a new
            # nonce is an explicit re-arm that clears it.
            "delivered": existing_delivered if existing.get("nonce") == nonce else {},
        }
        atomic(path, record)
        return record
    finally:
        os.close(fd)


def rearm(repo, task, session_id, now=None) -> dict:
    """Explicitly clear delivery state for one registered session without changing the prompt."""
    now = time.time() if now is None else now
    _root, _common, board_file = board_file_for_repo(repo)
    task_id = require_task_arg(task)
    session = _validate_session(session_id)
    path, lock = gate_paths(session)
    fd = lock_fd(lock, blocking=True, timeout=10)
    try:
        gate, problem = read_gate(session)
        if gate is None:
            raise ValueError(f"no readable gate registration for this session ({problem})")
        if Path(str(gate.get("boardPath", ""))).resolve() != board_file.resolve():
            raise ValueError("gate registration board does not match this repository")
        if task_id not in (gate.get("taskIds") or []):
            raise ValueError(f"gate registration does not cover task {task_id!r}")
        gate["delivered"] = {}
        gate["updatedAt"] = now
        atomic(path, gate)
        return {"ok": True, "sessionId": session, "taskId": task_id,
                "deliveredCleared": True, "gatePath": str(path),
                "note": "explicit re-arm only; pending events are unchanged and will be "
                        "delivered once on the next exact registered tick"}
    finally:
        os.close(fd)


def envelope(automation_id: str, current_time_iso: str, instructions: str) -> str:
    return (f"<heartbeat>\n"
            f"  <automation_id>{automation_id}</automation_id>\n"
            f"  <current_time_iso>{current_time_iso}</current_time_iso>\n"
            f"  <instructions>\n{instructions}\n"
            f"  </instructions>\n</heartbeat>\n")


def _audit(decision: str, session: str, gate, cards, event_ids, started: float, now: float,
           reason=None) -> None:
    try:
        latency_ms = int(max(0.0, (time.monotonic() - started) * 1000))
        line = json.dumps({
            "schemaVersion": 1, "at": now, "decision": decision, "latencyMs": latency_ms,
            "sessionHash": session_key(session)[:16],
            "automationId": (gate or {}).get("automationId") if isinstance(gate, dict) else None,
            "taskIds": sorted({str(card.get("taskId")) for card in cards
                               if isinstance(card, dict)})[:10],
            "eventIds": [event_id for event_id in event_ids if isinstance(event_id, str)][:10],
            "reason": _text(reason, 200) if reason else None,
        }, ensure_ascii=False) + "\n"
        directory = handoff_root() / GATE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / AUDIT_FILE
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
        if path.stat().st_size > MAX_AUDIT_BYTES:
            _trim_lines(path, MAX_AUDIT_BYTES // 2)
    except OSError:
        pass


def stop_output(mode: str, reason: str) -> dict:
    if mode == "block":
        return {"decision": "block", "reason": reason}
    return {"continue": False, "stopReason": reason}


def _gate_diagnostic(session: str, message: str, started: float, now: float) -> dict:
    _audit("diagnostic", session, None, [], [], started, now, reason=message)
    text = ("codex-pi board gate diagnostic: " + _text(message, 400) +
            ". The automatic tick was allowed through (fail open); board state is unknown, "
            "not 'unchanged'.")
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def _mark_delivered(session: str, event_ids, now: float) -> None:
    path, lock = gate_paths(session)
    try:
        fd = lock_fd(lock, blocking=True, timeout=2)
    except LockHeld:
        return  # never block the hook; one redelivery is safer than a long wait
    try:
        data, problem = _read_bounded_json(path, MAX_GATE_BYTES)
        if problem is not None or not isinstance(data, dict):
            return
        delivered = data.setdefault("delivered", {})
        for event_id in event_ids:
            if isinstance(event_id, str):
                delivered[event_id] = now
        if len(delivered) > DELIVERED_LIMIT:
            ordered = sorted(delivered.items(),
                             key=lambda item: item[1] if isinstance(item[1], (int, float)) else 0)
            data["delivered"] = dict(ordered[-DELIVERED_LIMIT:])
        data["updatedAt"] = now
        atomic(path, data)
    finally:
        os.close(fd)


def cards_for_session(board: dict, session: str) -> list:
    result = []
    for card in (board.get("cards") or {}).values():
        if not isinstance(card, dict):
            continue
        if card.get("ownerSession") == session or card.get("codexTaskId") == session:
            result.append(card)
    return result


def _decide_hint(repo, task_id, event: dict) -> str:
    head = (event.get("candidate") or {}).get("head") or "unknown"
    return (f'python3 {shlex.quote(str(Path(__file__).resolve()))} decide '
            f'--repo {shlex.quote(str(repo))} --task {shlex.quote(str(task_id))} '
            f'--event-id {event.get("id")} --decision accept|reject --reviewed-head {head}')


def build_packet(session: str, pending, limit: int = 3) -> str:
    lines = ["codex-pi board packet (registered automatic tick; delivery is not acknowledgement)"]
    for card, event in pending[:limit]:
        evidence = event.get("evidence") or {}
        head = (event.get("candidate") or {}).get("head")
        lines.append(f"task={card.get('taskId')} round={event.get('round')} "
                     f"state={(card.get('pi') or {}).get('state')} "
                     f"paused={bool(card.get('paused'))}")
        lines.append(f"event={event.get('id')} kind={event.get('kind')} seq={event.get('seq')}")
        lines.append(f"summary={event.get('summary')}")
        lines.append(f"question={event.get('question')}")
        lines.append(f"candidate_head={head or 'unknown'}")
        for label, key in (("brief", "briefRef"), ("checks", "checksRef"),
                           ("receipt", "receiptRef"), ("state", "stateRef")):
            if evidence.get(key):
                lines.append(f"{label}={evidence.get(key)}")
        lines.append("decide=" + _decide_hint(card.get("repo"), card.get("taskId"), event))
    if len(pending) > limit:
        lines.append(f"... plus {len(pending) - limit} more pending events; run packet/show")
    return _text("\n".join(lines), MAX_PACKET_CHARS)


def evaluate_gate(event, stop_mode=None, now=None):
    """Return gate output for one exact registered tick, or None to pass through."""
    now = time.time() if now is None else now
    started = time.monotonic()
    prompt = event.get("prompt") if isinstance(event, dict) else None
    if not isinstance(prompt, str) or not prompt.startswith("<heartbeat>"):
        return None
    match = HEARTBEAT_RE.fullmatch(prompt)
    session_raw = event.get("session_id")
    if match is None or not isinstance(session_raw, str):
        return None
    try:
        session = _validate_session(session_raw)
    except ValueError:
        return None
    automation_id, _current_time, instructions = match.groups()
    gate, _problem = read_gate(session)
    if gate is None:
        return None  # no registered gate: current behavior applies
    if gate.get("automationId") != automation_id or gate.get("instructions") != instructions:
        return None  # other ids/instructions/nonces are ordinary prompts
    board_raw = gate.get("boardPath")
    if not isinstance(board_raw, str) or not Path(board_raw).is_absolute():
        return _gate_diagnostic(session, "gate registration has no valid board path", started, now)
    board_file = Path(board_raw)
    board, board_problem = read_board(board_file)
    if board is None:
        return _gate_diagnostic(session, f"board state is {board_problem}: {board_file}",
                                started, now)
    cards = cards_for_session(board, session)
    pending = []
    for card in cards:
        if card.get("paused"):
            continue
        for item in card.get("events", []):
            if isinstance(item, dict) and not item.get("handled"):
                pending.append((card, item))
    delivered = gate.get("delivered") if isinstance(gate.get("delivered"), dict) else {}
    new_pending = [(card, item) for card, item in pending if item.get("id") not in delivered]
    mode = stop_mode or os.environ.get("CODEX_PI_GATE_STOP_MODE") or "continue"
    if mode not in STOP_MODES:
        mode = "continue"
    if not new_pending:
        _audit("stop", session, gate, cards, [], started, now, reason="no actionable events")
        return stop_output(mode, "codex-pi board: no actionable events for this registered tick")
    packet = build_packet(session, new_pending)
    older = len(pending) - len(new_pending)
    if older > 0:
        packet = _text(packet + f"\n({older} older unhandled events were already delivered; "
                                 "run show/packet if needed)", MAX_PACKET_CHARS)
    event_ids = [item.get("id") for _card, item in new_pending]
    _mark_delivered(session, event_ids, now)
    _audit("deliver", session, gate, cards, event_ids, started, now)
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": packet}}


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
        "taskId": card.get("taskId"), "codexTaskId": card.get("codexTaskId"),
        "ownerSession": card.get("ownerSession"), "title": card.get("title"),
        "goal": card.get("goal"), "briefRef": card.get("briefRef"), "planRef": card.get("planRef"),
        "repo": card.get("repo"), "worktree": card.get("worktree"),
        "paused": bool(card.get("paused")), "pausedAt": card.get("pausedAt"),
        "pauseNote": card.get("pauseNote"), "pi": card.get("pi"),
        "evidence": card.get("evidence"), "check": card.get("check"),
        "attention": card.get("attention"), "codex": card.get("codex"),
        "pendingEvents": [compact_event(event) for event in pending[:max_events]],
        "pendingCount": len(pending),
        "recentHandled": [compact_event(event) for event in handled[-2:]],
    }


def select_cards(board: dict, owner=None, task_id=None, include_all=False) -> list:
    cards = board.get("cards") or {}
    if task_id is not None:
        card = cards.get(task_id)
        return [card] if isinstance(card, dict) else []
    if owner:
        return cards_for_session(board, owner)
    if include_all:
        return [card for card in cards.values() if isinstance(card, dict)][:50]
    raise ValueError("provide --task, --owner or --all")


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------

def register_task(repo, task, session_id, automation_id, title=None, goal=None, brief_ref=None,
                  plan_ref=None, codex_task_id=None, now=None, nonce=None,
                  new_nonce: bool = False) -> dict:
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
    session = _validate_session(session_id)
    automation = _validate_automation(automation_id)
    board_file = board_file_for_common(common)
    board_file.parent.mkdir(parents=True, exist_ok=True)
    if nonce is None:
        existing_gate, _problem = read_gate(session)
        reuse = (not new_nonce and isinstance(existing_gate, dict)
                 and existing_gate.get("automationId") == automation
                 and Path(str(existing_gate.get("boardPath", ""))).resolve() == board_file.resolve()
                 and isinstance(existing_gate.get("nonce"), str)
                 and bool(existing_gate.get("nonce")))
        nonce = existing_gate["nonce"] if reuse else secrets.token_hex(8)
    instructions = INSTRUCTIONS_TEMPLATE.format(nonce=nonce)
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
            card = _new_card(task_id, codex_task_id or session, session,
                             title or task_id, goal or "", brief_ref, plan_ref,
                             root, common, frozen.get("worktree"), now)
            cards[task_id] = card
        else:
            card["codexTaskId"] = codex_task_id or card.get("codexTaskId") or session
            card["ownerSession"] = session
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
        atomic(board_file, board)
    finally:
        os.close(fd)
    gate = register_gate(session, automation, board_file, [task_id], nonce, instructions, now)
    try:
        refresh_with_status(board_file, task_id, build_status(str(root), task_id), now, block=True)
    except (ValueError, OSError, LockHeld):
        pass
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session,
               "cwd": str(root), "prompt": envelope(automation, "2026-09-26T00:00:00Z",
                                                    instructions)}
    payload_text = json.dumps(payload, ensure_ascii=False)
    gate_cmd = (f"printf '%s' {shlex.quote(payload_text)} | "
                f"python3 {shlex.quote(str(Path(__file__).resolve()))} gate")
    hook_cmd = (f"printf '%s' {shlex.quote(payload_text)} | "
                f"python3 {shlex.quote(str(RUNTIME_DIR / 'pi_handoff.py'))} hook")
    return {
        "ok": True, "taskId": task_id, "codexTaskId": card.get("codexTaskId"),
        "boardPath": str(board_file), "revision": board.get("revision"),
        "gatePath": str(gate_paths(session)[0]), "automationId": automation, "nonce": nonce,
        "automationPrompt": instructions,
        "automationEnvelope": envelope(automation, "2026-09-26T00:00:00Z", instructions),
        "testCommands": [gate_cmd, hook_cmd],
        "note": "register this exact automation prompt; only this session/id/instructions envelope "
                "is eligible. Reading or delivering an event does not handle or accept it; "
                "re-registering with a new nonce is the explicit re-arm for delivered events.",
    }


def decide(repo, task, event_id, decision, reviewed_head=None, note=None, now=None) -> dict:
    now = time.time() if now is None else now
    event_id = str(event_id)
    if not EVENT_ID_RE.fullmatch(event_id):
        raise ValueError("event id must be a 64-character lowercase hex digest")
    mapped = DECISIONS.get(decision)
    if mapped is None:
        raise ValueError("decision must be accept, reject or changes_requested")
    if mapped == "accepted":
        if not reviewed_head or not HEAD_RE.fullmatch(str(reviewed_head)):
            raise ValueError("accept requires an explicit --reviewed-head (7-64 hex chars); "
                             "a zero exit code is never acceptance")
    else:
        if reviewed_head is not None and not HEAD_RE.fullmatch(str(reviewed_head)):
            raise ValueError("--reviewed-head must be a 7-64 hex commit id")
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
        if event is None:
            raise ValueError(f"unknown event {event_id} for task {task_id!r}")
        if event.get("handled"):
            if event.get("decision") == mapped:
                return {"ok": True, "idempotent": True, "taskId": task_id, "eventId": event_id,
                        "decision": mapped, "revision": board.get("revision")}
            raise ValueError(f"event {event_id} is already handled as "
                             f"{event.get('decision')!r}; refusing to overwrite a decision")
        candidate = event.get("candidate") or {}
        head = candidate.get("head")
        if mapped == "accepted" and head and str(reviewed_head).lower() != str(head).lower():
            raise ValueError(f"reviewed head {reviewed_head!r} does not match event candidate "
                             f"{head!r}; accept binds the exact candidate")
        event.update(handled=True, handledAt=now, decision=mapped,
                     reviewedHead=str(reviewed_head) if reviewed_head else None,
                     note=_text(note, MAX_NOTE) if note else None)
        codex = card.setdefault("codex", _default_codex())
        codex.update(review=mapped,
                     reviewedHead=str(reviewed_head) if reviewed_head else codex.get("reviewedHead"),
                     decidedAt=now, lastEventId=event_id)
        board["revision"] = int(board.get("revision") or 0) + 1
        board["updatedAt"] = now
        atomic(board_file, board)
        pending = sum(1 for item in card.get("events", [])
                      if isinstance(item, dict) and not item.get("handled"))
        return {"ok": True, "idempotent": False, "taskId": task_id, "eventId": event_id,
                "decision": mapped, "reviewedHead": reviewed_head,
                "revision": board["revision"], "pendingCount": pending,
                "note": "decision binds this exact event/candidate/round; accepted is explicit "
                        "main review, never exit 0"}
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
        atomic(board_file, board)
        return {"ok": True, "taskId": task_id, "paused": bool(paused),
                "revision": board["revision"],
                "note": ("paused: automatic ticks stop and progress events do not resume work"
                         if paused else "resumed explicitly; pending events are unchanged")}
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_register(args) -> dict:
    return register_task(args.repo, args.task, args.session_id, args.automation_id,
                         title=args.title, goal=args.goal, brief_ref=args.brief_ref,
                         plan_ref=args.plan_ref, codex_task_id=args.codex_task_id,
                         new_nonce=args.new_nonce)


def cmd_rearm(args) -> dict:
    return rearm(args.repo, args.task, args.session_id)


def cmd_refresh(args) -> dict:
    root, _common, board_file = board_file_for_repo(args.repo)
    task_id = require_task_arg(args.task)
    return refresh_with_status(board_file, task_id, build_status(str(root), task_id), block=True)


def cmd_show(args) -> dict:
    _root, _common, board_file = board_file_for_repo(args.repo)
    board, problem = read_board(board_file)
    if board is None:
        raise ValueError(f"board state is {problem}: {board_file}")
    owner = _validate_session(args.owner) if args.owner else None
    cards = select_cards(board, owner=owner, task_id=args.task, include_all=args.all)
    return {"ok": True, "revision": board.get("revision"), "count": len(cards),
            "cards": [compact_card(card) for card in cards],
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
    pending = [event for event in card.get("events", [])
               if isinstance(event, dict) and not event.get("handled")]
    if args.event_id:
        pending = [event for event in pending if event.get("id") == args.event_id]
    if not pending:
        return {"ok": True, "taskId": task_id, "pendingCount": 0,
                "note": "no unhandled events for this task; nothing to deliver"}
    return {"ok": True, "taskId": task_id, "revision": board.get("revision"),
            "pendingCount": len(pending),
            "events": [compact_event(event) for event in pending[:5]],
            "note": "reading/delivering does not handle or accept; use decide"}


def cmd_decide(args) -> dict:
    return decide(args.repo, args.task, args.event_id, args.decision,
                  reviewed_head=args.reviewed_head, note=args.note)


def cmd_pause(args) -> dict:
    return set_paused(args.repo, args.task, True, note=args.note)


def cmd_resume(args) -> dict:
    return set_paused(args.repo, args.task, False, note=args.note)


def cmd_gate(args) -> dict:
    raw = sys.stdin.read(MAX_GATE_BYTES + 1)
    try:
        event = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(event, dict):
        return {}
    output = evaluate_gate(event, stop_mode=args.stop_mode)
    return output if output is not None else {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi_board.py",
        description="Opt-in structured Codex-Pi board projection plus the exact registered "
                    "UserPromptSubmit pre-model gate. Never invokes the Codex CLI or a model.")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="bind an existing task to an owning Codex session")
    register.add_argument("--repo", required=True)
    register.add_argument("--task", required=True)
    register.add_argument("--session-id", required=True)
    register.add_argument("--automation-id", required=True)
    register.add_argument("--title")
    register.add_argument("--goal")
    register.add_argument("--brief-ref")
    register.add_argument("--plan-ref")
    register.add_argument("--codex-task-id")
    register.add_argument("--new-nonce", action="store_true",
                          help="explicitly rotate the automation nonce; clears delivery state")
    register.set_defaults(func=cmd_register)

    rearm = sub.add_parser("rearm", help="explicitly clear delivery state without changing the prompt")
    rearm.add_argument("--repo", required=True)
    rearm.add_argument("--task", required=True)
    rearm.add_argument("--session-id", required=True)
    rearm.set_defaults(func=cmd_rearm)

    refresh = sub.add_parser("refresh", help="one-shot bounded card projection refresh")
    refresh.add_argument("--repo", required=True)
    refresh.add_argument("--task", required=True)
    refresh.set_defaults(func=cmd_refresh)

    show = sub.add_parser("show", help="bounded compact cards for one owner or task")
    show.add_argument("--repo", required=True)
    show.add_argument("--owner")
    show.add_argument("--task")
    show.add_argument("--all", action="store_true")
    show.set_defaults(func=cmd_show)

    packet = sub.add_parser("packet", help="bounded pending-event packet for one task")
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

    pause = sub.add_parser("pause", help="persist explicit pause; automatic ticks stop")
    pause.add_argument("--repo", required=True)
    pause.add_argument("--task", required=True)
    pause.add_argument("--note")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="explicitly resume a paused card")
    resume.add_argument("--repo", required=True)
    resume.add_argument("--task", required=True)
    resume.add_argument("--note")
    resume.set_defaults(func=cmd_resume)

    gate = sub.add_parser("gate", help=argparse.SUPPRESS)
    gate.add_argument("--stop-mode", choices=STOP_MODES)
    gate.set_defaults(func=cmd_gate)
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
