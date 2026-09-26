"""Behavioral tests for ``pi_task.py check`` per-observer observation dedup.

The observer is a read-only projection of bounded status evidence plus one
small atomic cursor file per observer. It never acknowledges delivery,
acceptance, handoff or task ownership, and it never modifies worker state.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime_helpers import (RUNTIME, Repo, base_env, cleanup_repos, cli_json,
                             default_config, run_cli, snapshot_files)


class ObserverTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-observer-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        cleanup_repos()

    def make(self, name: str = "repo"):
        repo = Repo(self.tmp, name=name, config=default_config())
        worktree = repo.worktree("wt")
        return repo, worktree

    def check(self, repo, task: str, observer: str, env: dict | None = None) -> dict:
        return cli_json("check", "--repo", str(repo.root), "--task", task,
                        "--observer", observer, env=env or base_env())

    def run_pi_check(self, checks: Path, check_id: str, *command: str):
        checks.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(checks),
             "--id", check_id, "--", *command],
            capture_output=True, text=True, timeout=60, cwd=str(self.tmp))

    # ------------------------------------------------------------------
    def test_terminal_observation_is_new_once_then_unchanged(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("observer-terminal", worktree, env=env)
        self.assertEqual(repo.wait_terminal("observer-terminal", env=env)["state"], "completed")

        first = self.check(repo, "observer-terminal", "owner-1", env)
        self.assertTrue(first["changed"])
        self.assertTrue(first["firstObservation"])
        self.assertEqual(first["state"], "completed")
        self.assertEqual(first["round"], 1)
        self.assertEqual(first["acceptance"], "not_verified")
        self.assertTrue(first["reviewRequired"])
        terminal = [alert for alert in first["newAlerts"] if alert["kind"] == "round_terminal"]
        self.assertEqual(len(terminal), 1)
        self.assertTrue(terminal[0]["new"])
        self.assertTrue(terminal[0]["reviewRequired"])
        self.assertTrue(Path(terminal[0]["evidence"]["state"]).is_file())
        self.assertIn("checks", first)
        self.assertIn("main review", first["instruction"])
        cursor = Path(first["cursor"]["path"])
        self.assertTrue(cursor.is_file())
        self.assertEqual(cursor.parent.name, "observers")
        self.assertIn("observation dedup", first["dedup"])

        second = self.check(repo, "observer-terminal", "owner-1", env)
        self.assertFalse(second["changed"])
        self.assertEqual(second["observation"], "unchanged")
        self.assertEqual(second["newAlertCount"], 0)
        self.assertEqual(second["newAlerts"], [])
        self.assertGreaterEqual(second["unresolvedAlertCount"], 1)
        self.assertTrue(second["reviewRequired"])
        rendered = json.dumps(second)
        self.assertNotIn("logEvidence", rendered, "unchanged records carry no log tails")
        self.assertNotIn('"checks"', rendered)
        self.assertIn("do not", second["instruction"])
        self.assertIn("wait", second["instruction"])

    def test_failed_check_alert_surfaces_exactly_once_per_observer(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("observer-failure", worktree, env=env)
        repo.wait_round_state("observer-failure", "running")
        checks = repo.task_dir("observer-failure") / "rounds" / "1" / "round.checks"
        failed = self.run_pi_check(checks, "failing", sys.executable, "-c",
                                   "import sys; sys.exit(5)")
        self.assertEqual(failed.returncode, 5, failed.stderr)
        try:
            first = self.check(repo, "observer-failure", "owner-fail", env)
            alerts = [alert for alert in first["newAlerts"] if alert["kind"] == "check_failed"]
            self.assertEqual(len(alerts), 1)
            self.assertTrue(Path(alerts[0]["evidence"]["receipt"]).is_file())
            self.assertTrue(Path(alerts[0]["evidence"]["log"]).is_file())
            self.assertEqual(alerts[0]["round"], 1)

            second = self.check(repo, "observer-failure", "owner-fail", env)
            self.assertEqual(second["newAlertCount"], 0)
            self.assertFalse([alert for alert in second["newAlerts"]
                              if alert["kind"] == "check_failed"])
            unresolved = [alert for alert in second["unresolvedAlerts"]
                          if alert["kind"] == "check_failed"]
            self.assertEqual(len(unresolved), 1)
            self.assertFalse(unresolved[0]["new"])

            other = self.check(repo, "observer-failure", "owner-other", env)
            other_failed = [alert for alert in other["alerts"]
                            if alert["kind"] == "check_failed" and alert["new"]]
            self.assertEqual(len(other_failed), 1,
                             "a different observer has its own cursor and sees the fact as new")
        finally:
            repo.cancel("observer-failure", env=env)
            self.assertEqual(repo.wait_terminal("observer-failure", env=env, timeout=25)["state"],
                             "cancelled")

    def test_resource_breach_alert_surfaces_through_check_once(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("observer-breach", worktree, env=env)
        repo.wait_round_state("observer-breach", "running")
        watched = self.tmp / "watched"
        watched.mkdir()
        (watched / "payload.bin").write_bytes(b"x" * 20000)
        checks = repo.task_dir("observer-breach") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(checks),
             "--id", "breaching", "--watch-path", str(watched), "--max-bytes", "1000",
             "--health-interval-seconds", "0.2", "--", sys.executable, "-c", "pass"],
            capture_output=True, text=True, timeout=60, cwd=str(self.tmp))
        self.assertEqual(proc.returncode, 75, proc.stderr)
        try:
            status = cli_json("status", "--repo", str(repo.root), "--task", "observer-breach",
                              env=env)
            guard = status["checks"]["resourceGuard"]
            self.assertTrue(guard["breached"], "status must surface the guard breach")
            self.assertTrue(guard["breaches"], guard)
            self.assertEqual(guard["breaches"][0]["observedBytes"], 20000)
            self.assertEqual(guard["breaches"][0]["maxBytes"], 1000)
            self.assertTrue(any("resource guard" in note for note in status["notes"]))

            first = self.check(repo, "observer-breach", "owner-breach", env)
            breaches = [alert for alert in first["newAlerts"] if alert["kind"] == "resource_breach"]
            self.assertEqual(len(breaches), 1)
            self.assertTrue(first["resources"]["breached"])
            self.assertIn("20000", breaches[0]["message"])

            second = self.check(repo, "observer-breach", "owner-breach", env)
            self.assertEqual(second["newAlertCount"], 0)
            self.assertTrue(second["resources"]["breached"])
            self.assertFalse([alert for alert in second["unresolvedAlerts"]
                              if alert["kind"] == "resource_breach" and alert["new"]])
        finally:
            repo.cancel("observer-breach", env=env)
            self.assertEqual(repo.wait_terminal("observer-breach", env=env, timeout=25)["state"],
                             "cancelled")

    def test_round_transition_changes_for_the_same_observer(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("observer-round", worktree, env=env)
        self.assertEqual(repo.wait_terminal("observer-round", env=env)["state"], "completed")
        first = self.check(repo, "observer-round", "owner-round", env)
        self.assertEqual(first["round"], 1)
        stable = self.check(repo, "observer-round", "owner-round", env)
        self.assertFalse(stable["changed"])

        env_hang = base_env(PI_DOUBLE_MODE="hang")
        repo.continue_task("observer-round", env=env_hang)
        repo.wait_round_state("observer-round", "running")
        try:
            second = self.check(repo, "observer-round", "owner-round", env_hang)
            self.assertTrue(second["changed"], "a new round is a meaningful structure change")
            self.assertEqual(second["round"], 2)
            self.assertEqual(second["state"], "running")
            again = self.check(repo, "observer-round", "owner-round", env_hang)
            self.assertFalse(again["changed"])
        finally:
            repo.cancel("observer-round", env=env_hang)
            self.assertEqual(repo.wait_terminal("observer-round", env=env_hang, timeout=25)["state"],
                             "cancelled")

    def test_malformed_and_oversized_cursor_are_unknown_not_success(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("observer-cursor", worktree, env=env)
        self.assertEqual(repo.wait_terminal("observer-cursor", env=env)["state"], "completed")
        self.check(repo, "observer-cursor", "owner-corrupt", env)
        cursor = repo.task_dir("observer-cursor") / "observers" / "owner-corrupt.json"
        self.assertTrue(cursor.is_file())

        cursor.write_text("{not json", encoding="utf-8")
        malformed = self.check(repo, "observer-cursor", "owner-corrupt", env)
        self.assertTrue(malformed["changed"])
        self.assertTrue(any("cursor" in item for item in malformed["unknown"]), malformed["unknown"])

        cursor.write_bytes(b'{"schemaVersion": 1, "padding": "' + b"x" * 70000 + b'"}')
        oversized = self.check(repo, "observer-cursor", "owner-corrupt", env)
        self.assertTrue(oversized["changed"])
        self.assertTrue(any("oversized" in item for item in oversized["unknown"]),
                        oversized["unknown"])

        recovered = self.check(repo, "observer-cursor", "owner-corrupt", env)
        self.assertFalse(recovered["changed"], "a valid rewritten cursor keeps dedup working")
        self.assertFalse(any("cursor" in item for item in recovered["unknown"]),
                         recovered["unknown"])

    def test_observer_identity_and_symlinks_are_rejected_without_writes(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("observer-safety", worktree, env=env)
        self.assertEqual(repo.wait_terminal("observer-safety", env=env)["state"], "completed")
        task_dir = repo.task_dir("observer-safety")
        for bad in ("../escape", "a/b", ".hidden", "", "x" * 101):
            proc = run_cli("check", "--repo", str(repo.root), "--task", "observer-safety",
                           "--observer", bad, env=env, expect=2)
            self.assertIn("observer", proc.stderr)
        self.assertFalse((task_dir / "observers").exists(), "rejected identities create nothing")

        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, task_dir / "observers")
        proc = run_cli("check", "--repo", str(repo.root), "--task", "observer-safety",
                       "--observer", "owner-symlink", env=env, expect=2)
        self.assertIn("symlink", proc.stderr)
        self.assertEqual(list(outside.iterdir()), [], "a symlinked observers dir is never written")
        os.unlink(task_dir / "observers")

        observers = task_dir / "observers"
        observers.mkdir()
        outside_cursor = outside / "cursor.json"
        outside_cursor.write_text("ORIGINAL", encoding="utf-8")
        os.symlink(outside_cursor, observers / "owner-symlink.json")
        proc = run_cli("check", "--repo", str(repo.root), "--task", "observer-safety",
                       "--observer", "owner-symlink", env=env, expect=2)
        self.assertIn("symlink", proc.stderr)
        self.assertEqual(outside_cursor.read_text(encoding="utf-8"), "ORIGINAL")

    def test_status_stays_read_only_and_check_writes_only_its_cursor(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("observer-readonly", worktree, env=env)
        self.assertEqual(repo.wait_terminal("observer-readonly", env=env)["state"], "completed")
        task_dir = repo.task_dir("observer-readonly")
        before = snapshot_files(task_dir)
        cli_json("status", "--repo", str(repo.root), "--task", "observer-readonly", env=env)
        self.assertEqual(snapshot_files(task_dir), before, "status must stay read-only")
        self.assertFalse((task_dir / "observers").exists())

        self.check(repo, "observer-readonly", "owner-read-only", env)
        self.assertTrue((task_dir / "observers" / "owner-read-only.json").is_file())
        after = snapshot_files(task_dir)
        self.assertEqual({key: value for key, value in before.items()
                          if not key.startswith("observers/")},
                         {key: value for key, value in after.items()
                          if not key.startswith("observers/")},
                         "check must not modify round, task or worker evidence")

    def test_snapshot_contains_self_contained_helpers(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("observer-snapshot", worktree, env=env)
        task_dir = repo.task_dir("observer-snapshot")
        tools = task_dir / "tools"
        for name in ("pi_task.py", "pi_check.py", "pi_summary.py", "pi_copy.py", "pi_size.py",
                     "pi_board.py", "VERSION"):
            self.assertTrue((tools / name).is_file(), f"frozen snapshot is missing {name}")
        task_json = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        self.assertIn("pi_copy.py", task_json["helperHashes"])
        self.assertIn("pi_size.py", task_json["helperHashes"])
        self.assertIn("pi_board.py", task_json["helperHashes"])
        check = subprocess.run(
            [sys.executable, str(tools / "pi_check.py"), "--output-dir", str(self.tmp / "snap-checks"),
             "--id", "snapshot-check", "--", sys.executable, "-c", "print('snapshot ok')"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stderr)
        source = self.tmp / "snapshot-src"
        source.mkdir()
        (source / "x.txt").write_text("snapshot", encoding="utf-8")
        copied = subprocess.run(
            [sys.executable, str(tools / "pi_copy.py"), str(source),
             str(self.tmp / "snapshot-dst"), "--max-bytes", "100"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertTrue((self.tmp / "snapshot-dst" / "x.txt").is_file())
        self.assertEqual(repo.wait_terminal("observer-snapshot", env=env)["state"], "completed")

    def test_unknown_task_is_rejected(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        proc = run_cli("check", "--repo", str(repo.root), "--task", "missing",
                       "--observer", "owner-missing", env=env, expect=2)
        self.assertIn("unknown task", proc.stderr)


if __name__ == "__main__":
    unittest.main()
