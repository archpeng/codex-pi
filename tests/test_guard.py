"""Behavioral tests for the bounded no-follow resource guard in pi_check.py.

All tests use tiny limits and fast subprocess doubles; no GB allocation and no
minute-long waits. The guard is local detection only: these tests never claim
that app delivery, scheduling or business checks passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from runtime_helpers import RUNTIME

sys.path.insert(0, str(RUNTIME))
import pi_size  # noqa: E402
from pi_size import measure  # noqa: E402


class GuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-guard-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.checks = self.tmp / "round.checks"
        self.checks.mkdir()
        self.watch = self.tmp / "watch"
        self.watch.mkdir()

    def run_check(self, *args, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(self.checks),
             "--id", "guard-check", *[str(arg) for arg in args]],
            capture_output=True, text=True, timeout=timeout, cwd=str(self.tmp))

    def start_check(self, *args) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(self.checks),
             "--id", "guard-check", *[str(arg) for arg in args]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(self.tmp))

    def wait_marker(self, timeout: float = 15) -> Path:
        deadline = time.monotonic() + timeout
        while True:
            found = sorted(self.checks.glob("guard-check-*.running"))
            if found:
                try:
                    data = json.loads(found[0].read_text(encoding="utf-8"))
                except ValueError:
                    data = None
                if isinstance(data, dict) and isinstance(data.get("pid"), int):
                    return found[0]
            if time.monotonic() >= deadline:
                raise AssertionError("running marker with a child pid never appeared")
            time.sleep(0.05)

    def receipt_from(self, proc: subprocess.CompletedProcess) -> dict:
        summary = json.loads(proc.stdout)
        return json.loads(Path(summary["receipt"]).read_text(encoding="utf-8"))

    @staticmethod
    def alive(pid) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except (ProcessLookupError, ValueError, TypeError):
            return False
        except PermissionError:
            return True

    # ------------------------------------------------------------------
    def test_below_budget_quiet_child_succeeds_with_guard_snapshot(self):
        (self.watch / "data.bin").write_bytes(b"x" * 2048)
        child_marker = self.tmp / "child-ran"
        proc = self.run_check(
            "--watch-path", self.watch, "--max-bytes", "1000000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c",
            f"import time; open({str(child_marker)!r}, 'w').write('yes'); time.sleep(0.5)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = self.receipt_from(proc)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertFalse(receipt["timed_out"])
        self.assertFalse(receipt["cancelled"])
        guard = receipt["resource_limit"]
        self.assertEqual(receipt["resourceLimit"], guard, "camelCase guard alias must match")
        self.assertFalse(guard["breached"])
        self.assertFalse(guard["unknown"])
        self.assertGreaterEqual(guard["observed_bytes"], 2048)
        self.assertEqual(guard["max_bytes"], 1000000)
        self.assertEqual(guard["path"], str(self.watch))
        self.assertGreaterEqual(guard["scans"], 2)
        self.assertTrue(child_marker.is_file(), "a below-budget quiet child must run to completion")
        self.assertEqual(receipt["acceptance"], "not_verified")

    def test_preexisting_over_budget_stops_before_spawn(self):
        (self.watch / "existing.bin").write_bytes(b"x" * 5000)
        child_marker = self.tmp / "child-ran"
        proc = self.run_check(
            "--watch-path", self.watch, "--max-bytes", "1000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", f"open({str(child_marker)!r}, 'w').write('yes')")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.returncode, 75, proc.stderr)
        receipt = self.receipt_from(proc)
        self.assertFalse(child_marker.exists(), "a pre-existing breach must not spawn the check child")
        self.assertFalse(receipt["timed_out"])
        guard = receipt["resource_limit"]
        self.assertTrue(guard["breached"])
        self.assertTrue(guard["stopped_child"])
        self.assertGreater(guard["observed_bytes"], 1000)
        self.assertEqual(guard["path"], str(self.watch))
        self.assertIn("max_bytes 1000", guard["reason"])
        log = Path(receipt["log"])
        if not log.is_absolute():
            log = self.checks / receipt["log"]
        self.assertTrue(log.is_file(), "an immutable receipt keeps a retained (empty) log")
        self.assertEqual(receipt["log_sha256"], hashlib.sha256(log.read_bytes()).hexdigest())

    def test_breach_while_child_is_quiet_stops_only_owned_child_and_retains_log(self):
        (self.watch / "seed.bin").write_bytes(b"s" * 100)
        proc = self.start_check(
            "--watch-path", self.watch, "--max-bytes", "10000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c",
            "import time; print('CHECK-START', flush=True); time.sleep(60)")
        marker = self.wait_marker()
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
        child_pid = marker_data["pid"]
        self.assertTrue(self.alive(child_pid))
        (self.watch / "grown.bin").write_bytes(b"z" * 50000)
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 75, stderr)
        summary = json.loads(stdout)
        receipt = json.loads(Path(summary["receipt"]).read_text(encoding="utf-8"))
        guard = receipt["resource_limit"]
        self.assertTrue(guard["breached"])
        self.assertTrue(guard["stopped_child"])
        self.assertGreater(guard["observed_bytes"], 10000)
        self.assertFalse(receipt["timed_out"])
        self.assertEqual(list(self.checks.glob("guard-check-*.running")), [])
        log = self.checks / receipt["log"]
        self.assertIn("CHECK-START", log.read_text(encoding="utf-8"),
                      "the full child log must be retained after a guard stop")
        self.assertFalse(self.alive(child_pid), "the owned check process group must be stopped")

    def test_unknown_measurement_is_not_treated_as_under_budget(self):
        missing = self.tmp / "does-not-exist"
        proc = self.run_check(
            "--watch-path", missing, "--max-bytes", "10",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", "import sys; sys.exit(7)")
        self.assertEqual(proc.returncode, 7, proc.stderr)
        receipt = self.receipt_from(proc)
        guard = receipt["resource_limit"]
        self.assertTrue(guard["unknown"])
        self.assertFalse(guard["breached"], "unknown must never be reported as a breach")
        self.assertFalse(receipt["timed_out"])

        link = self.tmp / "watch-link"
        os.symlink(self.watch, link)
        (self.watch / "content.bin").write_bytes(b"x" * 4096)
        proc = self.run_check(
            "--watch-path", link, "--max-bytes", "16",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", "import sys; sys.exit(9)")
        self.assertEqual(proc.returncode, 9, proc.stderr)
        guard = self.receipt_from(proc)["resource_limit"]
        self.assertTrue(guard["unknown"], "a symlink watch root is never followed")
        self.assertFalse(guard["breached"])

    def test_bounded_walk_reports_unknown_and_skips_symlinks(self):
        external = self.tmp / "external"
        external.mkdir()
        (external / "big.bin").write_bytes(b"E" * 100000)
        (self.watch / "small.bin").write_bytes(b"s" * 10)
        os.symlink(external, self.watch / "external-link")
        measured = measure(self.watch)
        self.assertTrue(measured["complete"])
        self.assertFalse(measured["unknown"])
        self.assertEqual(measured["bytes"], 10, "symlink targets are never counted or followed")
        self.assertEqual(measured["symlinksSkipped"], 1)

        for index in range(6):
            (self.watch / f"extra-{index}.bin").write_bytes(b"x")
        bounded = measure(self.watch, max_entries=3)
        self.assertFalse(bounded["complete"])
        self.assertTrue(bounded["unknown"])
        self.assertEqual(bounded["boundedBy"], "entries")
        self.assertLessEqual(bounded["bytes"], measured["bytes"] + 6)

        timed = measure(self.watch, max_entries=100000, max_seconds=0.0)
        self.assertTrue(timed["unknown"], "a zero time bound cannot be complete")
        invalid = measure(self.watch, max_entries=0)
        self.assertTrue(invalid["unknown"])

    def test_unreadable_and_failing_iteration_are_bounded_unknown(self):
        if os.geteuid() != 0:
            parent = self.tmp / "unreadable-tree"
            (parent / "locked").mkdir(parents=True)
            (parent / "locked" / "hidden.bin").write_bytes(b"x" * 10)
            os.chmod(parent / "locked", 0o000)
            self.addCleanup(os.chmod, parent / "locked", 0o700)
            result = measure(parent)
            self.assertTrue(result["unknown"], "an unreadable directory is unknown, not under budget")
            self.assertFalse(result["complete"])
            direct = measure(parent / "locked")
            self.assertTrue(direct["unknown"])

        class BrokenScan:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                raise OSError("simulated readdir failure")

        original = pi_size.os.scandir
        pi_size.os.scandir = lambda _path: BrokenScan()
        try:
            failed = measure(self.watch)
        finally:
            pi_size.os.scandir = original
        self.assertTrue(failed["unknown"])
        self.assertIn("unreadable directory entry", failed["reason"])

    def test_guard_unreadable_subtree_is_unknown_and_child_survives(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        locked = self.watch / "locked"
        locked.mkdir()
        (locked / "hidden.bin").write_bytes(b"x" * 5000)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        proc = self.run_check(
            "--watch-path", self.watch, "--max-bytes", "10",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", "import sys; sys.exit(6)")
        self.assertEqual(proc.returncode, 6, proc.stderr)
        receipt = self.receipt_from(proc)
        guard = receipt["resource_limit"]
        self.assertTrue(guard["unknown"])
        self.assertFalse(guard["breached"])
        self.assertFalse(receipt["timed_out"])
        self.assertFalse(receipt["cancelled"])

    def test_partial_lower_bound_over_budget_breaches_and_stops_child(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        locked = self.watch / "locked"
        locked.mkdir()
        (locked / "hidden.bin").write_bytes(b"y" * 10)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        proc = self.start_check(
            "--watch-path", self.watch, "--max-bytes", "1000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c",
            "import time; print('START', flush=True); time.sleep(60)")
        marker = self.wait_marker()
        pid = json.loads(marker.read_text(encoding="utf-8"))["pid"]
        (self.watch / "big.bin").write_bytes(b"x" * 50000)
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 75, stderr)
        receipt = json.loads(Path(json.loads(stdout)["receipt"]).read_text(encoding="utf-8"))
        guard = receipt["resource_limit"]
        self.assertTrue(guard["breached"])
        self.assertEqual(guard["breach_basis"], "partial lower bound")
        self.assertTrue(guard["unknown"], "the full size stays unknown")
        self.assertGreaterEqual(guard["observed_bytes"], 50000)
        self.assertTrue(guard["stopped_child"])
        self.assertIn("partial lower bound", guard["reason"])
        log = self.checks / receipt["log"]
        self.assertIn("START", log.read_text(encoding="utf-8"))
        self.assertFalse(self.alive(pid), "a partial lower-bound breach must stop the owned child")

    def test_preflight_partial_over_budget_stops_before_spawn(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        (self.watch / "big.bin").write_bytes(b"x" * 50000)
        locked = self.watch / "locked"
        locked.mkdir()
        (locked / "hidden.bin").write_bytes(b"y" * 10)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        child_marker = self.tmp / "child-ran"
        proc = self.run_check(
            "--watch-path", self.watch, "--max-bytes", "1000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", f"open({str(child_marker)!r}, 'w').write('ran')")
        self.assertEqual(proc.returncode, 75, proc.stderr)
        receipt = self.receipt_from(proc)
        guard = receipt["resource_limit"]
        self.assertTrue(guard["breached"])
        self.assertEqual(guard["breach_basis"], "partial lower bound")
        self.assertFalse(child_marker.exists(), "a proven partial breach must not spawn a child")

    def test_guard_timeout_and_cancel_stay_distinct(self):
        (self.watch / "small.bin").write_bytes(b"x" * 10)
        proc = self.run_check(
            "--timeout-seconds", "1", "--watch-path", self.watch, "--max-bytes", "1000000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", "import time; time.sleep(30)")
        self.assertEqual(proc.returncode, 124, proc.stderr)
        receipt = self.receipt_from(proc)
        self.assertTrue(receipt["timed_out"])
        self.assertFalse(receipt["cancelled"])
        self.assertFalse(receipt["resource_limit"]["breached"])

        running = self.start_check(
            "--watch-path", self.watch, "--max-bytes", "1000000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", "import time; time.sleep(60)")
        marker = self.wait_marker()
        pid = json.loads(marker.read_text(encoding="utf-8"))["pid"]
        running.terminate()
        stdout, stderr = running.communicate(timeout=30)
        self.assertEqual(running.returncode, 143, stderr)
        receipt = json.loads(
            Path(json.loads(stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertTrue(receipt["cancelled"])
        self.assertEqual(receipt["signal"], 15)
        self.assertFalse(receipt["timed_out"])
        self.assertFalse(receipt["resource_limit"]["breached"])
        self.assertEqual(list(self.checks.glob("guard-check-*.running")), [])
        self.assertFalse(self.alive(pid), "interruption must clean up the owned process group")

    def test_running_marker_exposes_guard_snapshot(self):
        proc = self.start_check(
            "--watch-path", self.watch, "--max-bytes", "1000000",
            "--health-interval-seconds", "0.2", "--",
            sys.executable, "-c", "import time; time.sleep(60)")
        marker = self.wait_marker()
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            guard = data["resource_limit"]
            self.assertEqual(guard["path"], str(self.watch))
            self.assertEqual(guard["max_bytes"], 1000000)
            self.assertFalse(guard["breached"])
            self.assertIn("symlinks are never followed", guard["scope"])
            self.assertIn("unknown", guard["note"])
        finally:
            proc.terminate()
            proc.communicate(timeout=30)

    def test_guard_options_must_be_provided_together(self):
        base = [sys.executable, "-c", "pass"]
        missing_budget = self.run_check("--watch-path", self.watch, "--", *base)
        self.assertEqual(missing_budget.returncode, 2)
        self.assertIn("provided together", missing_budget.stderr)
        missing_path = self.run_check("--max-bytes", "5", "--", *base)
        self.assertEqual(missing_path.returncode, 2)
        self.assertIn("provided together", missing_path.stderr)
        zero = self.run_check("--watch-path", self.watch, "--max-bytes", "0", "--", *base)
        self.assertEqual(zero.returncode, 2)
        for interval in ("0", "-1", "nan", "inf", "4000"):
            proc = self.run_check("--watch-path", self.watch, "--max-bytes", "5",
                                  "--health-interval-seconds", interval, "--", *base)
            self.assertEqual(proc.returncode, 2, f"interval {interval!r} must be rejected\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
