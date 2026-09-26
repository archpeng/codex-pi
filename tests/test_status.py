"""Bounded read-only ``pi_task.py status`` and bounded ``wait`` tests.

The status command must never summarize, scan a full transcript or mutate
evidence, and must treat quiet logs, live PIDs and stale summaries as uncertain
rather than as progress or failure.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from runtime_helpers import (RUNTIME, Repo, base_env, cleanup_repos, cli_json,
                             default_config, pid_alive, run_cli, snapshot_files)


class StatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-status-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        cleanup_repos()

    def make(self, config: dict | None = None, name: str = "repo"):
        repo = Repo(self.tmp, name=name, config=config if config is not None else default_config())
        worktree = repo.worktree("wt")
        return repo, worktree

    def status(self, repo, task: str, round_number: int | None = None, env: dict | None = None) -> dict:
        args = ["status", "--repo", str(repo.root), "--task", task]
        if round_number is not None:
            args += ["--round", str(round_number)]
        return cli_json(*args, env=env or base_env())

    def wait_for(self, directory: Path, pattern: str, timeout: float = 15) -> Path:
        deadline = time.monotonic() + timeout
        while True:
            found = sorted(directory.glob(pattern))
            if found:
                return found[0]
            if time.monotonic() >= deadline:
                raise AssertionError(f"{pattern} never appeared under {directory}")
            time.sleep(0.05)

    def start_check(self, checks: Path, check_id: str, *command: str,
                    timeout_seconds: float | None = None) -> subprocess.Popen:
        argv = [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(checks),
                "--id", check_id]
        if timeout_seconds is not None:
            argv += ["--timeout-seconds", str(timeout_seconds)]
        argv += ["--", *command]
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(self.tmp))

    # ------------------------------------------------------------------
    def test_quiet_running_task_is_uncertain_not_failed(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("quiet", worktree, env=env)
        repo.wait_round_state("quiet", "running")
        try:
            data = self.status(repo, "quiet", env=env)
            self.assertEqual(data["state"], "running")
            self.assertEqual(data["recordedState"], "running")
            self.assertTrue(data["ownership"]["activeWorker"])
            self.assertTrue(data["ownership"]["supervisorAlive"])
            self.assertEqual(data["acceptance"], "not_verified")
            self.assertEqual(data["model"], "deepseek/deepseek-flash")
            self.assertEqual(data["session"]["sessionId"], "quiet")
            self.assertEqual(data["round"], 1)
            self.assertGreater(data["elapsedMs"], 0)
            self.assertGreater(data["deadlineAt"], data["startedAt"])
            self.assertNotIn("summary", data)
            self.assertNotIn("summaryText", data)
            self.assertIsNone(data["checks"]["running"])
            self.assertIsNone(data["checks"]["legacyCandidate"])
            notes = " ".join(data["notes"])
            self.assertIn("never progress or failure", notes)
            self.assertIn("read-only snapshot", notes)
            self.assertTrue(data["evidence"]["state"].endswith("round.state.json"))
        finally:
            repo.cancel("quiet", env=env)
            self.assertEqual(repo.wait_terminal("quiet", env=env, timeout=25)["state"], "cancelled")

    def test_status_reports_running_check_marker_and_bounded_tail(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("checking", worktree, env=env)
        repo.wait_round_state("checking", "running")
        checks = repo.task_dir("checking") / "rounds" / "1" / "round.checks"
        proc = self.start_check(checks, "live-check", sys.executable, "-c",
                                "import time; print('CHECK-LINE', flush=True); time.sleep(60)")
        try:
            marker = self.wait_for(checks, "live-check-*.running")
            log = checks / marker.name.replace(".running", ".log")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (not log.is_file() or "CHECK-LINE" not in log.read_text()):
                time.sleep(0.05)
            data = self.status(repo, "checking", env=env)
            running = data["checks"]["running"]
            self.assertIsNotNone(running, data)
            self.assertEqual(running["id"], "live-check")
            self.assertIsInstance(running["pid"], int)
            self.assertTrue(running["pidAlive"])
            self.assertTrue(running["uncertain"])
            self.assertGreater(running["deadlineAt"], running["startedAt"])
            self.assertEqual(running["deadlineScope"],
                             "wrapper timeout only; never an inner command deadline")
            self.assertGreater(running["logEvidence"]["bytes"], 0)
            self.assertIn("CHECK-LINE", "\n".join(running["logEvidence"]["tail"]))
        finally:
            proc.terminate()
            proc.communicate(timeout=30)
            repo.cancel("checking", env=env)
            self.assertEqual(repo.wait_terminal("checking", env=env, timeout=25)["state"], "cancelled")
        self.assertEqual(list(checks.glob("live-check-*.running")), [],
                         "a finished wrapper removes its running marker")
        receipt = json.loads(next(checks.glob("live-check-*.json")).read_text())
        self.assertEqual(receipt["running_marker"], marker.name)
        data = self.status(repo, "checking", env=env)
        self.assertEqual(data["checks"]["receipts"]["scanned"], 1)
        self.assertEqual(data["checks"]["receipts"]["latest"]["id"], "live-check")
        self.assertIsNone(data["checks"]["legacyCandidate"],
                          "a real pi_check receipt must cover its own log")

    def test_status_legacy_unreceipted_log_is_uncertain_and_bounded(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("legacy", worktree, env=env)
        repo.wait_round_state("legacy", "running")
        checks = repo.task_dir("legacy") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        big = checks / "legacy-run-0001.log"
        with big.open("w", encoding="utf-8") as stream:
            for index in range(40000):
                stream.write(f"line {index:06d} " + "x" * 40 + "\n")
        (checks / "corrupt-0002.running").write_text("{not json", encoding="utf-8")
        (checks / "unrelated.json").write_text("{broken", encoding="utf-8")
        try:
            started = time.monotonic()
            data = self.status(repo, "legacy", env=env)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 8, "status must bound directory and log reads")
            candidate = data["checks"]["legacyCandidate"]
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["log"], "legacy-run-0001.log")
            self.assertTrue(candidate["uncertain"])
            self.assertIn("not proof", candidate["note"])
            self.assertEqual(candidate["logEvidence"]["bytes"], big.stat().st_size)
            self.assertTrue(candidate["logEvidence"]["truncated"])
            self.assertLessEqual(len(candidate["logEvidence"]["tail"]), 20)
            self.assertLessEqual(sum(len(line) for line in candidate["logEvidence"]["tail"]), 8192)
            self.assertIsNone(data["checks"]["running"])
            ignored = {item["name"] for item in data["checks"]["ignored"]}
            self.assertIn("corrupt-0002.running", ignored)
            self.assertIn("unrelated.json", ignored)
            self.assertNotIn("summary", data)
            self.assertLess(len(json.dumps(data)), 60000)
        finally:
            repo.cancel("legacy", env=env)
            self.assertEqual(repo.wait_terminal("legacy", env=env, timeout=25)["state"], "cancelled")

    def test_status_counts_failed_attempts_and_never_leaks_argv(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("receipts", worktree, env=env)
        repo.wait_round_state("receipts", "running")
        checks = repo.task_dir("receipts") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        for index, (code, timed_out) in enumerate(((0, False), (5, False), (None, True))):
            log = checks / f"attempt-{index}-000000000000.log"
            log.write_text(f"attempt {index}\n", encoding="utf-8")
            receipt = checks / f"attempt-{index}-000000000000.json"
            receipt.write_text(json.dumps({
                "schema_version": 1, "id": f"attempt-{index}",
                "argv": ["--secret-argument", "TOPSECRET-ARGV"],
                "log_sha256": "a" * 64,
                "exit_code": code, "timed_out": timed_out, "started_at": 100 + index,
                "ended_at": 200 + index,
                "test_counts": {"run": 1, "fail": 1 if code else 0,
                                 "evil-counter": 999, "nested": {"big": "x" * 50000}},
                "log": log.name}), encoding="utf-8")
        try:
            data = self.status(repo, "receipts", env=env)
            receipts = data["checks"]["receipts"]
            self.assertEqual(receipts["total"], 3)
            self.assertEqual(receipts["scanned"], 3)
            self.assertEqual(receipts["failedAttempts"], 2)
            self.assertIn(receipts["latest"]["id"], {"attempt-0", "attempt-1", "attempt-2"})
            latest_failed = receipts["latestFailed"]
            self.assertTrue(latest_failed["failed"])
            self.assertIn(latest_failed["exitCode"], (5, None))
            self.assertIsNotNone(receipts["latestSuccessful"])
            rendered = json.dumps(data)
            self.assertNotIn("TOPSECRET-ARGV", rendered, "status must never return full argv")
            self.assertNotIn("secret-argument", rendered)
            self.assertNotIn("evil-counter", rendered)
            self.assertNotIn("nested", rendered)
        finally:
            repo.cancel("receipts", env=env)
            self.assertEqual(repo.wait_terminal("receipts", env=env, timeout=25)["state"], "cancelled")

    def test_status_ignores_stale_summary_and_never_mutates_evidence(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("stale", worktree, env=env)
        self.assertEqual(repo.wait_terminal("stale", env=env)["state"], "completed")
        self.assertTrue((repo.task_dir("stale") / "rounds" / "1" / "round.summary.json").is_file())
        env_hang = base_env(PI_DOUBLE_MODE="hang")
        repo.continue_task("stale", env=env_hang)
        repo.wait_round_state("stale", "running")
        round_log = repo.task_dir("stale") / "rounds" / "2" / "round.jsonl"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not round_log.read_text(
                encoding="utf-8", errors="replace").strip():
            time.sleep(0.05)
        stale_summary = repo.task_dir("stale") / "rounds" / "2" / "round.summary.json"
        stale_summary.write_text(json.dumps({"source": "stale sum", "process_exit": "0",
                                             "final_text": "STALE-CURRENT-CLAIM"}), encoding="utf-8")
        try:
            before = snapshot_files(repo.task_dir("stale"))
            data = self.status(repo, "stale", env=env_hang)
            after = snapshot_files(repo.task_dir("stale"))
            self.assertEqual(before, after, "status must not mutate task evidence")
            self.assertEqual(data["round"], 2)
            self.assertEqual(data["recordedState"], "running")
            rendered = json.dumps(data)
            self.assertNotIn("STALE-CURRENT-CLAIM", rendered)
            self.assertNotIn("process_exit", rendered)
            self.assertNotIn("summary", data)
        finally:
            repo.cancel("stale", env=env_hang)
            self.assertEqual(repo.wait_terminal("stale", env=env_hang, timeout=25)["state"], "cancelled")

    def test_status_rejects_unknown_task_and_round(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("known", worktree, env=env)
        self.assertEqual(repo.wait_terminal("known", env=env)["state"], "completed")
        missing = run_cli("status", "--repo", str(repo.root), "--task", "missing",
                          env=env, expect=2)
        self.assertIn("unknown task", missing.stderr)
        unknown_round = run_cli("status", "--repo", str(repo.root), "--task", "known",
                                "--round", "9", env=env, expect=2)
        self.assertIn("does not exist", unknown_round.stderr)

    def test_status_bounds_corrupt_and_large_receipt_metadata(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("huge", worktree, env=env)
        repo.wait_round_state("huge", "running")
        checks = repo.task_dir("huge") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        huge_payload = {
            "schema_version": 1, "id": "a" * 500000, "log": "b" * 500000,
            "log_sha256": "c" * 64, "exit_code": 0, "timed_out": False,
            "started_at": 1, "ended_at": 2,
            "test_counts": {f"key-{index}": "x" * 1000 for index in range(500)},
            "argv": ["TOPSECRET-ARGV"],
        }
        for index in range(2):
            (checks / f"bogus-{index}-000000000000.json").write_text(
                json.dumps(huge_payload), encoding="utf-8")
        (checks / "oversize-0-000000000000.json").write_bytes(
            b'{"schema_version": 1, "padding": "' + b"x" * 3_000_000 + b'"}')
        valid_log = checks / "good-0-000000000000.log"
        valid_log.write_text("ok\n", encoding="utf-8")
        (checks / "good-0-000000000000.json").write_text(json.dumps({
            "schema_version": 1, "id": "good", "log": valid_log.name,
            "log_sha256": "a" * 64, "exit_code": 0, "timed_out": False,
            "started_at": 1, "ended_at": 2,
            "test_counts": {"run": 1, "pass": 1, "fail": 0, "skip": 0,
                            "format": "go_verbose_top_level",
                            "evil-counter": 999, "nested": {"big": "x" * 100000}},
            "argv": ["TOPSECRET-ARGV"]}), encoding="utf-8")
        try:
            started = time.monotonic()
            data = self.status(repo, "huge", env=env)
            self.assertLess(time.monotonic() - started, 10)
            rendered = json.dumps(data)
            self.assertLess(len(rendered), 100000, "hostile metadata must not inflate status output")
            self.assertNotIn("TOPSECRET-ARGV", rendered)
            self.assertNotIn("evil-counter", rendered)
            receipts = data["checks"]["receipts"]
            self.assertEqual(receipts["total"], 4)
            self.assertEqual(receipts["scanned"], 1, "hostile receipts must not be accepted")
            self.assertGreaterEqual(data["checks"]["ignoredCount"], 3)
            self.assertEqual(receipts["latest"]["testCounts"],
                             {"run": 1, "pass": 1, "fail": 0, "skip": 0,
                              "format": "go_verbose_top_level"})
            reasons = [item["reason"] for item in data["checks"]["ignored"]]
            self.assertTrue(any(reason.startswith("oversized") for reason in reasons), reasons)
            self.assertTrue(any("unsafe" in reason or "unrecognized" in reason for reason in reasons), reasons)
        finally:
            repo.cancel("huge", env=env)
            self.assertEqual(repo.wait_terminal("huge", env=env, timeout=25)["state"], "cancelled")

    def test_status_receipt_precedence_and_uncertain_log_candidate(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("precedence", worktree, env=env)
        repo.wait_round_state("precedence", "running")
        checks = repo.task_dir("precedence") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        log_a = checks / "finished-0-000000000000.log"
        log_a.write_text("done\n", encoding="utf-8")
        (checks / "finished-0-000000000000.json").write_text(json.dumps({
            "schema_version": 1, "id": "finished", "log": log_a.name,
            "log_sha256": "a" * 64, "exit_code": 0, "timed_out": False,
            "started_at": 1, "ended_at": 2, "test_counts": {"run": 1},
            "argv": ["x"]}), encoding="utf-8")
        (checks / "finished-0-000000000000.running").write_text(json.dumps({
            "schema_version": 1, "id": "finished", "pid": 1,
            "started_at": time.time(), "deadline_at": time.time() + 60,
            "timeout_seconds": 60, "deadline_scope": "wrapper timeout only",
            "log": log_a.name}), encoding="utf-8")
        log_b = checks / "legacy-0-000000000000.log"
        log_b.write_text("quiet\n", encoding="utf-8")
        (checks / "legacy-0-000000000000.json").write_text("{broken", encoding="utf-8")
        try:
            data = self.status(repo, "precedence", env=env)
            self.assertIsNone(data["checks"]["running"],
                              "a valid final receipt must supersede its stale running marker")
            self.assertEqual(data["checks"]["legacyCandidate"]["log"], log_b.name,
                             "invalid .json must not suppress a legitimate unreceipted log")
            self.assertEqual(data["checks"]["receipts"]["total"], 2)
            self.assertEqual(data["checks"]["receipts"]["scanned"], 1)
            reasons = {item["name"]: item["reason"] for item in data["checks"]["ignored"]}
            self.assertIn("superseded", reasons["finished-0-000000000000.running"])
            self.assertIn("invalid", reasons["legacy-0-000000000000.json"])
        finally:
            repo.cancel("precedence", env=env)
            self.assertEqual(repo.wait_terminal("precedence", env=env, timeout=25)["state"],
                             "cancelled")

    def test_status_partial_directory_scan_is_truthful(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("partial", worktree, env=env)
        repo.wait_round_state("partial", "running")
        checks = repo.task_dir("partial") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        for index in range(600):
            (checks / f"p-{index:05d}.log").write_text("x\n", encoding="utf-8")
        try:
            data = self.status(repo, "partial", env=env)
            self.assertTrue(data["checks"]["partial"])
            self.assertLessEqual(data["checks"]["entries"], 512)
            self.assertTrue(data["checks"]["receipts"]["partial"])
            notes = " ".join(data["notes"])
            self.assertIn("partially inspected", notes)
            self.assertNotIn("newest entries", notes,
                             "a partial arbitrary subset must not claim newest entries")
            self.assertLess(len(json.dumps(data)), 100000)
        finally:
            repo.cancel("partial", env=env)
            self.assertEqual(repo.wait_terminal("partial", env=env, timeout=25)["state"], "cancelled")

    def test_status_reports_execution_activity_and_process_identities(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("activity", worktree, env=env)
        state = repo.wait_round_state("activity", "running")
        try:
            data = self.status(repo, "activity", env=env)
            activity = data["executionActivity"]
            self.assertTrue(activity["roundJsonl"]["path"].endswith("round.jsonl"))
            self.assertTrue(activity["roundErr"]["path"].endswith("round.err"))
            self.assertIsInstance(activity["roundJsonl"]["bytes"], int)
            self.assertIsInstance(activity["roundJsonl"]["mtime"], float)
            self.assertIn("never useful progress", activity["note"])
            processes = data["processes"]
            self.assertEqual(processes["supervisorPid"], state["supervisorPid"])
            self.assertEqual(processes["piPid"], state["piPid"])
            self.assertTrue(processes["supervisorLeaseHeld"])
            self.assertTrue(processes["taskLockHeld"])
        finally:
            repo.cancel("activity", env=env)
            self.assertEqual(repo.wait_terminal("activity", env=env, timeout=25)["state"], "cancelled")

    def test_status_unknown_exit_is_never_latest_successful(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("unknown-exit", worktree, env=env)
        repo.wait_round_state("unknown-exit", "running")
        checks = repo.task_dir("unknown-exit") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        for index, code in enumerate((0, None)):
            log = checks / f"case-{index}-000000000000.log"
            log.write_text("evidence\n", encoding="utf-8")
            (checks / f"case-{index}-000000000000.json").write_text(json.dumps({
                "schema_version": 1, "id": f"case-{index}", "log": log.name,
                "log_sha256": "a" * 64, "exit_code": code, "timed_out": False,
                "started_at": 100 + index, "ended_at": 200 + index,
                "test_counts": {"run": 1}}), encoding="utf-8")
        try:
            receipts = self.status(repo, "unknown-exit", env=env)["checks"]["receipts"]
            self.assertEqual(receipts["scanned"], 2)
            self.assertEqual(receipts["failedAttempts"], 0)
            self.assertEqual(receipts["latest"]["id"], "case-1",
                             "the unknown exit stays the latest receipt")
            self.assertEqual(receipts["latestSuccessful"]["id"], "case-0",
                             "only a real exit code 0 may count as successful")
            # With only unknown exits there is no successful receipt at all.
            (checks / "case-0-000000000000.json").unlink()
            (checks / "case-0-000000000000.log").unlink()
            receipts = self.status(repo, "unknown-exit", env=env)["checks"]["receipts"]
            self.assertEqual(receipts["scanned"], 1)
            self.assertIsNone(receipts["latestSuccessful"],
                              "missing exit is unknown, never success")
        finally:
            repo.cancel("unknown-exit", env=env)
            self.assertEqual(repo.wait_terminal("unknown-exit", env=env, timeout=25)["state"],
                             "cancelled")

    def test_status_overflow_metadata_stays_bounded(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("overflow", worktree, env=env)
        repo.wait_round_state("overflow", "running")
        checks = repo.task_dir("overflow") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        huge = 10 ** 3000
        log = checks / "overflow-0-000000000000.log"
        log.write_text("x\n", encoding="utf-8")
        (checks / "overflow-time-0-000000000000.running").write_text(json.dumps({
            "schema_version": 1, "id": "overflow-time", "pid": 1, "started_at": huge,
            "deadline_at": 2, "timeout_seconds": 60,
            "deadline_scope": "wrapper timeout only", "log": log.name}), encoding="utf-8")
        (checks / "overflow-0-000000000000.running").write_text(json.dumps({
            "schema_version": 1, "id": "overflow", "pid": huge, "started_at": 1,
            "deadline_at": 2, "timeout_seconds": 60,
            "deadline_scope": "wrapper timeout only", "log": log.name}), encoding="utf-8")
        state_path = repo.task_dir("overflow") / "rounds" / "1" / "round.state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["supervisorPid"] = huge
        state["piPid"] = huge
        state_path.write_text(json.dumps(state), encoding="utf-8")
        try:
            started = time.monotonic()
            data = self.status(repo, "overflow", env=env)
            self.assertLess(time.monotonic() - started, 10)
            self.assertLess(len(json.dumps(data)), 100000)
            self.assertIsNone(data["processes"]["supervisorPid"])
            self.assertIsNone(data["processes"]["piPid"])
            running = data["checks"]["running"]
            self.assertIsNotNone(running, "the valid marker with a huge pid stays uncertain")
            self.assertIsNone(running["pid"])
            self.assertFalse(running["pidAlive"])
            reasons = " ".join(item["reason"] for item in data["checks"]["ignored"])
            self.assertIn("invalid running marker", reasons)
        finally:
            repo.cancel("overflow", env=env)
            self.assertEqual(repo.wait_terminal("overflow", env=env, timeout=25)["state"],
                             "cancelled")

    def test_wait_is_bounded_and_returns_compact_status_without_cancelling(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("waiting", worktree, env=env)
        state = repo.wait_round_state("waiting", "running")
        started = time.monotonic()
        data = cli_json("wait", "--repo", str(repo.root), "--task", "waiting",
                        "--timeout-ms", "400", env=env)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(data["wait"]["timedOut"])
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["acceptance"], "not_verified")
        self.assertIn("never cancels", data["wait"]["note"])
        self.assertIn("steering", data["wait"]["instruction"])
        self.assertIn("repeat", data["wait"]["instruction"].lower())
        self.assertIn("collect the full result", data["wait"]["instruction"])
        self.assertIn("checks", data, "active wait must return the compact status snapshot")
        self.assertTrue(pid_alive(state["piPid"]))
        repo.cancel("waiting", env=env)
        self.assertEqual(repo.wait_terminal("waiting", env=env, timeout=25)["state"], "cancelled")

    def test_wait_terminal_exact_round_returns_full_result(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("exact", worktree, env=env)
        self.assertEqual(repo.wait_terminal("exact", env=env)["state"], "completed")
        env_hang = base_env(PI_DOUBLE_MODE="hang")
        repo.continue_task("exact", env=env_hang)
        repo.wait_round_state("exact", "running")
        try:
            data = cli_json("wait", "--repo", str(repo.root), "--task", "exact", "--round", "1",
                            "--timeout-ms", "5000", env=env_hang)
            self.assertEqual(data["round"], 1)
            self.assertEqual(data["state"], "completed")
            self.assertFalse(data["wait"]["timedOut"])
            self.assertIsNotNone(data["summary"])
            self.assertEqual(data["acceptance"], "not_verified")
        finally:
            repo.cancel("exact", env=env_hang)
            self.assertEqual(repo.wait_terminal("exact", env=env_hang, timeout=25)["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
