#!/usr/bin/env python3
"""Run one project check command and retain its true exit, logs and revision.

This helper records execution evidence only. exit 0 is never acceptance PASS.
No Codex CLI is invoked here.

While the child runs, a uniquely named ``<id>-<nonce>.running`` marker records
the attempt identity, wrapper start/deadline, child PID and log name so the
read-only ``pi_task.py status`` snapshot can see the attempt without scanning
the transcript. The wrapper deadline is never an inner command deadline, and a
command failure inside the wrapper (for example a test runner's own timeout)
stays a nonzero exit even when this wrapper itself did not time out. The final
immutable receipt is written and the running marker is removed in a final
cleanup; a killed wrapper leaves the marker as crash evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

from pi_task import atomic, terminate

RUNNING_SUFFIX = ".running"


def remove_running(marker: Path) -> None:
    try:
        marker.unlink()
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--timeout-seconds', type=float, default=3600)
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    if not argv or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', args.id):
        parser.error('provide a safe check id and a command after --')
    if not 0 < args.timeout_seconds <= 604800:
        parser.error('timeout must be positive and at most seven days')
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
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                         start_new_session=True)
                marker_data['pid'] = child.pid
                atomic(marker, marker_data)
                raw = child.wait(timeout=args.timeout_seconds)
                code = raw if raw >= 0 else 128 - raw
        except subprocess.TimeoutExpired:
            timed_out, code = True, 124
        except KeyboardInterrupt:
            code = 128 + (caught['signal'] or signal.SIGINT)
        except OSError as exc:
            error = str(exc)
        finally:
            for s in previous:
                signal.signal(s, signal.SIG_IGN)
            if child is not None:
                terminate(child)
            for s, handler in previous.items():
                signal.signal(s, handler)
        raw = log.read_bytes()
        text = raw.decode(errors='replace')
        counts = {'run': len(re.findall(r'^=== RUN\s', text, re.M)),
                  'pass': len(re.findall(r'^--- PASS:', text, re.M)),
                  'fail': len(re.findall(r'^--- FAIL:', text, re.M)),
                  'skip': len(re.findall(r'^--- SKIP:', text, re.M))}
        counts = dict(counts, format='go_verbose_top_level') if any(counts.values()) else None
        try:
            head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True,
                                           stderr=subprocess.DEVNULL).strip()
            status = subprocess.check_output(['git', 'status', '--porcelain'], stderr=subprocess.DEVNULL)
            diff = subprocess.check_output(['git', 'diff', 'HEAD', '--binary'], stderr=subprocess.DEVNULL)
            dirty, diff_hash = bool(status), hashlib.sha256(diff).hexdigest()
        except subprocess.CalledProcessError:
            head, dirty, diff_hash = None, None, None
        data = {'schema_version': 1, 'id': args.id, 'argv': argv, 'cwd': str(Path.cwd()),
                'head': head, 'dirty': dirty, 'tracked_diff_sha256': diff_hash,
                'started_at': started, 'ended_at': time.time(), 'deadline_at': deadline,
                'exit_code': code, 'timed_out': timed_out,
                'error': error, 'test_counts': counts, 'log': log.name,
                'log_sha256': hashlib.sha256(raw).hexdigest(),
                'running_marker': marker.name,
                'acceptance': 'not_verified'}
        # The immutable receipt replaces the terminal state; the marker is
        # removed in the final cleanup below even if receipt writing fails.
        atomic(receipt, data)
        print(json.dumps({'receipt': str(receipt.resolve()), 'exit_code': code, 'timed_out': timed_out,
                          'test_counts': counts, 'acceptance': 'not_verified'}))
        return code
    finally:
        remove_running(marker)


if __name__ == '__main__':
    raise SystemExit(main())
