#!/usr/bin/env python3
"""Run one project check command and retain its true exit, logs and revision.

This helper records execution evidence only. exit 0 is never acceptance PASS.
No Codex CLI is invoked here.

While the child runs, a uniquely named ``<id>-<nonce>.running`` marker records
the attempt identity, wrapper start/deadline, child PID, optional resource
guard snapshot and log name so the read-only ``pi_task.py status`` snapshot can
see the attempt without scanning the transcript. The wrapper deadline is never
an inner command deadline, and a command failure inside the wrapper (for
example a test runner's own timeout) stays a nonzero exit even when this
wrapper itself did not time out. The final immutable receipt is written and the
running marker is removed in a final cleanup; a killed wrapper leaves the
marker as crash evidence.

Optional declared directory budget: ``--watch-path PATH --max-bytes N`` counts
regular-file bytes under one explicitly selected path without following
symlinks, before spawning and at ``--health-interval-seconds`` (default 15)
while the owned child runs. Incomplete or unreadable measurements stay unknown
and are never treated as under budget. A known breach stops only this owned
check process group, retains the log and writes an immutable receipt with
explicit resourceLimit bytes/path/reason and a nonzero exit; a breach observed
at child exit also prevents a pass. Timed out, cancelled and guard-stopped
attempts keep distinct fields. Without both options the helper is unguarded.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

# The frozen helper snapshot is immutable evidence: never write bytecode caches
# into the task tools directory.
sys.dont_write_bytecode = True

from pi_size import measure
from pi_task import atomic, terminate

RUNNING_SUFFIX = ".running"
GUARD_EXIT_CODE = 75
MIN_HEALTH_INTERVAL = 0.05
MAX_HEALTH_INTERVAL = 3600.0
UNITTEST_RAN_RE = re.compile(r'^Ran (?P<run>\d+) tests? in [\d.]+s\s*$')
UNITTEST_RESULT_RE = re.compile(r'^(?P<result>OK|FAILED)(?:\s*\((?P<detail>[^)]*)\))?\s*$')


def unittest_counts(text: str):
    """Counts from the unambiguous final unittest summary; None otherwise.

    The last ``Ran N tests ...`` line is the final candidate summary. It must be
    followed by a recognizable ``OK``/``FAILED`` line; every detail field must be
    a well-formed, non-duplicated integer ``key=value``; the detail counters must
    be consistent with the result and the run count. Malformed, contradictory or
    incomplete evidence returns ``None``, so declared ``minRun``/``forbidSkip``
    rules stay unknown instead of passing on a contradictory zero-exit log.
    """
    lines = text.splitlines()
    last_ran = None
    for index, line in enumerate(lines):
        match = UNITTEST_RAN_RE.match(line.strip())
        if match:
            last_ran = (index, int(match.group('run')))
    if last_ran is None:
        return None
    result_line = next((line.strip() for line in lines[last_ran[0] + 1:] if line.strip()), None)
    if result_line is None:
        return None
    result = UNITTEST_RESULT_RE.match(result_line)
    if result is None:
        return None
    failures = errors = skipped = 0
    seen = set()
    detail = result.group('detail')
    if detail is not None:
        for part in detail.split(','):
            key, _sep, value = part.strip().partition('=')
            if not key or not _sep or not value.isdigit() or key in seen:
                return None
            seen.add(key)
            if key == 'failures':
                failures = int(value)
            elif key == 'errors':
                errors = int(value)
            elif key == 'skipped':
                skipped = int(value)
            # other well-formed integer fields (for example expected failures)
            # are forward-compatible and do not change the fixed counters
    run = last_ran[1]
    fail = failures + errors
    if result.group('result') == 'FAILED' and fail <= 0:
        return None
    if result.group('result') == 'OK' and fail > 0:
        return None
    if fail + skipped > run:
        return None
    return {'run': run, 'pass': max(0, run - fail - skipped), 'fail': fail,
            'skip': skipped, 'format': 'python_unittest_summary'}


class Guard:
    """One declared no-follow directory byte budget for an owned check child."""

    def __init__(self, path, max_bytes: int, interval: float):
        self.path = Path(os.path.abspath(os.path.expanduser(str(path))))
        self.max_bytes = int(max_bytes)
        self.interval = float(interval)
        self.scans = 0
        self.unknown_scans = 0
        self.breached = False
        self.stopped_child = False
        self.child_exit_code = None
        self.reason = None
        self.breach_basis = None
        self.preflight = None
        self.first_breach = None
        self.last = None

    def scan(self, preflight: bool = False) -> dict:
        try:
            measurement = measure(self.path)
        except Exception as exc:  # noqa: BLE001 - a guard must never strand its owned child
            measurement = {
                "path": str(self.path), "exists": False, "type": None, "bytes": None,
                "complete": False, "unknown": True,
                "reason": f"guard measurement failed: {exc}"[:200],
                "boundedBy": "error", "regularFiles": 0, "symlinksSkipped": 0,
                "entries": 0, "elapsedSeconds": 0.0,
            }
        self.scans += 1
        if measurement.get("unknown"):
            self.unknown_scans += 1
        self.last = measurement
        if preflight:
            self.preflight = measurement
        # A bounded/incomplete walk still yields a lower bound: if that known
        # lower bound already exceeds the cap, exceeding the cap is proven and
        # the owned check must stop even though the full size stays unknown.
        observed = measurement.get("bytes")
        if isinstance(observed, int) and not isinstance(observed, bool) and observed > self.max_bytes:
            complete = bool(measurement.get("complete")) and not measurement.get("unknown")
            self.breached = True
            self.breach_basis = "complete" if complete else "partial lower bound"
            if self.first_breach is None:
                self.first_breach = measurement
            self.reason = (f"observed at least {observed} bytes under {self.path} exceed "
                           f"the declared max_bytes {self.max_bytes}"
                           + ("" if complete else " (partial lower bound; full size unknown)"))
        return measurement

    def snapshot(self) -> dict:
        last = self.last or {}
        return {
            "path": str(self.path),
            "max_bytes": self.max_bytes,
            "observed_bytes": last.get("bytes"),
            "breached": self.breached,
            "breach_basis": self.breach_basis,
            "unknown": bool(last.get("unknown")),
            "complete": bool(last.get("complete")),
            "reason": self.reason or last.get("reason"),
            "scans": self.scans,
            "unknown_scans": self.unknown_scans,
            "health_interval_seconds": self.interval,
            "stopped_child": self.stopped_child,
            "child_exit_code": self.child_exit_code,
            "checked_at": time.time(),
            "scope": "regular file bytes under the declared path; symlinks are never followed",
            "note": "local detection only; unknown is not verified and is never treated as under budget",
        }


def remove_running(marker: Path) -> None:
    try:
        marker.unlink()
    except OSError:
        pass


def wait_for_child(child, guard, timeout_seconds: float, marker: Path, marker_data: dict):
    """Wait for the owned child, scanning the guard each health interval.

    Returns ``(exit_code, timed_out)``. A guard breach terminates with the
    dedicated guard exit so timeouts, cancellations and guard stops remain
    distinguishable.
    """
    deadline = time.monotonic() + timeout_seconds
    interval = guard.interval if guard is not None else timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 124, True
        try:
            raw = child.wait(timeout=max(0.01, min(remaining, interval)))
        except subprocess.TimeoutExpired:
            if guard is not None:
                guard.scan()
                marker_data["resource_limit"] = guard.snapshot()
                atomic(marker, marker_data)
                if guard.breached:
                    guard.stopped_child = True
                    return GUARD_EXIT_CODE, False
            continue
        return (raw if raw >= 0 else 128 - raw), False


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--timeout-seconds', type=float, default=3600)
    parser.add_argument('--watch-path', type=Path,
                        help='declared path whose regular-file bytes are budgeted; requires '
                             '--max-bytes and never follows symlinks')
    parser.add_argument('--max-bytes', type=int,
                        help='positive byte budget for regular files under --watch-path')
    parser.add_argument('--health-interval-seconds', type=float, default=15.0,
                        help='guard scan period while the owned child runs (default 15)')
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    if not argv or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', args.id):
        parser.error('provide a safe check id and a command after --')
    if not 0 < args.timeout_seconds <= 604800:
        parser.error('timeout must be positive and at most seven days')
    if (args.watch_path is None) != (args.max_bytes is None):
        parser.error('--watch-path and --max-bytes must be provided together')
    if args.max_bytes is not None and args.max_bytes <= 0:
        parser.error('--max-bytes must be a positive integer')
    if not (math.isfinite(args.health_interval_seconds)
            and MIN_HEALTH_INTERVAL <= args.health_interval_seconds <= MAX_HEALTH_INTERVAL):
        parser.error(f'--health-interval-seconds must be a finite value between '
                     f'{MIN_HEALTH_INTERVAL} and {MAX_HEALTH_INTERVAL}')
    guard = Guard(args.watch_path, args.max_bytes, args.health_interval_seconds) \
        if args.watch_path is not None else None

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.id + '-' + uuid.uuid4().hex[:12]
    log = args.output_dir / (stem + '.log')
    receipt = args.output_dir / (stem + '.json')
    marker = args.output_dir / (stem + RUNNING_SUFFIX)
    child = None
    timed_out, code, error = False, 1, None
    started = time.time()
    deadline = started + args.timeout_seconds
    marker_data = {
        'schema_version': 1, 'id': args.id, 'pid': None, 'started_at': started,
        'deadline_at': deadline, 'timeout_seconds': args.timeout_seconds,
        'deadline_scope': 'wrapper timeout only; never an inner command deadline',
        'log': log.name, 'receipt': receipt.name,
    }
    if guard is not None:
        marker_data['resource_limit'] = guard.snapshot()
    caught = {'signal': None}

    def interrupted(sig, _frame):
        caught['signal'] = sig
        raise KeyboardInterrupt

    # Marker first: a unique attempt identity exists before the child starts.
    atomic(marker, marker_data)
    try:
        previous = {s: signal.signal(s, interrupted) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            with log.open('xb') as output:
                if guard is not None:
                    guard.scan(preflight=True)
                    marker_data['resource_limit'] = guard.snapshot()
                    atomic(marker, marker_data)
                if guard is None or not guard.breached:
                    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                             stderr=output, start_new_session=True)
                    marker_data['pid'] = child.pid
                    if guard is not None:
                        marker_data['resource_limit'] = guard.snapshot()
                    atomic(marker, marker_data)
                    code, timed_out = wait_for_child(child, guard, args.timeout_seconds,
                                                     marker, marker_data)
                else:
                    # A pre-existing known breach stops this run before any child
                    # is created; nothing outside this check is touched.
                    guard.stopped_child = True
                    code = GUARD_EXIT_CODE
        except KeyboardInterrupt:
            code = 128 + (caught['signal'] or signal.SIGINT)
        except OSError as exc:
            error = str(exc)
        finally:
            for s in previous:
                signal.signal(s, signal.SIG_IGN)
            if child is not None:
                terminate(child)
                if guard is not None:
                    guard.child_exit_code = child.returncode
            for s, handler in previous.items():
                signal.signal(s, handler)
            if guard is not None:
                # Final measurement also covers bytes added at child exit; a
                # breach here must not be reported as a pass.
                guard.scan()
                marker_data['resource_limit'] = guard.snapshot()
                try:
                    atomic(marker, marker_data)
                except OSError as exc:
                    error = error or f"guard marker update failed: {exc}"
                if guard.breached:
                    guard.stopped_child = guard.stopped_child or child is not None
                    if not timed_out and caught['signal'] is None and error is None:
                        code = GUARD_EXIT_CODE
        raw = log.read_bytes() if log.exists() else b''
        text = raw.decode(errors='replace')
        counters = {'run': len(re.findall(r'^=== RUN\s', text, re.M)),
                    'pass': len(re.findall(r'^--- PASS:', text, re.M)),
                    'fail': len(re.findall(r'^--- FAIL:', text, re.M)),
                    'skip': len(re.findall(r'^--- SKIP:', text, re.M))}
        if any(counters.values()):
            counts = dict(counters, format='go_verbose_top_level')
        else:
            counts = unittest_counts(text)
        try:
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True,
                                           stderr=subprocess.DEVNULL).strip()
            status = subprocess.check_output(['git', 'status', '--porcelain'], stderr=subprocess.DEVNULL)
            diff = subprocess.check_output(['git', 'diff', 'HEAD', '--binary'], stderr=subprocess.DEVNULL)
            dirty, diff_hash = bool(status), hashlib.sha256(diff).hexdigest()
        except subprocess.CalledProcessError:
            head, dirty, diff_hash = None, None, None
        resource_limit = guard.snapshot() if guard is not None else None
        data = {'schema_version': 1, 'id': args.id, 'argv': argv, 'cwd': str(Path.cwd()),
                'head': head, 'dirty': dirty, 'tracked_diff_sha256': diff_hash,
                'started_at': started, 'ended_at': time.time(), 'deadline_at': deadline,
                'exit_code': code, 'timed_out': timed_out,
                'cancelled': caught['signal'] is not None,
                'signal': caught['signal'],
                'error': error, 'test_counts': counts, 'log': log.name,
                'log_sha256': hashlib.sha256(raw).hexdigest(),
                'running_marker': marker.name,
                'resource_limit': resource_limit,
                'resourceLimit': resource_limit,
                'acceptance': 'not_verified'}
        # The immutable receipt replaces the terminal state; the marker is
        # removed in the final cleanup below even if receipt writing fails.
        atomic(receipt, data)
        print(json.dumps({'receipt': str(receipt.resolve()), 'exit_code': code, 'timed_out': timed_out,
                          'cancelled': caught['signal'] is not None,
                          'resource_limit': data['resource_limit'],
                          'test_counts': counts, 'acceptance': 'not_verified'}))
        return code
    finally:
        remove_running(marker)


if __name__ == '__main__':
    raise SystemExit(main())
