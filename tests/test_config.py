"""Configuration resolution, path safety and project info tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime_helpers import (Repo, base_env, cleanup_repos, cli_json, default_config,
                             make_pi_trap, run_cli, write_config)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-config-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        cleanup_repos()

    def test_missing_config_is_actionable_and_no_pi_runs(self):
        repo = Repo(self.tmp, name="nocfg", config=None)
        marker = self.tmp / "pi-ran"
        trap = self.tmp / "pi-trap"
        trap.write_text(f"#!/bin/sh\ntouch {marker}\nexit 9\n", encoding="utf-8")
        trap.chmod(0o755)
        proc = run_cli("project", "--repo", str(repo.root), env=base_env(PI_BIN=str(trap)), expect=2)
        self.assertIn(".agents/codex-pi.json", proc.stderr)
        self.assertIn("schemaVersion", proc.stderr)
        self.assertFalse(marker.exists())

    def test_invalid_schema_version(self):
        repo = Repo(self.tmp, name="schema", config=default_config(schemaVersion=2))
        proc = run_cli("project", "--repo", str(repo.root), expect=2)
        self.assertIn("schemaVersion", proc.stderr)

    def test_unknown_config_key_rejected(self):
        config = default_config()
        config["execute"] = "rm -rf /"
        repo = Repo(self.tmp, name="unknown", config=config)
        proc = run_cli("project", "--repo", str(repo.root), expect=2)
        self.assertIn("unsupported keys", proc.stderr)

    def test_config_traversal_and_absolute_references_rejected(self):
        for index, bad in enumerate(("../../etc/passwd", "/etc/passwd", "docs/../../secrets.md")):
            with self.subTest(reference=bad):
                repo = Repo(self.tmp, name=f"traversal-{index}",
                            config=default_config(constraints=[bad]))
                proc = run_cli("project", "--repo", str(repo.root), expect=2)
                self.assertIn("rejected", proc.stderr)
        repo = Repo(self.tmp, name="check-abs", config=default_config(checks=["/usr/bin/env"]))
        proc = run_cli("project", "--repo", str(repo.root), expect=2)
        self.assertIn("rejected", proc.stderr)

    def test_readme_only_constraints_are_allowed(self):
        repo = Repo(self.tmp, name="okrefs",
                    config=default_config(constraints=["AGENTS.md", "docs/plan/notes.md"],
                                          checks=["make check", "make target"]))
        data = cli_json("project", "--repo", str(repo.root))
        self.assertEqual(data["config"]["constraints"][0], "AGENTS.md")
        self.assertEqual(data["limits"]["codexCliInvocations"], 0)

    def test_symlinked_config_escape_rejected(self):
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps(default_config()), encoding="utf-8")
        repo = Repo(self.tmp, name="symlink", config=None)
        (repo.root / ".agents").mkdir()
        (repo.root / ".agents" / "codex-pi.json").symlink_to(outside)
        proc = run_cli("project", "--repo", str(repo.root), expect=2)
        self.assertIn("escapes the repository", proc.stderr)

    def test_project_info_returns_root_config_capabilities_and_limits(self):
        repo = Repo(self.tmp, name="info", config=default_config(maxWorkers=2, timeoutSeconds=600,
                                                                 model="deepseek/deepseek-flash",
                                                                 thinking="high"))
        data = cli_json("project", "--repo", str(repo.root))
        self.assertEqual(data["repo"], str(repo.root.resolve()))
        self.assertEqual(data["config"]["maxWorkers"], 2)
        self.assertEqual(data["config"]["timeoutSeconds"], 600)
        self.assertEqual(data["limits"]["model"], "deepseek/deepseek-flash")
        self.assertEqual(data["limits"]["thinking"], "high")
        self.assertIn("read", data["capabilities"]["readOnlyTools"])
        self.assertIn("bash", data["capabilities"]["writableTools"])
        self.assertTrue(data["limits"]["readOnlyIsNotASecuritySandbox"])
        self.assertEqual(data["limits"]["allowedModels"], ["deepseek/deepseek-flash"])
        self.assertIn("only deepseek/deepseek-flash", data["limits"]["modelPolicy"])
        self.assertEqual(data["activeTaskCount"], 0)

    def test_invalid_limits_rejected(self):
        for index, config in enumerate((default_config(maxWorkers=0), default_config(maxWorkers=True),
                                        default_config(timeoutSeconds=0), default_config(timeoutSeconds=10**9),
                                        default_config(thinking="turbo"))):
            with self.subTest(config=config):
                repo = Repo(self.tmp, name=f"limits-{index}", config=config)
                proc = run_cli("project", "--repo", str(repo.root), expect=2)
                self.assertNotEqual(proc.returncode, 0)

    def test_minimal_config_gets_documented_defaults(self):
        repo = Repo(self.tmp, name="minimal", config={"schemaVersion": 1})
        data = cli_json("project", "--repo", str(repo.root))
        self.assertEqual(data["config"]["model"], "deepseek/deepseek-flash")
        self.assertEqual(data["config"]["thinking"], "max")
        self.assertEqual(data["config"]["maxWorkers"], 1)
        self.assertEqual(data["config"]["timeoutSeconds"], 14400)
        self.assertEqual(data["config"]["constraints"], [])
        self.assertEqual(data["config"]["checks"], [])

    def test_task_id_traversal_rejected(self):
        repo = Repo(self.tmp, name="taskid", config=default_config())
        worktree = repo.worktree("wt")
        for bad in ("../evil", "/etc/passwd", "a/b", ".hidden", "has space"):
            with self.subTest(task=bad):
                proc = repo.start(bad, worktree, env=base_env(), expect=2)
                self.assertIn("task must match", proc.stderr)
        self.assertFalse((self.tmp / "evil").exists())

    def test_repo_path_traversal_is_resolved_through_git(self):
        repo = Repo(self.tmp, name="rooted", config=default_config())
        nested = repo.root / "nested" / "deeper"
        nested.mkdir(parents=True)
        data = cli_json("project", "--repo", str(nested))
        self.assertEqual(data["repo"], str(repo.root.resolve()))

    def test_linked_checkout_keeps_its_own_config(self):
        repo = Repo(self.tmp, name="linked", config=None)
        project_wt = repo.worktree("project")
        write_config(project_wt)
        worker_wt = repo.worktree("worker")
        data = cli_json("project", "--repo", str(project_wt))
        self.assertEqual(data["repo"], str(project_wt.resolve()))
        self.assertEqual(data["config"]["model"], "deepseek/deepseek-flash")
        self.assertEqual(data["limits"]["allowedModels"], ["deepseek/deepseek-flash"])
        self.assertEqual(data["capabilities"]["configCheckout"], str(project_wt.resolve()))
        # A second linked worker checkout is accepted and uses the project checkout config.
        started = run_cli("start", "--repo", str(project_wt), "--task", "cfg",
                          "--worktree", str(worker_wt), "--prompt", "x", env=base_env(), expect=0)
        response = json.loads(started.stdout)
        self.assertEqual(response["repo"], str(project_wt.resolve()))
        self.assertEqual(response["worktree"], str(worker_wt.resolve()))
        result = repo.wait_terminal("cfg", env=base_env())
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["repo"], str(project_wt.resolve()))
        frozen = json.loads((repo.task_dir("cfg") / "task.json").read_text())
        self.assertEqual(frozen["repo"], str(project_wt.resolve()))

    def test_configured_and_primary_checkouts_rejected_as_worktree(self):
        repo = Repo(self.tmp, name="rejectwt", config=None)
        project_wt = repo.worktree("project")
        write_config(project_wt)
        worker_wt = repo.worktree("worker")
        env = base_env()
        configured = run_cli("start", "--repo", str(project_wt), "--task", "reject-config",
                             "--worktree", str(project_wt), "--prompt", "x", env=env, expect=2)
        self.assertIn("separate checkout", configured.stderr)
        self.assertIn("configured project checkout", configured.stderr)
        primary = run_cli("start", "--repo", str(project_wt), "--task", "reject-primary",
                          "--worktree", str(repo.root), "--prompt", "x", env=env, expect=2)
        self.assertIn("Git primary checkout", primary.stderr)
        # --repo inversion: a different checkout must not target the configured
        # project checkout, and its own (missing) config is never substituted.
        inversion = run_cli("start", "--repo", str(worker_wt), "--task", "reject-invert",
                            "--worktree", str(project_wt), "--prompt", "x", env=env, expect=2)
        self.assertIn("config exists only in another checkout", inversion.stderr)
        self.assertIn(str(project_wt.resolve()), inversion.stderr)
        primary_inversion = run_cli("start", "--repo", str(repo.root), "--task", "reject-invert2",
                                    "--worktree", str(project_wt), "--prompt", "x", env=env, expect=2)
        self.assertIn("config exists only in another checkout", primary_inversion.stderr)
        for rejected in ("reject-config", "reject-primary", "reject-invert", "reject-invert2"):
            self.assertFalse(repo.task_dir(rejected).exists())

    def test_missing_config_hints_configured_checkout_without_loading_it(self):
        repo = Repo(self.tmp, name="hint", config=None)
        project_wt = repo.worktree("project")
        write_config(project_wt)
        primary = run_cli("project", "--repo", str(repo.root), expect=2)
        self.assertIn(".agents/codex-pi.json", primary.stderr)
        self.assertIn("config exists only in another checkout", primary.stderr)
        self.assertIn(str(project_wt.resolve()), primary.stderr)
        worker = repo.worktree("worker")
        from_worker = run_cli("project", "--repo", str(worker), expect=2)
        self.assertIn(str(project_wt.resolve()), from_worker.stderr)

    def test_disallowed_model_rejected_by_project_and_start(self):
        trap, marker = make_pi_trap(self.tmp / "bin")
        cases = {
            "openai-codex/gpt-6-luna": "only permits",
            "gpt-4o": "only permits",
            "deepseek-flash": "only permits",
            " deepseek/deepseek-flash": "only permits",
            "deepseek/deepseek-flash ": "only permits",
            "": "non-empty",
        }
        for index, (bad, expected) in enumerate(cases.items()):
            with self.subTest(model=bad):
                repo = Repo(self.tmp, name=f"badmodel-{index}", config=default_config(model=bad))
                env = base_env(PI_BIN=str(trap))
                project = run_cli("project", "--repo", str(repo.root), env=env, expect=2)
                self.assertIn(expected, project.stderr)
                worktree = repo.worktree("wt")
                started = repo.start("bad", worktree, env=env, expect=2)
                self.assertIn(expected, started.stderr)
                self.assertFalse(repo.task_dir("bad").exists())
        self.assertFalse(marker.exists())

    def test_actual_user_project_paths_are_read_only(self):
        project = Path("/Users/jlpeng/works/eugl-bake-pos-recovery-bake03")
        primary = Path("/Users/jlpeng/works/eugl-bake-pos-recovery")
        if not (project.is_dir() and (project / ".agents" / "codex-pi.json").is_file()
                and primary.is_dir()):
            self.skipTest("authorized user project paths are not present")
        trap, marker = make_pi_trap(self.tmp / "bin")
        env = base_env(PI_BIN=str(trap))
        data = cli_json("project", "--repo", str(project), env=env)
        self.assertEqual(data["repo"], str(project.resolve()))
        self.assertEqual(data["config"]["model"], "deepseek/deepseek-flash")
        self.assertEqual(data["limits"]["allowedModels"], ["deepseek/deepseek-flash"])
        primary_proc = run_cli("project", "--repo", str(primary), env=env, expect=None)
        self.assertIn(primary_proc.returncode, (0, 2))
        if primary_proc.returncode == 0:
            primary_data = json.loads(primary_proc.stdout)
            self.assertEqual(primary_data["repo"], str(primary.resolve()))
            self.assertNotEqual(primary_data["configPath"], data["configPath"])
        else:
            self.assertTrue("codex-pi.json" in primary_proc.stderr
                            or "only permits" in primary_proc.stderr,
                            primary_proc.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
