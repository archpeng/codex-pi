"""The runtime must never execute a Codex binary, proven with a real PATH trap."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from runtime_helpers import Repo, base_env, cleanup_repos, cli_json, default_config


class CodexTrapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-trap-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        self.marker = self.tmp / "codex-was-executed"
        trap = self.bindir / "codex"
        trap.write_text(f"#!/bin/sh\ntouch {self.marker}\nexit 97\n", encoding="utf-8")
        trap.chmod(0o755)

    def tearDown(self):
        cleanup_repos()

    def trap_env(self, **extra):
        env = base_env(**extra)
        env["PATH"] = str(self.bindir) + os.pathsep + env.get("PATH", "")
        return env

    def test_full_lifecycle_never_executes_codex(self):
        repo = Repo(self.tmp, name="trapped", config=default_config())
        worktree = repo.worktree("wt")
        trace = self.tmp / "trace.jsonl"
        env = self.trap_env(PI_DOUBLE_MODE="session-trace", PI_DOUBLE_TRACE=str(trace))
        cli_json("project", "--repo", str(repo.root), env=env)
        repo.start("trapped-task", worktree, env=env)
        result = repo.wait_terminal("trapped-task", env=env, timeout=30)
        self.assertEqual(result["state"], "completed")
        repo.continue_task("trapped-task", env=env)
        result = repo.wait_terminal("trapped-task", env=env, timeout=30)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertFalse(self.marker.exists(),
                         "a codex executable on PATH was invoked by the runtime")
        brief = (repo.task_dir("trapped-task") / "rounds" / "1" / "brief.md").read_text()
        self.assertIn("do not execute the Codex CLI", brief)
        self.assertEqual(result["acceptance"], "not_verified")

    def test_worker_environment_keeps_trap_without_running_it(self):
        repo = Repo(self.tmp, name="trapped2", config=default_config())
        worktree = repo.worktree("wt")
        env = self.trap_env(PI_DOUBLE_MODE="ok")
        repo.start("quiet", worktree, env=env)
        repo.wait_terminal("quiet", env=env, timeout=30)
        self.assertFalse(self.marker.exists())
        self.assertTrue(json.loads(
            (repo.task_dir("quiet") / "rounds" / "1" / "round.state.json").read_text())["exitCode"] == 0)


if __name__ == "__main__":
    unittest.main()
