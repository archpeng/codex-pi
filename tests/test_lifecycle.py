"""End-to-end detached worker lifecycle tests against real git repos/worktrees."""
from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from runtime_helpers import (CLI, RUNTIME, Repo, base_env, cli_json,
                             cleanup_repos, default_config, kill_pid, make_pi_trap, pid_alive,
                             run_cli, snapshot_files, wait_gone, write_config)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-life-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        cleanup_repos()

    def make(self, config: dict | None = None, name: str = "repo"):
        repo = Repo(self.tmp, name=name, config=config if config is not None else default_config())
        worktree = repo.worktree("wt")
        return repo, worktree

    # ------------------------------------------------------------------
    def test_start_completes_with_bounded_evidence_and_no_acceptance(self):
        repo, worktree = self.make()
        trace = self.tmp / "trace.jsonl"
        env = base_env(PI_DOUBLE_MODE="session-trace", PI_DOUBLE_TRACE=str(trace))
        response = repo.start_json("alpha", worktree, env=env)
        self.assertEqual(response["round"], 1)
        self.assertEqual(response["state"], "starting")
        self.assertEqual(response["worktree"], str(worktree.resolve()))
        self.assertEqual(response["sessionId"], "alpha")
        self.assertTrue(response["evidence"]["taskDir"].endswith("codex-pi/tasks/alpha"))
        task_dir = repo.task_dir("alpha")
        self.assertTrue(task_dir.is_dir())

        brief = task_dir / "rounds" / "1" / "brief.md"
        self.assertTrue(brief.is_file())
        self.assertEqual(stat_mode(brief), 0o444)
        brief_text = brief.read_text(encoding="utf-8")
        self.assertIn("do not execute the Codex CLI", brief_text)
        self.assertIn("acceptance PASS", brief_text)
        self.assertIn("AGENTS.md", brief_text)
        self.assertIn(str(task_dir / "tools" / "pi_check.py"), brief_text)
        digest = hashlib.sha256(brief.read_bytes()).hexdigest()
        state = json.loads((task_dir / "rounds" / "1" / "round.state.json").read_text())
        self.assertEqual(state["briefSha256"], digest)
        task_json = json.loads((task_dir / "task.json").read_text())
        self.assertEqual(task_json["runtimeVersion"], (RUNTIME / "VERSION").read_text().strip())
        self.assertEqual(task_json["helperHashes"]["pi_task.py"],
                         hashlib.sha256((RUNTIME / "pi_task.py").read_bytes()).hexdigest())
        self.assertTrue((task_dir / "tools" / "pi_check.py").is_file())

        result = repo.wait_terminal("alpha", env=env)
        self.assertEqual(result["state"], "completed")
        finished = json.loads((task_dir / "rounds" / "1" / "round.state.json").read_text())
        self.assertEqual(Path(finished["workerScript"]).resolve().parent, (task_dir / "tools").resolve())
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["execution"], "completed_execution")
        self.assertEqual(result["acceptance"], "not_verified")
        self.assertFalse(result["activeWorker"])
        self.assertEqual(result["summary"]["usage"]["input"], 10)
        self.assertEqual(result["summary"]["usage"]["output"], 5)
        self.assertEqual(result["summary"]["model_check"], "matched")
        self.assertTrue(result["usageComplete"])
        self.assertEqual(result["summary"]["acceptance"], "not_verified")
        checks = result["summary"]["checks"]
        self.assertEqual(checks["total"], 0)
        self.assertIn("missing evidence", checks["absence"])
        self.assertNotIn("tool_execution_start", json.dumps(result["summary"]))
        self.assertNotIn("commands", result["summary"])

        recorded = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["sessionId"], "alpha")
        self.assertEqual(recorded[0]["model"], "deepseek/deepseek-flash")
        self.assertEqual(recorded[0]["thinking"], "max")
        self.assertEqual(recorded[0]["tools"], "read,write,edit,bash")
        self.assertTrue(recorded[0]["noExtensions"] and recorded[0]["noSkills"]
                        and recorded[0]["noPromptTemplates"])
        self.assertFalse(recorded[0]["noContextFiles"], "AGENTS.md discovery must stay enabled")
        self.assertEqual(Path(recorded[0]["atFile"]).name, "brief.md")

    def test_read_only_limits_pi_tools(self):
        repo, worktree = self.make()
        trace = self.tmp / "trace.jsonl"
        env = base_env(PI_DOUBLE_MODE="session-trace", PI_DOUBLE_TRACE=str(trace))
        repo.start("ro", worktree, read_only=True, env=env)
        result = repo.wait_terminal("ro", env=env)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["readOnly"])
        recorded = json.loads(trace.read_text().splitlines()[0])
        self.assertEqual(recorded["tools"], "read,grep,find,ls")
        self.assertIn("read-only", (repo.task_dir("ro") / "rounds" / "1" / "brief.md").read_text())

    def test_project_selected_newapi_model_is_pinned_and_passed_to_pi(self):
        model = "newapi/glm-5.3"
        repo, worktree = self.make(default_config(model=model))
        trace = self.tmp / "newapi-trace.jsonl"
        env = base_env(PI_DOUBLE_MODE="session-trace", PI_DOUBLE_TRACE=str(trace),
                       PI_DOUBLE_REPORTED_PROVIDER="newapi",
                       PI_DOUBLE_REPORTED_MODEL="glm-5.3")

        started = repo.start_json("newapi-model", worktree, env=env)
        self.assertEqual(started["model"], model)
        result = repo.wait_terminal("newapi-model", env=env)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["summary"]["model_check"], "matched")

        task = json.loads((repo.task_dir("newapi-model") / "task.json").read_text())
        self.assertEqual(task["model"], model)
        brief = (repo.task_dir("newapi-model") / "rounds" / "1" / "brief.md").read_text()
        self.assertIn(f"pinned to `{model}`", brief)
        recorded = json.loads(trace.read_text().splitlines()[0])
        self.assertEqual(recorded["model"], model)

    # ------------------------------------------------------------------
    def test_failed_attempt_stays_visible_and_continue_uses_same_session(self):
        repo, worktree = self.make()
        trace = self.tmp / "trace.jsonl"
        fail_env = base_env(PI_DOUBLE_MODE="fail", PI_DOUBLE_TRACE=str(trace))
        repo.start("beta", worktree, env=fail_env)
        first = repo.wait_terminal("beta", env=fail_env)
        self.assertEqual(first["state"], "failed")
        self.assertEqual(first["exitCode"], 3)
        self.assertEqual(first["acceptance"], "not_verified")
        round1 = repo.task_dir("beta") / "rounds" / "1"
        brief1 = (round1 / "brief.md").read_bytes()
        log1 = (round1 / "round.jsonl").read_bytes()

        # Changing project selection never changes an already-started task.
        write_config(repo.root, default_config(model="newapi/glm-5.3"))

        ok_env = base_env(PI_DOUBLE_MODE="session-trace", PI_DOUBLE_TRACE=str(trace))
        response = json.loads(repo.continue_task("beta", env=ok_env).stdout)
        self.assertEqual(response["round"], 2)
        second = repo.wait_terminal("beta", env=ok_env)
        self.assertEqual(second["state"], "completed")
        self.assertEqual(len(second["rounds"]), 2)
        self.assertEqual(second["rounds"][0]["state"], "failed")
        self.assertEqual(second["rounds"][0]["exitCode"], 3)
        self.assertEqual(second["round"], 2)
        explicit = repo.result("beta", round=1, env=ok_env)
        self.assertEqual(explicit["round"], 1)
        self.assertEqual(explicit["state"], "failed")
        self.assertEqual(explicit["execution"], "failed")

        self.assertEqual((round1 / "brief.md").read_bytes(), brief1)
        self.assertEqual((round1 / "round.jsonl").read_bytes(), log1)
        recorded = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(recorded), 2)
        self.assertEqual(recorded[0]["model"], "deepseek/deepseek-flash")
        self.assertEqual(recorded[1]["model"], "deepseek/deepseek-flash")
        self.assertEqual(recorded[0]["sessionId"], recorded[1]["sessionId"])
        self.assertEqual(recorded[0]["sessionDir"], recorded[1]["sessionDir"])
        self.assertEqual(recorded[1]["cwd"], str(worktree.resolve()))
        self.assertIn("Previous round 1", (repo.task_dir("beta") / "rounds" / "2" / "brief.md").read_text())

    def test_continue_refused_while_worker_lock_is_held(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("locked", worktree, env=env)
        repo.wait_round_state("locked", "running")
        proc = repo.continue_task("locked", env=env, expect=2)
        self.assertIn("still held", proc.stderr)
        cancel = repo.cancel("locked", env=env)
        self.assertIn(cancel["cancel"]["request"], ("requested", "observed"))
        result = repo.wait_terminal("locked", env=env, timeout=20)
        self.assertEqual(result["state"], "cancelled")

    # ------------------------------------------------------------------
    def test_timeout_is_recorded_and_kills_the_pi_process(self):
        repo, worktree = self.make(default_config(timeoutSeconds=1))
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("timeout", worktree, env=env)
        started = time.monotonic()
        result = repo.wait_terminal("timeout", env=env, timeout=25)
        elapsed = time.monotonic() - started
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(result["exitCode"], 124)
        self.assertTrue(result["timedOut"])
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertEqual(result["acceptance"], "not_verified")

    def test_cancel_stops_descendants_that_ignore_sigterm(self):
        repo, worktree = self.make()
        pidfile = self.tmp / "grandchild.pid"
        env = base_env(PI_DOUBLE_MODE="descendant-hang",
                       PI_DOUBLE_GRANDCHILD_PIDFILE=str(pidfile))
        repo.start("cancel", worktree, env=env)
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(pidfile.exists(), "descendant never started")
        grandchild_pid = int(pidfile.read_text())
        cancel = repo.cancel("cancel", env=env)
        self.assertIn(cancel["cancel"]["request"], ("requested", "observed"))
        result = repo.wait_terminal("cancel", env=env, timeout=25)
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["cancelled"])
        self.assertTrue(wait_gone(grandchild_pid, 10),
                        "descendant ignoring SIGTERM survived cancellation")

    def test_lingering_descendant_dies_after_normal_completion(self):
        repo, worktree = self.make()
        pidfile = self.tmp / "grandchild2.pid"
        env = base_env(PI_DOUBLE_MODE="descendant-ignore-term",
                       PI_DOUBLE_GRANDCHILD_PIDFILE=str(pidfile))
        repo.start("linger", worktree, env=env)
        result = repo.wait_terminal("linger", env=env, timeout=25)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["exitCode"], 0)
        self.assertTrue(pidfile.exists())
        grandchild_pid = int(pidfile.read_text())
        self.assertTrue(wait_gone(grandchild_pid, 10),
                        "descendant ignoring SIGTERM survived a completed round")

    # ------------------------------------------------------------------
    def test_unknown_after_supervisor_crash_is_never_success(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("lost", worktree, env=env)
        state = repo.wait_round_state("lost", "running")
        kill_pid(state["supervisorPid"], signal.SIGKILL)
        if state.get("piPid"):
            kill_pid(state["piPid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        result = None
        while time.monotonic() < deadline:
            result = repo.result("lost", env=env)
            if result["state"] == "unknown":
                break
            time.sleep(0.2)
        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "unknown")
        self.assertNotEqual(result["execution"], "completed_execution")
        self.assertEqual(result["acceptance"], "not_verified")
        self.assertFalse(result["activeWorker"])
        proc = repo.continue_task("lost", env=env, expect=2)
        self.assertIn("not terminal-known", proc.stderr)

    # ------------------------------------------------------------------
    def test_same_task_worktree_and_max_workers_are_enforced(self):
        repo, worktree = self.make(default_config(maxWorkers=1))
        second = repo.worktree("wt2")
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("one", worktree, env=env)
        repeated = repo.start("one", worktree, env=env, expect=2)
        self.assertIn("already exists", repeated.stderr)
        claimed = repo.start("two", worktree, env=env, expect=2)
        self.assertIn("claimed", claimed.stderr)
        over_limit = repo.start("two", second, env=env, expect=2)
        self.assertIn("maxWorkers", over_limit.stderr)
        self.assertFalse(repo.task_dir("two").exists())
        repo.cancel("one", env=env)
        repo.wait_terminal("one", env=env, timeout=20)
        repo.start("two", second, env=env)
        running = repo.result("two", env=env)
        self.assertIn(running["state"], ("starting", "running"))
        self.assertNotEqual(running["acceptance"], "PASS")
        repo.cancel("two", env=env)
        result = repo.wait_terminal("two", env=env, timeout=20)
        self.assertEqual(result["state"], "cancelled")
        # A finished task still owns its worktree: the claim is not silently reused.
        third = repo.start("three", worktree, env=env, expect=2)
        self.assertIn("claimed", third.stderr)

    def test_project_isolation_for_same_task_id(self):
        alpha, alpha_wt = self.make(name="alpha-repo")
        beta, beta_wt = self.make(name="beta-repo")
        env = base_env(PI_DOUBLE_MODE="hang")
        alpha.start("shared", alpha_wt, env=env)
        beta.start("shared", beta_wt, env=env)
        self.assertNotEqual(alpha.task_dir("shared"), beta.task_dir("shared"))
        alpha.cancel("shared", env=env)
        beta.cancel("shared", env=env)
        self.assertEqual(alpha.wait_terminal("shared", env=env, timeout=20)["state"], "cancelled")
        self.assertEqual(beta.wait_terminal("shared", env=env, timeout=20)["state"], "cancelled")

    def test_worktree_validation(self):
        repo, worktree = self.make()
        env = base_env()
        canonical = repo.start("x1", repo.root, env=env, expect=2)
        self.assertIn("separate checkout", canonical.stderr)
        missing = repo.start("x2", self.tmp / "does-not-exist", env=env, expect=2)
        self.assertIn("does not exist", missing.stderr)
        other, other_wt = self.make(name="other-repo")
        foreign = repo.start("x3", other_wt, env=env, expect=2)
        self.assertIn("foreign", foreign.stderr)
        nested = worktree / "subdir"
        nested.mkdir()
        subdir = repo.start("x5", nested, env=env, expect=2)
        self.assertIn("checkout root", subdir.stderr)

    def test_wait_timeout_does_not_cancel_and_result_remains_bounded(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("waiting", worktree, env=env)
        data = cli_json("wait", "--repo", str(repo.root), "--task", "waiting",
                        "--timeout-ms", "500", env=env)
        self.assertTrue(data["wait"]["timedOut"])
        self.assertEqual(data["state"], "running")
        self.assertIn("never cancels", data["wait"]["note"])
        self.assertEqual(data["acceptance"], "not_verified")
        # Still alive after the wait timeout.
        self.assertTrue(pid_alive(json.loads(
            (repo.task_dir("waiting") / "rounds" / "1" / "round.state.json").read_text())["piPid"]))
        repo.cancel("waiting", env=env)
        repo.wait_terminal("waiting", env=env, timeout=20)

    def test_cancel_on_terminal_task_signals_nothing(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("done", worktree, env=env)
        repo.wait_terminal("done", env=env)
        cancel = repo.cancel("done", env=env)
        self.assertEqual(cancel["cancel"]["request"], "not_active")
        self.assertEqual(cancel["state"], "completed")
        self.assertEqual(cancel["acceptance"], "not_verified")
    def test_missing_model_usage_stays_unknown(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="no-usage")
        repo.start("nousage", worktree, env=env)
        result = repo.wait_terminal("nousage", env=env)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["usageComplete"])
        for key in ("input", "cacheRead", "cacheWrite", "output", "totalTokens"):
            self.assertIsNone(result["summary"]["usage"][key])
        self.assertEqual(result["summary"]["model_check"], "matched")
        self.assertEqual(result["acceptance"], "not_verified")

    def test_stale_round_evidence_blocks_continue(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("stale", worktree, env=env)
        repo.wait_terminal("stale", env=env)
        meta = repo.task_dir("stale") / "rounds" / "1" / "round.meta"
        meta.rename(meta.with_name("round.meta.moved"))
        proc = repo.continue_task("stale", env=env, expect=2)
        self.assertIn("no exit evidence", proc.stderr)

    def test_parallel_same_worktree_starts_have_one_winner(self):
        repo, worktree = self.make(default_config(maxWorkers=2))
        env = base_env(PI_DOUBLE_MODE="hang")
        processes = [subprocess.Popen(
            [sys.executable, str(CLI), "start", "--repo", str(repo.root), "--task", task,
             "--worktree", str(worktree), "--prompt", "race"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for task in ("race-a", "race-b")]
        results = [process.communicate(timeout=60) for process in processes]
        codes = sorted(process.returncode for process in processes)
        self.assertEqual(codes, [0, 2])
        loser = results[1][1] if processes[0].returncode == 0 else results[0][1]
        self.assertIn("claimed", loser)
        winner = next(process for process in range(2)
                      if processes[process].returncode == 0)
        winner_task = ("race-a", "race-b")[winner]
        repo.cancel(winner_task, env=env)
        repo.wait_terminal(winner_task, env=env, timeout=20)

    def test_parallel_max_workers_starts_have_one_winner(self):
        repo, worktree = self.make(default_config(maxWorkers=1))
        second = repo.worktree("wt2")
        env = base_env(PI_DOUBLE_MODE="hang")
        processes = [subprocess.Popen(
            [sys.executable, str(CLI), "start", "--repo", str(repo.root), "--task", task,
             "--worktree", str(worktree_path), "--prompt", "race"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for task, worktree_path in (("limit-a", worktree), ("limit-b", second))]
        results = [process.communicate(timeout=60) for process in processes]
        codes = sorted(process.returncode for process in processes)
        self.assertEqual(codes, [0, 2])
        loser = results[1][1] if processes[0].returncode == 0 else results[0][1]
        self.assertIn("maxWorkers", loser)
        winner = next(process for process in range(2)
                      if processes[process].returncode == 0)
        winner_task = ("limit-a", "limit-b")[winner]
        repo.cancel(winner_task, env=env)
        repo.wait_terminal(winner_task, env=env, timeout=20)
    def test_snapshot_check_helper_works_from_task_tools(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("helpers", worktree, env=env)
        repo.wait_terminal("helpers", env=env)
        task_dir = repo.task_dir("helpers")
        helper = task_dir / "tools" / "pi_check.py"
        self.assertTrue(helper.is_file())
        output = task_dir / "rounds" / "1" / "round.checks"
        proc = subprocess.run(
            [sys.executable, str(helper), "--output-dir", str(output), "--id", "snapshot-smoke",
             "--", sys.executable, "-c", "print('--- PASS: T')"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["acceptance"], "not_verified")
        summary = subprocess.run(
            [sys.executable, str(RUNTIME / "pi_summary.py"),
             str(task_dir / "rounds" / "1" / "round.jsonl"),
             "--worktree", str(worktree), "--run-dir", str(task_dir / "rounds" / "1"),
             "--checks-dir", str(output), "--json"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(summary.returncode, 0, summary.stderr)
        data = json.loads(summary.stdout)
        self.assertEqual(len(data["check_receipts"]), 1)
        self.assertEqual(data["check_receipts"][0]["exit_code"], 0)

    def test_unknown_round_rejected(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("rounds", worktree, env=env)
        repo.wait_terminal("rounds", env=env)
        proc = run_cli("result", "--repo", str(repo.root), "--task", "rounds", "--round", "9",
                       env=env, expect=2)
        self.assertIn("does not exist", proc.stderr)
    def test_duplicate_start_preserves_completed_evidence(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("dupe-done", worktree, env=env)
        repo.wait_terminal("dupe-done", env=env)
        task_dir = repo.task_dir("dupe-done")
        before = snapshot_files(task_dir)
        blocked = repo.start("dupe-done", worktree, env=env, expect=2)
        self.assertIn("already exists", blocked.stderr)
        self.assertIn("untouched", blocked.stderr)
        self.assertEqual(snapshot_files(task_dir), before,
                         "a duplicate start must not mutate existing evidence")
        state = json.loads((task_dir / "rounds" / "1" / "round.state.json").read_text())
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["exitCode"], 0)
        self.assertEqual(repo.result("dupe-done", env=env)["state"], "completed")

    def test_duplicate_start_preserves_failed_evidence(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="fail")
        repo.start("dupe-fail", worktree, env=env)
        first = repo.wait_terminal("dupe-fail", env=env)
        self.assertEqual(first["state"], "failed")
        task_dir = repo.task_dir("dupe-fail")
        before = snapshot_files(task_dir)
        blocked = repo.start("dupe-fail", worktree, env=base_env(PI_DOUBLE_MODE="ok"), expect=2)
        self.assertIn("already exists", blocked.stderr)
        self.assertEqual(snapshot_files(task_dir), before)
        after = repo.result("dupe-fail", env=env)
        self.assertEqual(after["state"], "failed")
        self.assertEqual(after["exitCode"], 3)

    def test_duplicate_start_preserves_active_round(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("dupe-live", worktree, env=env)
        repo.wait_round_state("dupe-live", "running")
        task_dir = repo.task_dir("dupe-live")
        round_log = task_dir / "rounds" / "1" / "round.jsonl"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not round_log.read_text(
                encoding="utf-8", errors="replace").strip():
            time.sleep(0.05)
        before = snapshot_files(task_dir)
        blocked = repo.start("dupe-live", worktree, env=env, expect=2)
        self.assertIn("already exists", blocked.stderr)
        self.assertEqual(snapshot_files(task_dir), before,
                         "a duplicate start must not mutate an active round")
        self.assertEqual(repo.result("dupe-live", env=env)["state"], "running")
        repo.cancel("dupe-live", env=env)
        self.assertEqual(repo.wait_terminal("dupe-live", env=env, timeout=20)["state"], "cancelled")

    def test_linked_repo_input_cannot_use_primary_checkout(self):
        repo, worktree = self.make()
        blocked = run_cli("start", "--repo", str(worktree), "--task", "bypass",
                          "--worktree", str(repo.root), "--prompt", "x",
                          env=base_env(), expect=2)
        self.assertIn("separate checkout", blocked.stderr)
        self.assertIn("primary checkout", blocked.stderr)
        self.assertFalse(repo.task_dir("bypass").exists())

    def test_continue_rejects_tampered_primary_worktree(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("tamper", worktree, env=env)
        repo.wait_terminal("tamper", env=env)
        task_json = repo.task_dir("tamper") / "task.json"
        data = json.loads(task_json.read_text())
        data["worktree"] = str(repo.root.resolve())
        task_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        blocked = repo.continue_task("tamper", env=env, expect=2)
        self.assertIn("primary checkout", blocked.stderr)

    def test_supervisor_sigkill_orphan_pi_keeps_capacity_and_unknown(self):
        repo, worktree = self.make(default_config(maxWorkers=1))
        second = repo.worktree("wt2")
        env = base_env(PI_DOUBLE_MODE="hang")
        repo.start("orphan", worktree, env=env)
        state = repo.wait_round_state("orphan", "running")
        kill_pid(state["supervisorPid"], signal.SIGKILL)
        try:
            self.assertTrue(pid_alive(state["piPid"]),
                            "the Pi child must survive a supervisor-only SIGKILL")
            result = repo.result("orphan", env=env)
            self.assertEqual(result["state"], "unknown")
            self.assertFalse(result["supervisorAlive"])
            self.assertTrue(result["activeWorker"])
            blocked_start = repo.start("second", second, env=env, expect=2)
            self.assertIn("maxWorkers", blocked_start.stderr)
            blocked_continue = repo.continue_task("orphan", env=env, expect=2)
            self.assertIn("still held", blocked_continue.stderr)
            orphan_cancel = repo.cancel("orphan", env=env)
            self.assertEqual(orphan_cancel["cancel"]["request"], "orphaned")
            self.assertEqual(orphan_cancel["state"], "unknown")
            self.assertFalse((repo.task_dir("orphan") / "cancel.json").exists())
        finally:
            kill_pid(state["piPid"], signal.SIGKILL)
            self.assertTrue(wait_gone(state["piPid"], 10))
        repo.start("second", second, env=env)
        repo.cancel("second", env=env)
        self.assertEqual(repo.wait_terminal("second", env=env, timeout=20)["state"], "cancelled")

    def test_worker_survives_caller_exit(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="1.5")
        proc = repo.start("detached", worktree, env=env)
        self.assertEqual(proc.returncode, 0)
        mid = repo.result("detached", env=env)
        self.assertIn(mid["state"], ("starting", "running"))
        result = repo.wait_terminal("detached", env=env, timeout=30)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["exitCode"], 0)
    def test_linked_repo_input_cannot_use_primary_checkout(self):
        repo, worktree = self.make()
        write_config(worktree)
        blocked = run_cli("start", "--repo", str(worktree), "--task", "bypass",
                          "--worktree", str(repo.root), "--prompt", "x",
                          env=base_env(), expect=2)
        self.assertIn("separate checkout", blocked.stderr)
        self.assertIn("Git primary checkout", blocked.stderr)
        self.assertFalse(repo.task_dir("bypass").exists())

    def test_continue_preserves_frozen_repo_identity(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("frozen", worktree, env=env)
        repo.wait_terminal("frozen", env=env)
        task_dir = repo.task_dir("frozen")
        before = snapshot_files(task_dir)
        blocked = run_cli("continue", "--repo", str(worktree), "--task", "frozen",
                          "--prompt", "x", env=env, expect=2)
        self.assertIn("belongs to checkout", blocked.stderr)
        self.assertIn(str(repo.root.resolve()), blocked.stderr)
        self.assertEqual(snapshot_files(task_dir), before)
        # result may still read common-dir evidence from any checkout of the project.
        result = cli_json("result", "--repo", str(worktree), "--task", "frozen", env=env)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["repo"], str(repo.root.resolve()))

    def test_stale_frozen_model_cannot_continue_and_evidence_is_preserved(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("stale-model", worktree, env=env)
        repo.wait_terminal("stale-model", env=env)
        task_dir = repo.task_dir("stale-model")
        task_json = task_dir / "task.json"
        data = json.loads(task_json.read_text())
        data["model"] = "openai-codex/gpt-6-luna"
        task_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        before = snapshot_files(task_dir)
        trap, marker = make_pi_trap(self.tmp / "bin")
        proc = run_cli("continue", "--repo", str(repo.root), "--task", "stale-model",
                       "--prompt", "x", env=base_env(PI_BIN=str(trap)), expect=2)
        self.assertIn("choose one", proc.stderr)
        self.assertIn("not modified", proc.stderr)
        self.assertEqual(snapshot_files(task_dir), before)
        self.assertFalse((task_dir / "rounds" / "2").exists())
        self.assertFalse(marker.exists())

    def test_internal_worker_revalidates_frozen_model(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok")
        repo.start("internal", worktree, env=env)
        repo.wait_terminal("internal", env=env)
        task_dir = repo.task_dir("internal")
        task_json = task_dir / "task.json"
        data = json.loads(task_json.read_text())
        data["model"] = "openai-codex/gpt-6-luna"
        task_json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        before = snapshot_files(task_dir)
        trap, marker = make_pi_trap(self.tmp / "bin")
        proc = subprocess.run(
            [sys.executable, str(task_dir / "tools" / "pi_task.py"), "_worker",
             "--task-dir", str(task_dir), "--round", "1", "--lock-fd", "0",
             "--timeout-seconds", "5"],
            env=base_env(PI_BIN=str(trap)), capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("choose one", proc.stderr)
        self.assertEqual(snapshot_files(task_dir), before)
        self.assertFalse(marker.exists())

    def test_reported_model_mismatch_stays_visible_and_not_verified(self):
        repo, worktree = self.make()
        env = base_env(PI_DOUBLE_MODE="ok", PI_DOUBLE_REPORTED_PROVIDER="openai-codex",
                       PI_DOUBLE_REPORTED_MODEL="gpt-6-luna")
        repo.start("mismatch", worktree, env=env)
        result = repo.wait_terminal("mismatch", env=env)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["summary"]["model_check"], "mismatch")
        self.assertIn("openai-codex/gpt-6-luna", result["summary"]["models"])
        self.assertEqual(result["acceptance"], "not_verified")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
