#!/usr/bin/env python3
"""Safe evidence copy with literal symlink preservation and an optional byte cap.

Contract::

    python3 pi_copy.py SOURCE DEST --max-bytes N

- ``--max-bytes`` is required and must be a positive integer: an unknown or
  incomplete preflight/final measurement never succeeds and never claims a
  safe copy.
- ``DEST`` must not exist (any type, including a dangling symlink), and it must
  not be inside ``SOURCE`` (a recursive copy is refused).
- ``SOURCE`` is copied recursively. Symlinks are recreated literally with
  ``os.readlink``/``os.symlink``; their targets are never followed, so
  dangling, external and cyclic links are preserved without expanding or
  recursing into them.
- Regular-file bytes and the optional budget use the bounded no-follow walk
  from ``pi_size.py``. Symlink targets never count toward the budget and are
  never copied.
- With ``--max-bytes`` an unknown preflight measurement refuses the copy. A
  known preflight overage refuses before any write. Growth beyond the cap
  stops the copy with partial evidence retained and a nonzero exit. After the
  copy the source and destination are re-measured; any unknown or violation is
  reported instead of a false success.
- Without ``--max-bytes`` the copy reports measured bytes but declares no
  budget.

Exit codes: 0 verified copy, 1 copy failure, 2 refused/usage, 3 budget
violation, 4 budget verification unknown. JSON is printed on stdout in every
non-argparse case. This is a bounded copy helper, not a backup system.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys

# The frozen helper snapshot is immutable evidence: never write bytecode caches
# into the task tools directory.
sys.dont_write_bytecode = True

from pi_size import measure

CHUNK_BYTES = 1024 * 1024


class BudgetExceeded(Exception):
    """The declared byte budget was crossed while copying."""


class CopyError(Exception):
    """The copy contract was violated or the filesystem refused an operation."""


def _normalize(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def _measure_summary(measurement: dict) -> dict:
    return {
        "path": measurement.get("path"),
        "exists": bool(measurement.get("exists")),
        "type": measurement.get("type"),
        "bytes": measurement.get("bytes"),
        "complete": bool(measurement.get("complete")),
        "unknown": bool(measurement.get("unknown")),
        "reason": measurement.get("reason"),
        "entries": measurement.get("entries"),
        "regularFiles": measurement.get("regularFiles"),
        "symlinksSkipped": measurement.get("symlinksSkipped"),
    }


def _emit(payload: dict, message, code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    if message:
        print(f"pi_copy: {message}", file=sys.stderr)
    return code


def _refuse(payload: dict, message: str) -> int:
    payload["status"] = "refused"
    payload["reason"] = message
    return _emit(payload, message, 2)


def _copy_entry(source: str, destination: str, state: dict, budget) -> None:
    source_stat = os.lstat(source)
    mode = source_stat.st_mode
    if stat.S_ISLNK(mode):
        # Literal preservation: never resolve, stat or recurse into the target.
        os.symlink(os.readlink(source), destination)
        state["symlinks"] += 1
        return
    if stat.S_ISREG(mode):
        with open(source, "rb") as incoming:
            try:
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                     stat.S_IMODE(mode))
            except FileExistsError:
                raise CopyError(f"destination entry already exists: {destination}") from None
            with os.fdopen(descriptor, "wb") as outgoing:
                while True:
                    chunk = incoming.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    if budget is not None and state["bytes"] + len(chunk) > budget:
                        state["breached"] = True
                        state["observed"] = state["bytes"] + len(chunk)
                        raise BudgetExceeded(
                            f"copy would exceed --max-bytes {budget}: "
                            f"{state['observed']} source bytes at {source}")
                    outgoing.write(chunk)
                    state["bytes"] += len(chunk)
        state["files"] += 1
        try:
            shutil.copystat(source, destination, follow_symlinks=False)
        except OSError:
            pass
        return
    if stat.S_ISDIR(mode):
        try:
            os.mkdir(destination, stat.S_IMODE(mode))
        except FileExistsError:
            raise CopyError(f"destination directory already exists: {destination}") from None
        state["dirs"] += 1
        with os.scandir(source) as entries:
            for entry in entries:
                _copy_entry(entry.path, os.path.join(destination, entry.name), state, budget)
        try:
            shutil.copystat(source, destination, follow_symlinks=False)
        except OSError:
            pass
        return
    raise CopyError(f"unsupported source entry type: {source}")


def perform_copy(source: str, destination: str, max_bytes=None, measure_fn=measure) -> tuple:
    """Copy ``source`` to the new ``destination``; returns ``(payload, exit_code)``.

    ``measure_fn`` is injectable for tests that need to exercise unknown or
    violation final verification without filesystem races.
    """
    source = _normalize(source)
    destination = _normalize(destination)
    payload = {
        "schemaVersion": 1,
        "source": source,
        "dest": destination,
        "maxBytes": max_bytes,
        "sourcePreflight": None,
        "destinationFinal": None,
        "sourceFinal": None,
        "copiedBytes": 0,
        "filesCopied": 0,
        "dirsCreated": 0,
        "symlinksCopied": 0,
        "partial": False,
        "status": "refused",
        "violation": None,
        "unknown": [],
        "acceptance": "not_verified",
    }
    if max_bytes is not None and (isinstance(max_bytes, bool) or max_bytes <= 0):
        return payload, _refuse(payload, "--max-bytes must be a positive integer")
    if os.path.lexists(destination):
        return payload, _refuse(payload, "destination already exists; refusing to overwrite")
    if not os.path.lexists(source):
        return payload, _refuse(payload, "source does not exist")
    parent = os.path.dirname(destination) or "."
    if not os.path.isdir(parent):
        return payload, _refuse(payload, "destination parent directory does not exist")
    real_source = os.path.realpath(source)
    real_parent = os.path.realpath(parent)
    if real_parent == real_source or real_parent.startswith(real_source.rstrip(os.sep) + os.sep):
        return payload, _refuse(payload, "destination is inside the source; refusing a recursive copy")

    preflight = measure_fn(source)
    payload["sourcePreflight"] = _measure_summary(preflight)
    if max_bytes is not None:
        if preflight.get("unknown") or not preflight.get("complete"):
            payload["status"] = "unknown"
            payload["unknown"].append("source preflight measurement was incomplete or unreadable")
            return payload, _emit(payload, "refusing copy: source size is unknown; the declared budget "
                                           "cannot be verified", 4)
        if preflight.get("bytes") is not None and preflight["bytes"] > max_bytes:
            payload["status"] = "budget_exceeded"
            payload["violation"] = {"reason": "source exceeds --max-bytes before any write",
                                    "observedBytes": preflight["bytes"],
                                    "maxBytes": max_bytes, "path": source, "phase": "preflight"}
            return payload, _emit(payload, f"source measures {preflight['bytes']} bytes and exceeds "
                                           f"--max-bytes {max_bytes}; nothing was written", 3)

    state = {"bytes": 0, "files": 0, "dirs": 0, "symlinks": 0, "breached": False,
             "observed": None}
    try:
        _copy_entry(source, destination, state, max_bytes)
    except BudgetExceeded as exc:
        payload.update(partial=True, status="budget_exceeded",
                       copiedBytes=state["bytes"], filesCopied=state["files"],
                       dirsCreated=state["dirs"], symlinksCopied=state["symlinks"],
                       violation={"reason": str(exc), "observedBytes": state.get("observed"),
                                  "maxBytes": max_bytes, "path": source, "phase": "copy"})
        return payload, _emit(payload, str(exc), 3)
    except (CopyError, OSError) as exc:
        payload.update(partial=True, status="error",
                       copiedBytes=state["bytes"], filesCopied=state["files"],
                       dirsCreated=state["dirs"], symlinksCopied=state["symlinks"],
                       reason=str(exc)[:400])
        return payload, _emit(payload, f"copy failed: {exc}", 1)

    payload.update(copiedBytes=state["bytes"], filesCopied=state["files"],
                   dirsCreated=state["dirs"], symlinksCopied=state["symlinks"])
    destination_final = measure_fn(destination)
    source_final = measure_fn(source)
    payload["destinationFinal"] = _measure_summary(destination_final)
    payload["sourceFinal"] = _measure_summary(source_final)

    if max_bytes is not None:
        incomplete = [name for name, measurement in (("destination", destination_final),
                                                     ("source", source_final))
                      if measurement.get("unknown") or not measurement.get("complete")]
        if incomplete:
            payload["status"] = "unknown"
            payload["unknown"].append(
                "post-copy measurement was incomplete or unreadable for: " + ", ".join(incomplete))
            return payload, _emit(payload, "copy finished but the declared budget cannot be verified; "
                                           "reporting unknown instead of success", 4)
        for name, measurement in (("destination", destination_final), ("source", source_final)):
            if measurement.get("bytes") is not None and measurement["bytes"] > max_bytes:
                payload["status"] = "budget_exceeded"
                payload["violation"] = {"reason": f"{name} exceeds --max-bytes after copy",
                                        "observedBytes": measurement["bytes"],
                                        "maxBytes": max_bytes, "path": measurement.get("path"),
                                        "phase": "final"}
                return payload, _emit(payload, f"{name} measures {measurement['bytes']} bytes and exceeds "
                                               f"--max-bytes {max_bytes}; partial evidence retained", 3)

    payload["status"] = "copied"
    return payload, _emit(payload, None, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source")
    parser.add_argument("dest")
    parser.add_argument("--max-bytes", type=int, required=True,
                        help="required positive byte budget for regular files under SOURCE; "
                             "symlinks do not count and are never followed")
    args = parser.parse_args()
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be a positive integer")
    payload, code = perform_copy(args.source, args.dest, args.max_bytes)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
