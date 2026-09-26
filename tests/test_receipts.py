"""Receipt helper tests: true exits, hashes, counts, bounds and failed evidence."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
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
            capture_output=True, text=True, timeout=60)

    def run_summary(self, *args):
        return subprocess.run([sys.executable, str(RUNTIME / "pi_summary.py"), *args],
                              capture_output=True, text=True, timeout=60)

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


if __name__ == "__main__":
    unittest.main()
