#!/usr/bin/env python3
"""Bounded, no-follow size measurement shared by pi_check and pi_copy.

This module never follows symlinks: it uses ``os.lstat`` and
``os.scandir(..., follow_symlinks=False)``. Measurements bounded by entry count
or wall time are reported as incomplete/unknown instead of guessed, and callers
must treat unknown as "not verified", never as under budget.

Only regular-file bytes are counted. Directories are entered, symlinks are
recorded and skipped, and other entry types are counted but contribute no
bytes.
"""
from __future__ import annotations

import os
import stat
import time

DEFAULT_MAX_ENTRIES = 200_000
DEFAULT_MAX_SECONDS = 5.0
MAX_BYTES_VALUE = 10 ** 18
MAX_PATH_TEXT = 400
MAX_REASON_TEXT = 200


def _path_text(path) -> str:
    """Absolute, user-expanded text without following a final symlink."""
    return os.path.abspath(os.path.expanduser(str(path)))


def _reason(exc) -> str:
    text = str(exc).replace("\x00", " ").strip()
    return text[:MAX_REASON_TEXT]


def _base_result(path) -> dict:
    return {
        "path": _path_text(path),
        "exists": False,
        "type": None,
        "bytes": 0,
        "regularFiles": 0,
        "directories": 0,
        "symlinksSkipped": 0,
        "otherEntries": 0,
        "entries": 0,
        "complete": False,
        "unknown": True,
        "reason": None,
        "boundedBy": None,
        "elapsedSeconds": 0.0,
    }


def measure(path, max_entries: int = DEFAULT_MAX_ENTRIES,
            max_seconds: float = DEFAULT_MAX_SECONDS) -> dict:
    """Measure regular-file bytes beneath ``path`` without following symlinks.

    Result keys:

    ``bytes``
        Regular-file byte total. For an incomplete measurement this is a lower
        bound only.
    ``complete`` / ``unknown``
        True/False when the whole declared tree was measured; incomplete or
        unreadable measurements are ``complete=False``, ``unknown=True``.
    ``boundedBy``
        ``None``, ``"entries"`` or ``"time"`` when an internal bound stopped
        the scan.

    This wrapper never raises for filesystem/iteration errors: an unexpected
    failure returns a bounded unknown measurement so a guard cannot strand its
    owned child on a measurement exception.
    """
    try:
        return _measure(path, max_entries, max_seconds)
    except Exception as exc:  # noqa: BLE001 - bounded unknown beats an uncaught error
        result = _base_result(path)
        try:
            result["exists"] = os.path.lexists(result["path"])
        except Exception:  # noqa: BLE001
            pass
        result["reason"] = f"measurement failed: {_reason(exc)}"
        return result


def _measure(path, max_entries: int = DEFAULT_MAX_ENTRIES,
             max_seconds: float = DEFAULT_MAX_SECONDS) -> dict:
    started = time.monotonic()
    result = _base_result(path)
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
        result["reason"] = "invalid entry bound"
        return result
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) \
            or not 0 < float(max_seconds) < float("inf"):
        result["reason"] = "invalid time bound"
        return result
    try:
        root_stat = os.lstat(result["path"])
    except FileNotFoundError:
        result["reason"] = "path does not exist"
        return result
    except OSError as exc:
        result["reason"] = f"unreadable path: {_reason(exc)}"
        return result

    result["exists"] = True
    mode = root_stat.st_mode
    if stat.S_ISLNK(mode):
        result["type"] = "symlink"
        result["symlinksSkipped"] = 1
        result["reason"] = "symlink is not followed"
        return result
    if stat.S_ISREG(mode):
        result["type"] = "file"
        result["bytes"] = root_stat.st_size
        result["regularFiles"] = 1
        result["entries"] = 1
        result["complete"] = True
        result["unknown"] = False
        return result
    if not stat.S_ISDIR(mode):
        result["type"] = "other"
        result["otherEntries"] = 1
        result["entries"] = 1
        result["reason"] = "not a regular file or directory"
        return result

    result["type"] = "directory"
    complete = True
    stack = [result["path"]]
    while stack:
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError as exc:
            complete = False
            result["reason"] = f"unreadable directory: {_reason(exc)}"
            continue
        with iterator:
            while True:
                if time.monotonic() - started > max_seconds:
                    complete = False
                    result["boundedBy"] = "time"
                    result["reason"] = "time bound reached; measurement is incomplete"
                    stack.clear()
                    break
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError as exc:
                    complete = False
                    result["reason"] = f"unreadable directory entry: {_reason(exc)}"
                    stack.clear()
                    break
                result["entries"] += 1
                if result["entries"] > max_entries:
                    complete = False
                    result["boundedBy"] = "entries"
                    result["reason"] = "entry bound reached; measurement is incomplete"
                    stack.clear()
                    break
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    complete = False
                    result["reason"] = f"unreadable entry: {_reason(exc)}"
                    continue
                entry_mode = entry_stat.st_mode
                if stat.S_ISLNK(entry_mode):
                    result["symlinksSkipped"] += 1
                elif stat.S_ISDIR(entry_mode):
                    result["directories"] += 1
                    stack.append(entry.path)
                elif stat.S_ISREG(entry_mode):
                    result["regularFiles"] += 1
                    result["bytes"] += entry_stat.st_size
                else:
                    result["otherEntries"] += 1
    result["complete"] = complete
    result["unknown"] = not complete
    result["elapsedSeconds"] = round(time.monotonic() - started, 6)
    return result


def sanitize_snapshot(value, path_limit: int = MAX_PATH_TEXT):
    """Bound an untrusted resource-guard snapshot read from marker/receipt JSON.

    Returns ``None`` for anything that is not a dict. Only fixed, numeric and
    clipped fields survive; timestamps and free-form payloads are dropped so the
    same guard state cannot churn observer fingerprints.
    """
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    reason = value.get("reason")
    out = {
        "path": path[:path_limit] if isinstance(path, str) else None,
        "maxBytes": None,
        "observedBytes": None,
        "breached": bool(value.get("breached")),
        "unknown": bool(value.get("unknown")),
        "complete": bool(value.get("complete")),
        "reason": reason[:MAX_REASON_TEXT] if isinstance(reason, str) else None,
        "scans": None,
        "unknownScans": None,
        "stoppedChild": None,
        "childExitCode": None,
    }
    for key, target, limit in (("max_bytes", "maxBytes", MAX_BYTES_VALUE),
                               ("observed_bytes", "observedBytes", MAX_BYTES_VALUE)):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= limit:
            out[target] = item
    for key, target, limit in (("scans", "scans", 10 ** 9),
                               ("unknown_scans", "unknownScans", 10 ** 9)):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= limit:
            out[target] = item
    stopped = value.get("stopped_child")
    if isinstance(stopped, bool):
        out["stoppedChild"] = stopped
    basis = value.get("breach_basis")
    if isinstance(basis, str) and basis in ("complete", "partial lower bound"):
        out["breachBasis"] = basis
    interval = value.get("health_interval_seconds")
    if isinstance(interval, (int, float)) and not isinstance(interval, bool) \
            and 0 < float(interval) <= 3600:
        out["healthIntervalSeconds"] = float(interval)
    child = value.get("child_exit_code")
    if isinstance(child, int) and not isinstance(child, bool) and -255 <= child <= 255:
        out["childExitCode"] = child
    return out
