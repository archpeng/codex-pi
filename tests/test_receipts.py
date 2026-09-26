"""Receipt helper tests: true exits, hashes, counts, bounds and failed evidence."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from runtime_helpers import RUNTIME


def synth_receipt(directory: Path, check_id: str, exit_code: int, test_counts=None,
                  log_text: str = "--- FAIL: TestSynthetic\n") -> Path:
    log = directory / f"{check_id}-{uuid.uuid4().hex[:8]}.log"
    log.write_text(log_text, encoding="utf-8")
    receipt = directory / f"{check_id}-{uuid.uuid4().hex[:8]}.json"
    receipt.write_text(json.dumps({
        "schema_version": 1, "id": check_id, "argv": ["synthetic"], "cwd": str(directory),
        "head": None, "dirty": None, "tracked_diff_sha256": None, "started_at": 0,
        "ended_at": 0, "exit_code": exit_code, "timed_out": False, "error": None,
        "test_counts": test_counts, "log": log.name,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "acceptance": "not_verified",
    }), encoding="utf-8")
    return receipt


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-receipt-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.checks = self.tmp / "round.checks"
        self.checks.mkdir()

    def run_pi_check(self, check_id: str, *command: str):
        return subprocess.run(
            [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(self.checks),
             "--id", check_id, "--", *command],
            capture_output=True, text=True, timeout=60, cwd=str(self.tmp))

    def run_summary(self, *args):
        return subprocess.run([sys.executable, str(RUNTIME / "pi_summary.py"), *args],
                              capture_output=True, text=True, timeout=60)

    def start_pi_check(self, check_id: str, *command: str,
                       timeout_seconds: float | None = None):
        argv = [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(self.checks),
                "--id", check_id]
        if timeout_seconds is not None:
            argv += ["--timeout-seconds", str(timeout_seconds)]
        argv += ["--", *command]
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(self.tmp))

    def wait_for_marker(self, check_id: str, timeout: float = 15) -> Path:
        deadline = time.monotonic() + timeout
        while True:
            for candidate in sorted(self.checks.glob(f"{check_id}-*.running")):
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = None
                # The wrapper writes the marker before spawning and updates it
                # with the child pid; wait for the initialized marker identity.
                if isinstance(data, dict) and isinstance(data.get("pid"), int):
                    return candidate
            if time.monotonic() >= deadline:
                raise AssertionError(f"running marker for {check_id} never appeared")
            time.sleep(0.05)

    def test_pi_check_records_true_exit_hash_and_counts(self):
        proc = self.run_pi_check(
            "go-check", sys.executable, "-c",
            "print('=== RUN TestA'); print('--- SKIP: TestB'); print('--- PASS: TestA')")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(summary["acceptance"], "not_verified")
        self.assertEqual(summary["test_counts"]["pass"], 1)
        self.assertEqual(summary["test_counts"]["skip"], 1)
        receipt = json.loads(Path(summary["receipt"]).read_text())
        log = Path(summary["receipt"]).parent / receipt["log"]
        self.assertEqual(receipt["log_sha256"], hashlib.sha256(log.read_bytes()).hexdigest())
        self.assertFalse(receipt["dirty"])  # outside a git repo the helper records None/False-safe evidence

        failed = self.run_pi_check("failing", sys.executable, "-c", "import sys; sys.exit(4)")
        self.assertEqual(failed.returncode, 4)
        self.assertEqual(json.loads(failed.stdout)["exit_code"], 4)

    def test_summary_counts_every_failed_attempt_beyond_display_cap(self):
        self.run_pi_check("passing", sys.executable, "-c", "print('--- PASS: TestA')")
        self.run_pi_check("real-failure", sys.executable, "-c", "import sys; sys.exit(2)")
        for index in range(8):
            synth_receipt(self.checks, f"synthetic-{index}", 1)
        # Corrupt one log after hashing: it must surface as unverified evidence.
        corrupt = next(self.checks.glob("synthetic-0-*.json"))
        data = json.loads(corrupt.read_text())
        (self.checks / data["log"]).write_text("--- PASS: tampered\n", encoding="utf-8")

        round_log = self.tmp / "round.jsonl"
        round_log.write_text("", encoding="utf-8")
        payload = self.run_summary(str(round_log), "--worktree", str(self.tmp),
                                   "--run-dir", str(self.tmp), "--checks-dir", str(self.checks), "--json")
        self.assertEqual(payload.returncode, 0, payload.stderr)
        data = json.loads(payload.stdout)
        self.assertEqual(len(data["check_receipts"]), 10)
        totals = {}
        for check in data["check_receipts"]:
            for key, value in {"failed": (check["exit_code"] not in (0, None)),
                               "unverified_log": not check["log_verified"]}.items():
                totals[key] = totals.get(key, 0) + int(value)
        self.assertEqual(totals["failed"], 9)
        self.assertGreaterEqual(totals["unverified_log"], 1)
        self.assertEqual(len(data["check_receipts"]), 10)

        overview = self.run_summary(str(round_log), "--worktree", str(self.tmp),
                                    "--run-dir", str(self.tmp), "--checks-dir", str(self.checks))
        text = overview.stdout
        self.assertIn("receipt_attempts: total=10", text)
        self.assertIn("failed=9", text)
        self.assertIn("All attempts counted", text)
        self.assertIn("acceptance=not_verified", text)
        shown = [line for line in text.splitlines() if line.startswith("  ") and "exit=" in line]
        self.assertLessEqual(len(shown), 8)

    def test_reasoning_usage_is_not_added_twice(self):
        round_log = self.tmp / "round.jsonl"
        round_log.write_text(json.dumps({
            "type": "message_end", "message": {
                "role": "assistant", "provider": "deepseek", "model": "deepseek-flash",
                "stopReason": "stop",
                "usage": {"input": 10, "cacheRead": 0, "cacheWrite": 0, "output": 5,
                          "totalTokens": 15, "reasoning": 50},
                "content": [{"type": "text", "text": "x"}],
            },
        }) + "\n", encoding="utf-8")
        payload = self.run_summary(str(round_log), "--worktree", str(self.tmp),
                                   "--run-dir", str(self.tmp), "--checks-dir", str(self.tmp / "absent"),
                                   "--json")
        data = json.loads(payload.stdout)
        self.assertEqual(data["usage"], {"input": 10, "cacheRead": 0, "cacheWrite": 0,
                                         "output": 5, "totalTokens": 15})
        self.assertNotIn("reasoning", data["usage"])

    def test_missing_receipts_are_reported_as_absence(self):
        round_log = self.tmp / "round.jsonl"
        round_log.write_text("", encoding="utf-8")
        payload = self.run_summary(str(round_log), "--worktree", str(self.tmp),
                                   "--run-dir", str(self.tmp), "--checks-dir", str(self.tmp / "absent"),
                                   "--json")
        self.assertEqual(payload.returncode, 0, payload.stderr)
        data = json.loads(payload.stdout)
        self.assertEqual(data["check_receipts"], [])
        overview = self.run_summary(str(round_log), "--worktree", str(self.tmp),
                                    "--run-dir", str(self.tmp), "--checks-dir", str(self.tmp / "absent"))
        self.assertIn("total=0", overview.stdout)
        self.assertIn("Absence is missing evidence", overview.stdout)

    def test_running_marker_is_visible_before_finish_and_removed_after_success(self):
        proc = self.start_pi_check(
            "brief", sys.executable, "-c",
            "import time; print('check started', flush=True); time.sleep(2)")
        marker = self.wait_for_marker("brief")
        data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(data["id"], "brief")
        self.assertIsInstance(data["pid"], int)
        self.assertGreater(data["deadline_at"], data["started_at"])
        self.assertEqual(data["deadline_scope"],
                         "wrapper timeout only; never an inner command deadline")
        self.assertEqual(data["log"], marker.name.replace(".running", ".log"))
        self.assertTrue((self.checks / data["log"]).is_file(),
                        "the marker must be available while the child is still running")
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertTrue(stdout)
        self.assertFalse(marker.exists(), "a completed receipt must remove the running marker")
        receipt = json.loads(next(self.checks.glob("brief-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["running_marker"], marker.name)
        self.assertEqual(receipt["exit_code"], 0)

    def test_running_marker_removed_after_failure_timeout_and_cancel(self):
        failed = self.start_pi_check("failfast", sys.executable, "-c", "import sys; sys.exit(7)")
        self.assertEqual(failed.wait(timeout=30), 7)
        failed.communicate(timeout=10)
        self.assertEqual(list(self.checks.glob("failfast-*.running")), [])
        receipt = json.loads(next(self.checks.glob("failfast-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["exit_code"], 7)
        self.assertFalse(receipt["timed_out"])

        timed = self.start_pi_check("wraptimeout", sys.executable, "-c",
                                    "import time; time.sleep(30)", timeout_seconds=1)
        marker = self.wait_for_marker("wraptimeout")
        self.assertTrue(marker.exists())
        self.assertEqual(timed.wait(timeout=30), 124)
        timed.communicate(timeout=10)
        self.assertEqual(list(self.checks.glob("wraptimeout-*.running")), [])
        receipt = json.loads(next(self.checks.glob("wraptimeout-*.json")).read_text(encoding="utf-8"))
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(receipt["exit_code"], 124)

        cancelled = self.start_pi_check("wrappedcancel", sys.executable, "-c",
                                        "import time; time.sleep(30)")
        self.wait_for_marker("wrappedcancel")
        cancelled.terminate()
        self.assertEqual(cancelled.wait(timeout=30), 143)
        cancelled.communicate(timeout=10)
        self.assertEqual(list(self.checks.glob("wrappedcancel-*.running")), [])
        receipt = json.loads(next(self.checks.glob("wrappedcancel-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["exit_code"], 143)
        self.assertFalse(receipt["timed_out"])

    def test_inner_failure_stays_nonzero_without_wrapper_timeout(self):
        proc = self.run_pi_check(
            "inner-timeout", sys.executable, "-c",
            "print('panic: test timed out after 10m0s'); raise SystemExit(1)")
        self.assertEqual(proc.returncode, 1)
        receipt = json.loads(Path(json.loads(proc.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["exit_code"], 1)
        self.assertFalse(receipt["timed_out"])
        self.assertIsNone(receipt["error"])

    def test_python_unittest_counts_positive_failure_and_skip(self):
        (self.tmp / "sample_ok.py").write_text(
            "import unittest\n\nclass Sample(unittest.TestCase):\n"
            "    def test_one(self):\n        self.assertTrue(True)\n"
            "    def test_two(self):\n        self.assertEqual(2, 2)\n",
            encoding="utf-8")
        ok = self.run_pi_check("py-ok", sys.executable, "-m", "unittest", "-v", "sample_ok")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        receipt = json.loads(Path(json.loads(ok.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["test_counts"],
                         {"run": 2, "pass": 2, "fail": 0, "skip": 0,
                          "format": "python_unittest_summary"})

        (self.tmp / "sample_bad.py").write_text(
            "import unittest\n\nclass Sample(unittest.TestCase):\n"
            "    def test_fail(self):\n        self.assertEqual(1, 2)\n"
            "    def test_skip(self):\n        self.skipTest('later')\n",
            encoding="utf-8")
        bad = self.run_pi_check("py-bad", sys.executable, "-m", "unittest", "-v", "sample_bad")
        self.assertNotEqual(bad.returncode, 0)
        receipt = json.loads(Path(json.loads(bad.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["test_counts"]["run"], 2)
        self.assertEqual(receipt["test_counts"]["fail"], 1)
        self.assertEqual(receipt["test_counts"]["skip"], 1)
        self.assertEqual(receipt["test_counts"]["format"], "python_unittest_summary")

    def test_python_unittest_zero_and_ambiguous_summaries_stay_explicit(self):
        zero = self.run_pi_check(
            "py-zero", sys.executable, "-c",
            "print('Ran 0 tests in 0.000s'); print(); print('OK')")
        self.assertEqual(zero.returncode, 0, zero.stderr)
        receipt = json.loads(Path(json.loads(zero.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["test_counts"]["run"], 0)
        self.assertEqual(receipt["test_counts"]["fail"], 0)
        self.assertEqual(receipt["test_counts"]["skip"], 0)

        ambiguous = self.run_pi_check(
            "py-ambiguous", sys.executable, "-c",
            "print('Ran 2 tests in 0.010s')")
        self.assertEqual(ambiguous.returncode, 0, ambiguous.stderr)
        receipt = json.loads(Path(json.loads(ambiguous.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertIsNone(receipt["test_counts"])

        malformed = self.run_pi_check(
            "py-malformed", sys.executable, "-c",
            "print('Ran 2 tests in 0.010s'); print(); print('OK (skipped=oops)')")
        self.assertEqual(malformed.returncode, 0, malformed.stderr)
        receipt = json.loads(Path(json.loads(malformed.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertIsNone(receipt["test_counts"],
                          "a malformed detail field must not become skip=0")

        dangling = self.run_pi_check(
            "py-dangling", sys.executable, "-c",
            "print('Ran 2 tests in 0.010s'); print(); print('OK'); print(); "
            "print('Ran 0 tests in 0.010s')")
        self.assertEqual(dangling.returncode, 0, dangling.stderr)
        receipt = json.loads(Path(json.loads(dangling.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertIsNone(receipt["test_counts"],
                          "an incomplete final summary must not reuse an earlier run count")

        forward = self.run_pi_check(
            "py-forward", sys.executable, "-c",
            "print('Ran 2 tests in 0.010s'); print(); print('OK (expected failures=1)')")
        self.assertEqual(forward.returncode, 0, forward.stderr)
        receipt = json.loads(Path(json.loads(forward.stdout)["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["test_counts"]["run"], 2)
        self.assertEqual(receipt["test_counts"]["skip"], 0)

        contradictory = [
            ("FAILED", "print('Ran 2 tests in 0.010s'); print(); print('FAILED')"),
            ("dup-skip", "print('Ran 2 tests in 0.010s'); print(); "
                         "print('OK (skipped=1, skipped=0)')"),
            ("ok-failures", "print('Ran 2 tests in 0.010s'); print(); "
                            "print('OK (failures=1)')"),
            ("failed-zero", "print('Ran 2 tests in 0.010s'); print(); "
                            "print('FAILED (failures=0, errors=0)')"),
        ]
        for label, script in contradictory:
            proc = self.run_pi_check(f"py-{label}", sys.executable, "-c", script)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(Path(json.loads(proc.stdout)["receipt"]).read_text(encoding="utf-8"))
            self.assertIsNone(receipt["test_counts"], label)


if __name__ == "__main__":
    unittest.main()
