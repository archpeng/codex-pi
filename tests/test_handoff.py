"""Behavioral tests for the synchronous Codex Stop handoff hook.

Every test runs the real ``runtime/pi_handoff.py`` CLI as a subprocess against a
real temporary git repository and the real detached lifecycle worker with the
offline ``PI_BIN`` double. No Codex binary and no model are used anywhere.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from runtime_helpers import (ROOT, RUNTIME, Repo, base_env, cleanup_repos,
                             default_config, kill_pid, make_pi_trap, wait_gone)

HANDOFF = RUNTIME / "pi_handoff.py"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"


def handoff_root(tmp: Path) -> Path:
    return Path(tmp) / "handoffs"


def h_env(tmp: Path, **extra) -> dict:
    env = base_env(**extra)
    env["CODEX_PI_HANDOFF_ROOT"] = str(handoff_root(tmp))
    env.pop("CODEX_THREAD_ID", None)
    return env


def run_handoff(*args, env: dict, expect: int | None = 0, timeout: float = 90,
                stdin: str | None = None, cwd=None) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, str(HANDOFF), *[str(arg) for arg in args]],
                          input=stdin, capture_output=True, text=True, env=env,
                          timeout=timeout, cwd=str(cwd) if cwd else None)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(f"pi_handoff {' '.join(str(arg) for arg in args)} exited "
                             f"{proc.returncode}, expected {expect}\n"
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def handoff_json(*args, env: dict, timeout: float = 90, cwd=None,
                 expect: int | None = 0) -> dict:
    proc = run_handoff(*args, env=env, expect=expect, timeout=timeout, cwd=cwd)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise AssertionError(f"pi_handoff did not return JSON: {exc}\nstdout={proc.stdout}") from exc


def ack_event(key: str, session: str, env: dict) -> dict:
    return handoff_json("ack", "--event-key", key, "--session-id", session, env=env)


def hook_payload(name: str, session: str, active: bool = False) -> str:
    return json.dumps({"hook_event_name": name, "session_id": session,
                       "cwd": "/tmp/unrelated-cwd", "turn_id": "turn-1",
                       "stop_hook_active": active, "model": "gpt-6-test",
                       "permission_mode": "default", "transcript_path": None,
                       "last_assistant_message": None})


def run_hook(name: str, session: str, tmp: Path, env: dict, active: bool = False,
             timeout: float = 90) -> dict:
    proc = run_handoff("hook", env=env, timeout=timeout, cwd=tmp,
                       stdin=hook_payload(name, session, active))
    return json.loads(proc.stdout)


def start_hook(name: str, session: str, tmp: Path, env: dict, active: bool = False):
    return subprocess.Popen([sys.executable, str(HANDOFF), "hook"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp))


def binding_file(tmp: Path, key: str) -> Path:
    return handoff_root(tmp) / "bindings" / f"{key}.json"


def binding_lock_file(tmp: Path, key: str) -> Path:
    return handoff_root(tmp) / "bindings" / f"{key}.lock"


def read_binding(tmp: Path, key: str) -> dict:
    return json.loads(binding_file(tmp, key).read_text(encoding="utf-8"))


def session_marker(tmp: Path, session: str) -> Path:
    return handoff_root(tmp) / "sessions" / (hashlib.sha256(session.encode()).hexdigest() + ".json")


def latest_state(repo, task: str) -> dict:
    rounds = sorted((repo.task_dir(task) / "rounds").glob("*"))
    return json.loads((rounds[-1] / "round.state.json").read_text(encoding="utf-8"))


def wait_pi_pid(repo, task: str, timeout: float = 10) -> int:
    deadline = time.monotonic() + timeout
    while True:
        state = latest_state(repo, task)
        if isinstance(state.get("piPid"), int):
            return state["piPid"]
        if time.monotonic() >= deadline:
            raise AssertionError(f"piPid was never recorded for {task}")
        time.sleep(0.05)


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-handoff-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        cleanup_repos()

    def make(self, config: dict | None = None, name: str = "repo"):
        repo = Repo(self.tmp, name=name, config=config if config is not None
                    else default_config())
        worktree = repo.worktree("wt")
        return repo, worktree

    def arm(self, repo, session: str, tmp: Path, env: dict, task: str = "alpha",
            round_number: int = 1, expect: int | None = 0, resume: bool = False):
        args = ["arm", "--repo", str(repo.root), "--task", task,
                "--round", str(round_number), "--session-id", session]
        if resume:
            args.append("--resume")
        return run_handoff(*args, env=env, expect=expect)

    def legacy_wait(self, key: str, seconds: float = 14400) -> None:
        """Rewrite a fresh record into a legacy 0.2 binding with a long wait."""
        data = read_binding(self.tmp, key)
        data["waitSeconds"] = seconds
        binding_file(self.tmp, key).write_text(json.dumps(data), encoding="utf-8")

    # ------------------------------------------------------------------
    def test_quick_stop_never_waits_and_delivers_once_after_terminal(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="1.5")
        repo.start("alpha", worktree, env=env)
        repo.wait_round_state("alpha", "running")
        arm = json.loads(self.arm(repo, "session-a", self.tmp, env).stdout)
        key = arm["eventKey"]
        self.assertEqual(arm["binding"]["state"], "armed")
        self.assertEqual(read_binding(self.tmp, key)["waitSeconds"], 0)
        self.legacy_wait(key)

        started = time.monotonic()
        pending = run_hook("Stop", "session-a", self.tmp, env, timeout=20)
        elapsed = time.monotonic() - started
        self.assertEqual(pending, {})
        self.assertLess(elapsed, 5, "a live worker and legacy four-hour wait must not make Stop wait")
        self.assertEqual(read_binding(self.tmp, key)["state"], "armed",
                         "a pending event stays armed instead of expiring")
        os.kill(wait_pi_pid(repo, "alpha"), 0)

        result = repo.wait_terminal("alpha", env=env)
        self.assertEqual(result["state"], "completed")

        processes = [start_hook("Stop", "session-a", self.tmp, env) for _ in range(2)]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(
                hook_payload("Stop", "session-a"), timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        blocks = [item for item in outputs if item.get("decision") == "block"]
        self.assertEqual(len(blocks), 1, outputs)
        reason = blocks[0]["reason"]
        self.assertIn(f"event={key}", reason)
        self.assertIn("task=alpha", reason)
        self.assertIn("round=1", reason)
        self.assertIn("terminal_state=completed", reason)
        self.assertIn("exit_code=0", reason)
        self.assertIn(f"ack --event-key {key}", reason)
        self.assertNotIn("test double finished", reason, "Pi output must never reach the reason")

        repeated = run_hook("Stop", "session-a", self.tmp, env, active=True)
        self.assertEqual(repeated, {})
        result = repo.wait_terminal("alpha", env=env)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["acceptance"], "not_verified")

        first = ack_event(key, "session-a", env)
        self.assertTrue(first["ok"])
        self.assertFalse(first["alreadyAcked"])
        second = ack_event(key, "session-a", env)
        self.assertTrue(second["alreadyAcked"])
        self.assertEqual(first["ackAt"], second["ackAt"])

    def test_failed_pi_returns_failure_event_once(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="fail")
        repo.start("beta", worktree, env=env)
        repo.wait_terminal("beta", env=env)
        arm = json.loads(self.arm(repo, "session-b", self.tmp, env, task="beta").stdout)
        first = run_hook("Stop", "session-b", self.tmp, env)
        self.assertEqual(first.get("decision"), "block")
        self.assertIn("terminal_state=failed", first["reason"])
        self.assertIn("exit_code=3", first["reason"])
        self.assertEqual(run_hook("Stop", "session-b", self.tmp, env), {})
        ack_event(arm["eventKey"], "session-b", env)

    def test_pending_result_before_arm_delivers_once(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("gamma", worktree, env=env)
        result = repo.wait_terminal("gamma", env=env)
        self.assertEqual(result["state"], "completed")
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "gamma",
                             "--round", "1", "--session-id", "session-c", env=env)
        first = run_hook("Stop", "session-c", self.tmp, env)
        self.assertEqual(first.get("decision"), "block")
        self.assertEqual(run_hook("Stop", "session-c", self.tmp, env), {})
        ack_event(armed["eventKey"], "session-c", env)

    def test_worker_timeout_is_delivered_as_terminal_event(self):
        repo, worktree = self.make(default_config(timeoutSeconds=1))
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("timeout", worktree, env=env)
        repo.wait_terminal("timeout", env=env, timeout=25)
        self.arm(repo, "session-t", self.tmp, env, task="timeout")
        output = run_hook("Stop", "session-t", self.tmp, env)
        self.assertEqual(output.get("decision"), "block")
        self.assertIn("terminal_state=timed_out", output["reason"])
        self.assertIn("exit_code=124", output["reason"])
        self.assertIn("timed_out=True", output["reason"])
        result = repo.wait_terminal("timeout", env=env)
        self.assertEqual(result["state"], "timed_out")

    def test_pending_stop_leaves_pi_alive_and_event_armed(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("waiting", worktree, env=env)
        repo.wait_round_state("waiting", "running")
        armed = json.loads(self.arm(repo, "session-w", self.tmp, env, task="waiting").stdout)
        started = time.monotonic()
        output = run_hook("Stop", "session-w", self.tmp, env, timeout=20)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertEqual(output, {})
        binding = read_binding(self.tmp, armed["eventKey"])
        self.assertEqual(binding["state"], "armed")
        self.assertIsNone(binding.get("lastWaitExpiredAt"))
        status = handoff_json("status", "--event-key", armed["eventKey"], env=env)
        item = status["bindings"][0]
        self.assertEqual(item["state"], "armed")
        self.assertTrue(item["evidence"]["taskDir"].endswith("tasks/waiting"))
        os.kill(wait_pi_pid(repo, "waiting"), 0)
        repo.cancel("waiting", env=env)
        self.assertEqual(repo.wait_terminal("waiting", env=env, timeout=25)["state"], "cancelled")
        delivered = run_hook("Stop", "session-w", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")
        self.assertIn("terminal_state=cancelled", delivered["reason"])
        self.assertTrue(read_binding(self.tmp, armed["eventKey"])["delivery"]["cancelled"])
        ack_event(armed["eventKey"], "session-w", env)

    def test_arm_rejects_positive_wait_seconds_with_actionable_message(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("nowait", worktree, env=env)
        repo.wait_round_state("nowait", "running")
        blocked = run_handoff("arm", "--repo", str(repo.root), "--task", "nowait",
                              "--round", "1", "--session-id", "session-nw",
                              "--wait-seconds", "60", env=env, expect=2)
        self.assertIn("positive Stop wait window is no longer supported", blocked.stderr)
        self.assertIn("bounded pi_task.py wait/status", blocked.stderr)
        self.assertEqual(list((handoff_root(self.tmp) / "bindings").glob("*.json")), [])
        os.kill(wait_pi_pid(repo, "nowait"), 0)
        repo.cancel("nowait", env=env)
        self.assertEqual(repo.wait_terminal("nowait", env=env, timeout=25)["state"], "cancelled")

    def test_release_is_owner_scoped_idempotent_and_old_loop_compatible(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("release", worktree, env=env)
        repo.wait_round_state("release", "running")
        armed = json.loads(self.arm(repo, "session-rel", self.tmp, env, task="release").stdout)
        key = armed["eventKey"]
        self.legacy_wait(key)

        wrong = run_handoff("release", "--event-key", key, "--session-id", "session-other",
                            env=env, expect=2)
        self.assertIn("does not match", wrong.stderr)
        self.assertEqual(read_binding(self.tmp, key)["generation"], 1)
        missing = run_handoff("release", "--event-key", key, env=env, expect=2)
        self.assertIn("event owner session", missing.stderr)

        # The 0.2 loop conditions are generation change or state change: a real
        # polling observer must exit within its poll interval after release.
        observer_script = (
            "import json, sys, time\n"
            "path = sys.argv[1]\n"
            "started = time.monotonic()\n"
            "while True:\n"
            "    data = json.load(open(path))\n"
            "    if data.get('generation') != 1 or data.get('state') != 'armed':\n"
            "        print(json.dumps({'elapsed': time.monotonic() - started,\n"
            "                          'generation': data.get('generation'),\n"
            "                          'state': data.get('state')}))\n"
            "        break\n"
            "    time.sleep(0.1)\n")
        observer = subprocess.Popen(
            [sys.executable, "-c", observer_script, str(binding_file(self.tmp, key))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.3)
        released = handoff_json("release", "--event-key", key, "--session-id", "session-rel", env=env)
        stdout, stderr = observer.communicate(timeout=10)
        self.assertEqual(observer.returncode, 0, stderr)
        observed = json.loads(stdout)
        self.assertLess(observed["elapsed"], 3, "the waiting hook must observe release within a poll interval")
        self.assertEqual(observed["generation"], 2)
        self.assertEqual(observed["state"], "suspended")

        self.assertFalse(released["alreadyReleased"])
        binding = read_binding(self.tmp, key)
        self.assertEqual(binding["state"], "suspended")
        self.assertEqual(binding["releaseReason"], "released for bounded tool waiting")
        self.assertEqual(binding["lastError"], "released for bounded tool waiting")
        self.assertEqual(binding["generation"], 2)
        self.assertEqual(binding["task"], "release")
        self.assertEqual(binding["round"], 1)
        self.assertFalse(session_marker(self.tmp, "session-rel").exists(),
                         "release must not forge a user interrupt marker")
        os.kill(wait_pi_pid(repo, "release"), 0)  # release never cancels Pi

        duplicate = handoff_json("release", "--event-key", key, "--session-id", "session-rel", env=env)
        self.assertTrue(duplicate["alreadyReleased"])
        self.assertEqual(read_binding(self.tmp, key)["generation"], 2,
                         "duplicate release must not bump generation again")
        # A released event stays suspended: Stop neither delivers nor expires it.
        self.assertEqual(run_hook("Stop", "session-rel", self.tmp, env), {})
        self.assertEqual(read_binding(self.tmp, key)["state"], "suspended")
        # Explicit resume uses the CAS generation path and bumps again.
        resumed = json.loads(self.arm(repo, "session-rel", self.tmp, env, task="release", resume=True).stdout)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(read_binding(self.tmp, key)["generation"], 3)
        self.assertEqual(read_binding(self.tmp, key)["state"], "armed")
        repo.cancel("release", env=env)
        self.assertEqual(repo.wait_terminal("release", env=env, timeout=25)["state"], "cancelled")
        delivered = run_hook("Stop", "session-rel", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")
        ack_event(key, "session-rel", env)

    def test_release_refuses_terminal_delivered_and_acked_records(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("releasedone", worktree, env=env)
        repo.wait_terminal("releasedone", env=env)
        armed = json.loads(self.arm(repo, "session-rd", self.tmp, env, task="releasedone").stdout)
        key = armed["eventKey"]
        data = read_binding(self.tmp, key)
        data["state"] = "needs_recovery"
        data["lastError"] = "synthetic recovery evidence"
        binding_file(self.tmp, key).write_text(json.dumps(data), encoding="utf-8")
        early = run_handoff("release", "--event-key", key, "--session-id", "session-rd",
                            env=env, expect=2)
        self.assertIn("release only migrates an armed", early.stderr)
        self.assertEqual(read_binding(self.tmp, key)["state"], "needs_recovery")
        data["state"] = "armed"
        data["lastError"] = None
        binding_file(self.tmp, key).write_text(json.dumps(data), encoding="utf-8")

        delivered = run_hook("Stop", "session-rd", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")
        blocked = run_handoff("release", "--event-key", key, "--session-id", "session-rd",
                              env=env, expect=2)
        self.assertIn("must not be erased", blocked.stderr)
        self.assertEqual(read_binding(self.tmp, key)["state"], "delivered")
        self.assertIsNotNone(read_binding(self.tmp, key)["deliveredAt"])

        first = ack_event(key, "session-rd", env)
        blocked = run_handoff("release", "--event-key", key, "--session-id", "session-rd",
                              env=env, expect=2)
        self.assertIn("must not be erased", blocked.stderr)
        binding = read_binding(self.tmp, key)
        self.assertEqual(binding["state"], "acked")
        self.assertEqual(binding["ackedAt"], first["ackAt"])

    def test_resumed_generation_is_not_demoted_by_quick_stop(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("genstop", worktree, env=env)
        repo.wait_round_state("genstop", "running")
        armed = json.loads(self.arm(repo, "session-gen", self.tmp, env, task="genstop").stdout)
        key = armed["eventKey"]
        self.assertEqual(run_hook("Stop", "session-gen", self.tmp, env), {})
        self.assertEqual(read_binding(self.tmp, key)["state"], "armed")
        self.assertEqual(read_binding(self.tmp, key)["generation"], 1)
        handoff_json("release", "--event-key", key, "--session-id", "session-gen", env=env)
        self.assertEqual(read_binding(self.tmp, key)["generation"], 2)
        self.arm(repo, "session-gen", self.tmp, env, task="genstop", resume=True)
        self.assertEqual(read_binding(self.tmp, key)["generation"], 3)
        self.assertEqual(read_binding(self.tmp, key)["state"], "armed")
        self.assertEqual(run_hook("Stop", "session-gen", self.tmp, env), {})
        binding = read_binding(self.tmp, key)
        self.assertEqual(binding["state"], "armed", "a quick Stop must not demote a resumed generation")
        self.assertEqual(binding["generation"], 3)
        repo.cancel("genstop", env=env)
        self.assertEqual(repo.wait_terminal("genstop", env=env, timeout=25)["state"], "cancelled")

    def test_quick_stop_then_interrupt_suspends_while_pi_continues(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("interrupting", worktree, env=env)
        repo.wait_round_state("interrupting", "running")
        armed = json.loads(self.arm(repo, "session-i", self.tmp, env, task="interrupting").stdout)
        started = time.monotonic()
        output = run_hook("Stop", "session-i", self.tmp, env, timeout=20)
        self.assertLess(time.monotonic() - started, 5, "Stop must not wait for the live worker")
        self.assertEqual(output, {})
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "armed")
        started = time.monotonic()
        interrupt = run_hook("Interrupt", "session-i", self.tmp, env)
        self.assertEqual(interrupt, {})
        self.assertLess(time.monotonic() - started, 5, "Interrupt hook must return promptly")
        binding = read_binding(self.tmp, armed["eventKey"])
        self.assertEqual(binding["state"], "suspended")
        self.assertIn("interrupted", binding["lastError"] or "")
        os.kill(wait_pi_pid(repo, "interrupting"), 0)  # Pi keeps running after a user interrupt
        repo.cancel("interrupting", env=env)
        self.assertEqual(repo.wait_terminal("interrupting", env=env, timeout=25)["state"],
                         "cancelled")

    def test_wrong_session_and_unknown_binding_are_safe_noops(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("wrong", worktree, env=env)
        repo.wait_terminal("wrong", env=env)
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "wrong",
                             "--round", "1", "--session-id", "session-right", env=env)
        self.assertEqual(run_hook("Stop", "session-wrong", self.tmp, env), {})
        self.assertEqual(run_hook("Stop", "session-nobody", self.tmp, env), {})
        binding = read_binding(self.tmp, armed["eventKey"])
        self.assertEqual(binding["state"], "armed")
        delivered = run_hook("Stop", "session-right", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")
        self.assertEqual(run_hook("Stop", "session-wrong", self.tmp, env), {})

    def test_corrupt_and_wrong_repo_bindings_fail_safe(self):
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")

        repo, worktree = self.make(name="repo-corrupt")
        repo.start("corrupt", worktree, env=env)
        repo.wait_terminal("corrupt", env=env)
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "corrupt",
                             "--round", "1", "--session-id", "session-x", env=env)
        binding_file(self.tmp, armed["eventKey"]).write_text("{not json", encoding="utf-8")
        corrupt = run_hook("Stop", "session-x", self.tmp, env)
        self.assertNotIn("decision", corrupt)
        self.assertIn("corrupt", corrupt.get("systemMessage", ""))

        repo2, worktree2 = self.make(name="repo-rewrite")
        repo2.start("rewrite", worktree2, env=env)
        repo2.wait_terminal("rewrite", env=env)
        armed2 = handoff_json("arm", "--repo", str(repo2.root), "--task", "rewrite",
                              "--round", "1", "--session-id", "session-y", env=env)
        data = read_binding(self.tmp, armed2["eventKey"])
        data["repo"] = "/definitely/missing/repo"
        binding_file(self.tmp, armed2["eventKey"]).write_text(json.dumps(data), encoding="utf-8")
        rekeyed = run_hook("Stop", "session-y", self.tmp, env)
        self.assertNotIn("decision", rekeyed)
        self.assertIn("invalid binding", rekeyed.get("systemMessage", ""))

        repo3, worktree3 = self.make(name="repo-gone")
        repo3.start("gone", worktree3, env=env)
        repo3.wait_terminal("gone", env=env)
        handoff_json("arm", "--repo", str(repo3.root), "--task", "gone",
                     "--round", "1", "--session-id", "session-z2", env=env)
        shutil.rmtree(repo3.root)
        gone = run_hook("Stop", "session-z2", self.tmp, env)
        self.assertNotIn("decision", gone)
        self.assertIn("repository path is unavailable", gone.get("systemMessage", ""))

    def test_arm_refuses_unknown_task_round_and_stale_round(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        missing = run_handoff("arm", "--repo", str(repo.root), "--task", "missing",
                              "--round", "1", "--session-id", "s", env=env, expect=2)
        self.assertIn("unknown task", missing.stderr)
        repo.start("staling", worktree, env=env)
        repo.wait_terminal("staling", env=env)
        unknown_round = run_handoff("arm", "--repo", str(repo.root), "--task", "staling",
                                    "--round", "9", "--session-id", "s", env=env, expect=2)
        self.assertIn("unknown round", unknown_round.stderr)
        repo.continue_task("staling", env=env)
        repo.wait_terminal("staling", env=env)
        stale = run_handoff("arm", "--repo", str(repo.root), "--task", "staling",
                            "--round", "1", "--session-id", "s", env=env, expect=2)
        self.assertIn("stale", stale.stderr)
        latest = run_handoff("arm", "--repo", str(repo.root), "--task", "staling",
                             "--round", "2", "--session-id", "s", env=env, expect=0)
        self.assertTrue(json.loads(latest.stdout)["ok"])

    def test_arm_is_idempotent_and_refuses_live_rebind_across_sessions(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("rebind", worktree, env=env)
        repo.wait_terminal("rebind", env=env)
        first = json.loads(self.arm(repo, "session-one", self.tmp, env, task="rebind").stdout)
        again = json.loads(self.arm(repo, "session-one", self.tmp, env, task="rebind").stdout)
        self.assertTrue(again["idempotent"])
        self.assertEqual(first["eventKey"], again["eventKey"])
        self.assertEqual(len(list((handoff_root(self.tmp) / "bindings").glob("*.json"))), 1)
        blocked = self.arm(repo, "session-two", self.tmp, env, task="rebind", expect=2)
        self.assertIn("belongs to session", blocked.stderr)

        delivered = run_hook("Stop", "session-one", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")
        still_live = self.arm(repo, "session-two", self.tmp, env, task="rebind", expect=2)
        self.assertIn("ack it before rebinding", still_live.stderr)
        ack_event(first["eventKey"], "session-one", env)
        rebound = json.loads(self.arm(repo, "session-two", self.tmp, env, task="rebind").stdout)
        self.assertNotEqual(rebound["eventKey"], first["eventKey"])
        self.assertFalse(rebound["idempotent"])

    def test_concurrent_arms_same_round_have_one_winner(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("race-arm", worktree, env=env)
        repo.wait_terminal("race-arm", env=env)
        processes = [subprocess.Popen(
            [sys.executable, str(HANDOFF), "arm", "--repo", str(repo.root), "--task", "race-arm",
             "--round", "1", "--session-id", session],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for session in ("race-one", "race-two")]
        results = [process.communicate(timeout=60) for process in processes]
        codes = sorted(process.returncode for process in processes)
        self.assertEqual(codes, [0, 2])
        loser = results[1][1] if processes[0].returncode == 0 else results[0][1]
        self.assertTrue("live handoff" in loser or "--resume" in loser, loser)
        files = list((handoff_root(self.tmp) / "bindings").glob("*.json"))
        self.assertEqual(len(files), 1, "concurrent arms must never write two live bindings")

    def test_new_round_eligible_after_prior_delivery_and_ack(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("rounds", worktree, env=env)
        repo.wait_terminal("rounds", env=env)
        one = handoff_json("arm", "--repo", str(repo.root), "--task", "rounds", "--round", "1",
                           "--session-id", "session-r", env=env)
        first = run_hook("Stop", "session-r", self.tmp, env)
        self.assertEqual(first.get("decision"), "block")
        ack_event(one["eventKey"], "session-r", env)
        repo.continue_task("rounds", env=env)
        repo.wait_terminal("rounds", env=env)
        two = handoff_json("arm", "--repo", str(repo.root), "--task", "rounds", "--round", "2",
                           "--session-id", "session-r", env=env)
        self.assertNotEqual(one["eventKey"], two["eventKey"])
        second = run_hook("Stop", "session-r", self.tmp, env, active=True)
        self.assertEqual(second.get("decision"), "block",
                         "a new explicitly armed round stays eligible during stop_hook_active")
        self.assertIn("round=2", second["reason"])
        ack_event(two["eventKey"], "session-r", env)
        status = handoff_json("status", "--all", env=env)
        self.assertEqual({item["state"] for item in status["bindings"]}, {"acked"})

    def test_ack_requires_session_and_delivery(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("acking", worktree, env=env)
        repo.wait_terminal("acking", env=env)
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "acking", "--round", "1",
                             "--session-id", "session-a", env=env)

        early = run_handoff("ack", "--event-key", armed["eventKey"], "--session-id", "session-a",
                            env=env, expect=2)
        self.assertIn("not delivered", early.stderr)
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "armed")

        delivered = run_hook("Stop", "session-a", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")

        env_wrong = dict(env)
        env_wrong["CODEX_THREAD_ID"] = "session-wrong"
        wrong = run_handoff("ack", "--event-key", armed["eventKey"], env=env_wrong, expect=2)
        self.assertIn("does not match", wrong.stderr)
        missing = run_handoff("ack", "--event-key", armed["eventKey"], env=env, expect=2)
        self.assertIn("resolved session", missing.stderr)
        run_handoff("ack", "--event-key", "0" * 64, "--session-id", "session-a",
                    env=env, expect=2)
        run_handoff("ack", "--repo", str(repo.root), "--task", "acking", "--round", "2",
                    "--session-id", "session-a", env=env, expect=2)
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "delivered",
                         "rejected acks must not suppress or corrupt the delivered event")

        first = ack_event(armed["eventKey"], "session-a", env)
        self.assertFalse(first["alreadyAcked"])
        second = ack_event(armed["eventKey"], "session-a", env)
        self.assertTrue(second["alreadyAcked"])
        self.assertEqual(first["binding"]["ackedAt"], second["binding"]["ackedAt"])

    def test_invalid_records_never_deliver(self):
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")

        repo, worktree = self.make(name="repo-schema")
        repo.start("schema", worktree, env=env)
        repo.wait_terminal("schema", env=env)
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "schema", "--round", "1",
                             "--session-id", "session-iv1", env=env)
        data = read_binding(self.tmp, armed["eventKey"])
        data["schemaVersion"] = 2
        binding_file(self.tmp, armed["eventKey"]).write_text(json.dumps(data), encoding="utf-8")
        out = run_hook("Stop", "session-iv1", self.tmp, env)
        self.assertNotIn("decision", out)
        self.assertIn("schemaVersion", out.get("systemMessage", ""))
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "needs_recovery")

        repo2, worktree2 = self.make(name="repo-rekey")
        repo2.start("rekey", worktree2, env=env)
        repo2.wait_terminal("rekey", env=env)
        armed2 = handoff_json("arm", "--repo", str(repo2.root), "--task", "rekey", "--round", "1",
                              "--session-id", "session-iv2", env=env)
        data2 = read_binding(self.tmp, armed2["eventKey"])
        data2["eventKey"] = "d" * 64
        binding_file(self.tmp, armed2["eventKey"]).write_text(json.dumps(data2), encoding="utf-8")
        out2 = run_hook("Stop", "session-iv2", self.tmp, env)
        self.assertNotIn("decision", out2)
        self.assertIn("filename", out2.get("systemMessage", ""))

        repo3, worktree3 = self.make(name="repo-frozen")
        repo3.start("frozen", worktree3, env=env)
        repo3.wait_terminal("frozen", env=env)
        armed3 = handoff_json("arm", "--repo", str(repo3.root), "--task", "frozen", "--round", "1",
                              "--session-id", "session-iv3", env=env)
        valid = read_binding(self.tmp, armed3["eventKey"])
        task_json = repo3.task_dir("frozen") / "task.json"
        frozen = json.loads(task_json.read_text(encoding="utf-8"))
        frozen["repo"] = str(self.tmp / "elsewhere")
        task_json.write_text(json.dumps(frozen), encoding="utf-8")
        out3 = run_hook("Stop", "session-iv3", self.tmp, env)
        self.assertNotIn("decision", out3)
        self.assertIn("frozen repository", out3.get("systemMessage", ""))

        # A copied record under another filename is surfaced as invalid evidence.
        copied = "e" * 64
        binding_file(self.tmp, copied).write_text(json.dumps(valid), encoding="utf-8")
        status = handoff_json("status", "--all", env=env)
        invalid = [item for item in status["bindings"] if item.get("state") == "invalid"]
        self.assertTrue(any(item["eventKey"] == copied for item in invalid), status)

    def test_recovery_surface_on_user_prompt_and_no_auto_redelivery(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("recover", worktree, env=env)
        repo.wait_terminal("recover", env=env)
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "recover", "--round", "1",
                             "--session-id", "session-u", env=env)
        first = run_hook("Stop", "session-u", self.tmp, env)
        self.assertEqual(first.get("decision"), "block")
        self.assertEqual(run_hook("Stop", "session-u", self.tmp, env), {},
                         "a delivered event must not be re-notified")
        recovery = run_hook("UserPromptSubmit", "session-u", self.tmp, env)
        self.assertNotIn("decision", recovery)
        context = recovery["hookSpecificOutput"]["additionalContext"]
        self.assertIn(armed["eventKey"], context)
        self.assertIn("delivered", context)
        ack_event(armed["eventKey"], "session-u", env)
        self.assertEqual(run_hook("UserPromptSubmit", "session-u", self.tmp, env), {})

    def test_explicit_rearm_resumes_suspended_event(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("resume", worktree, env=env)
        repo.wait_round_state("resume", "running")
        armed = json.loads(self.arm(repo, "session-e", self.tmp, env, task="resume").stdout)
        self.assertEqual(run_hook("Stop", "session-e", self.tmp, env), {})
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "armed")
        self.assertEqual(run_hook("Interrupt", "session-e", self.tmp, env), {})
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "suspended")

        blocked = self.arm(repo, "session-e", self.tmp, env, task="resume", expect=2)
        self.assertIn("--resume", blocked.stderr)
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "suspended")
        resumed = json.loads(self.arm(repo, "session-e", self.tmp, env, task="resume", resume=True).stdout)
        self.assertFalse(resumed["idempotent"])
        self.assertTrue(resumed["resumed"])
        binding = read_binding(self.tmp, armed["eventKey"])
        self.assertEqual(binding["state"], "armed")
        self.assertEqual(binding["rearmCount"], 1)
        repo.cancel("resume", env=env)
        self.assertEqual(repo.wait_terminal("resume", env=env, timeout=25)["state"], "cancelled")
        delivered = run_hook("Stop", "session-e", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")
        self.assertIn("terminal_state=cancelled", delivered["reason"])
        ack_event(armed["eventKey"], "session-e", env)

    def test_interrupt_is_prompt_with_contended_binding_locks(self):
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        keys = []
        for name, task in (("repo-lock-a", "lock-a"), ("repo-lock-b", "lock-b")):
            repo, worktree = self.make(name=name)
            repo.start(task, worktree, env=env)
            repo.wait_terminal(task, env=env)
            armed = handoff_json("arm", "--repo", str(repo.root), "--task", task, "--round", "1",
                                 "--session-id", "session-lock", env=env)
            keys.append(armed["eventKey"])
        handles = []
        try:
            for key in keys:
                stream = binding_lock_file(self.tmp, key).open("a+")
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                handles.append(stream)
            started = time.monotonic()
            self.assertEqual(run_hook("Interrupt", "session-lock", self.tmp, env), {})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, "Interrupt must not wait on contended binding locks")
            self.assertTrue(session_marker(self.tmp, "session-lock").is_file())
            for key in keys:
                self.assertEqual(read_binding(self.tmp, key)["state"], "armed",
                                 "contended bindings stay recoverable until locks release")
        finally:
            for stream in handles:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()
        self.assertEqual(run_hook("Interrupt", "session-lock", self.tmp, env), {})
        for key in keys:
            self.assertEqual(read_binding(self.tmp, key)["state"], "suspended")

    def test_ready_second_task_delivered_while_first_hangs(self):
        repo_a, worktree_a = self.make(name="repo-multi-a")
        repo_b, worktree_b = self.make(name="repo-multi-b")
        env_hang = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        env_ok = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo_a.start("hang", worktree_a, env=env_hang)
        repo_a.wait_round_state("hang", "running")
        repo_b.start("ready", worktree_b, env=env_ok)
        repo_b.wait_terminal("ready", env=env_ok)

        first = json.loads(self.arm(repo_a, "session-m", self.tmp, env_ok, task="hang").stdout)
        second = json.loads(self.arm(repo_b, "session-m", self.tmp, env_ok, task="ready").stdout)
        started = time.monotonic()
        output = run_hook("Stop", "session-m", self.tmp, env_ok, timeout=30)
        elapsed = time.monotonic() - started
        self.assertEqual(output.get("decision"), "block", output)
        self.assertIn("task=ready", output["reason"])
        self.assertLess(elapsed, 10, "a ready second task must not be starved by a hanging first")
        self.assertEqual(read_binding(self.tmp, first["eventKey"])["state"], "armed",
                         "the pending event must be preserved")
        ack_event(second["eventKey"], "session-m", env_ok)
        repo_a.cancel("hang", env=env_ok)
        self.assertEqual(repo_a.wait_terminal("hang", env=env_ok, timeout=25)["state"], "cancelled")

    def test_hooks_command_executes_through_shell_with_spaces(self):
        config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
        plugin_root = self.tmp / "plugin root with spaces"
        plugin_root.mkdir()
        os.symlink(RUNTIME, plugin_root / "runtime")
        resolved = command.replace("${PLUGIN_ROOT}", str(plugin_root))
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo, worktree = self.make()
        repo.start("shell", worktree, env=env)
        repo.wait_terminal("shell", env=env)
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "shell", "--round", "1",
                             "--session-id", "session-shell", env=env)

        def shell_hook(name, session, active=False):
            proc = subprocess.run(["/bin/sh", "-c", resolved],
                                  input=hook_payload(name, session, active),
                                  capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        self.assertEqual(shell_hook("Interrupt", "session-shell"), {})
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "suspended")
        recovery = shell_hook("UserPromptSubmit", "session-shell")
        self.assertNotIn("decision", recovery)
        self.assertIn(armed["eventKey"], recovery["hookSpecificOutput"]["additionalContext"])
        resumed = json.loads(self.arm(repo, "session-shell", self.tmp, env, task="shell",
                                      resume=True).stdout)
        self.assertTrue(resumed["resumed"])
        stop = shell_hook("Stop", "session-shell")
        self.assertEqual(stop.get("decision"), "block")
        ack_event(armed["eventKey"], "session-shell", env)

    def test_wait_zero_inspects_once_and_pending_stays_armed(self):
        env_ok = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo, worktree = self.make(name="repo-zero-ready")
        repo.start("ready0", worktree, env=env_ok)
        repo.wait_terminal("ready0", env=env_ok)
        armed = json.loads(self.arm(repo, "session-zero-ready", self.tmp, env_ok, task="ready0").stdout)
        delivered = run_hook("Stop", "session-zero-ready", self.tmp, env_ok, timeout=30)
        self.assertEqual(delivered.get("decision"), "block",
                         "one quick pass must still inspect an already-terminal round")
        ack_event(armed["eventKey"], "session-zero-ready", env_ok)

        env_hang = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo2, worktree2 = self.make(name="repo-zero-hang")
        repo2.start("hang0", worktree2, env=env_hang)
        repo2.wait_round_state("hang0", "running")
        armed2 = json.loads(self.arm(repo2, "session-zero-hang", self.tmp, env_hang, task="hang0").stdout)
        pending = run_hook("Stop", "session-zero-hang", self.tmp, env_hang, timeout=30)
        self.assertEqual(pending, {})
        self.assertEqual(read_binding(self.tmp, armed2["eventKey"])["state"], "armed")
        os.kill(wait_pi_pid(repo2, "hang0"), 0)
        repo2.cancel("hang0", env=env_hang)
        self.assertEqual(repo2.wait_terminal("hang0", env=env_hang, timeout=25)["state"],
                         "cancelled")
        delivered2 = run_hook("Stop", "session-zero-hang", self.tmp, env_hang)
        self.assertEqual(delivered2.get("decision"), "block")
        ack_event(armed2["eventKey"], "session-zero-hang", env_hang)

    def test_terminal_record_with_lingering_ownership_stays_armed(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("locks", worktree, env=env)
        repo.wait_round_state("locks", "running")
        state_path = repo.task_dir("locks") / "rounds" / "1" / "round.state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(state="completed", exitCode=0, endedAt=time.time(),
                     timedOut=False, cancelled=False)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        armed = json.loads(self.arm(repo, "session-lockhold", self.tmp, env, task="locks").stdout)
        output = run_hook("Stop", "session-lockhold", self.tmp, env, timeout=30)
        self.assertNotIn("decision", output,
                         "a terminal record still holding worker ownership must not continue")
        self.assertIn("ownership", output.get("systemMessage", ""))
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "armed",
                         "ownership-uncertain terminal rounds stay armed, not expired")
        repo.cancel("locks", env=env)
        self.assertEqual(repo.wait_terminal("locks", env=env, timeout=25)["state"], "cancelled")

    def test_running_record_without_supervisor_becomes_needs_recovery(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("orphan-sup", worktree, env=env)
        repo.wait_round_state("orphan-sup", "running")
        armed = json.loads(self.arm(repo, "session-orphan", self.tmp, env, task="orphan-sup").stdout)
        supervisor_pid = latest_state(repo, "orphan-sup")["supervisorPid"]
        pi_pid = wait_pi_pid(repo, "orphan-sup")
        kill_pid(supervisor_pid, signal.SIGKILL)
        try:
            output = run_hook("Stop", "session-orphan", self.tmp, env, timeout=30)
            self.assertNotIn("decision", output,
                             "an inherited child task lock is still uncertain ownership")
            self.assertIn("supervisor", output.get("systemMessage", ""))
            self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "needs_recovery")
        finally:
            kill_pid(pi_pid, signal.SIGKILL)
            self.assertTrue(wait_gone(pi_pid, 10))

    def test_unknown_record_delivery_is_refused(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("unknown", worktree, env=env)
        state = repo.wait_round_state("unknown", "running")
        armed = json.loads(self.arm(repo, "session-unknown", self.tmp, env, task="unknown").stdout)
        state_path = repo.task_dir("unknown") / "rounds" / "1" / "round.state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.update(state="unknown", exitCode=None, endedAt=time.time())
        state_path.write_text(json.dumps(state), encoding="utf-8")
        output = run_hook("Stop", "session-unknown", self.tmp, env, timeout=30)
        self.assertNotIn("decision", output)
        self.assertIn("unknown", output.get("systemMessage", ""))
        self.assertEqual(read_binding(self.tmp, armed["eventKey"])["state"], "needs_recovery")
        kill_pid(state.get("piPid"), signal.SIGKILL)
        kill_pid(state.get("supervisorPid"), signal.SIGKILL)
        wait_gone(state.get("piPid"), 10)

    def test_inflight_arm_honors_interrupt_during_arm(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("inflight", worktree, env=env)
        repo.wait_round_state("inflight", "running")
        root = handoff_root(self.tmp)
        root.mkdir(parents=True, exist_ok=True)
        admission = (root / ".admission.lock").open("a+")
        fcntl.flock(admission.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = None
        try:
            proc = subprocess.Popen([sys.executable, str(HANDOFF), "arm",
                                     "--repo", str(repo.root), "--task", "inflight",
                                     "--round", "1", "--session-id", "session-inflight"],
                                    env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            time.sleep(1.0)
            self.assertEqual(run_hook("Interrupt", "session-inflight", self.tmp, env), {})
        finally:
            fcntl.flock(admission.fileno(), fcntl.LOCK_UN)
            admission.close()
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["interrupted"])
        key = payload["eventKey"]
        binding = read_binding(self.tmp, key)
        self.assertEqual(binding["state"], "suspended")
        self.assertEqual(binding["observedInterruptGeneration"], 0)
        self.assertEqual(run_hook("Stop", "session-inflight", self.tmp, env), {})
        blocked = self.arm(repo, "session-inflight", self.tmp, env, task="inflight", expect=2)
        self.assertIn("--resume", blocked.stderr)
        repo.cancel("inflight", env=env)
        self.assertEqual(repo.wait_terminal("inflight", env=env, timeout=25)["state"], "cancelled")
        resumed = json.loads(self.arm(repo, "session-inflight", self.tmp, env, task="inflight",
                                      resume=True).stdout)
        self.assertTrue(resumed["resumed"])
        delivered = run_hook("Stop", "session-inflight", self.tmp, env)
        self.assertEqual(delivered.get("decision"), "block")
        ack_event(key, "session-inflight", env)

    def test_no_codex_and_no_pi_invocation_during_handoff(self):
        repo, worktree = self.make()
        codex_trap, codex_marker = make_pi_trap(self.tmp / "codexbin", name="codex")
        pi_trap, pi_marker = make_pi_trap(self.tmp / "pibin", name="pi-trap")
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        env["PATH"] = str(codex_trap.parent) + os.pathsep + env.get("PATH", "")
        repo.start("trap", worktree, env=env)
        repo.wait_terminal("trap", env=env)
        hand_env = dict(env)
        hand_env["PI_BIN"] = str(pi_trap)
        armed = handoff_json("arm", "--repo", str(repo.root), "--task", "trap", "--round", "1",
                             "--session-id", "session-z", env=hand_env)
        output = run_hook("Stop", "session-z", self.tmp, hand_env)
        self.assertEqual(output.get("decision"), "block")
        handoff_json("status", "--event-key", armed["eventKey"], env=hand_env)
        ack_event(armed["eventKey"], "session-z", hand_env)
        self.assertFalse(codex_marker.exists(), "the handoff invoked a codex executable")
        self.assertFalse(pi_marker.exists(), "the handoff launched a model process while waiting")

    def test_hooks_config_and_help_match_the_documented_contract(self):
        config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        stop = config["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(stop["type"], "command")
        self.assertEqual(stop["command"], 'python3 "${PLUGIN_ROOT}/runtime/pi_handoff.py" hook')
        self.assertFalse(stop["async"])
        self.assertLessEqual(stop["timeout"], 10, "the Stop hook must have a short hard timeout")
        interrupt = config["hooks"]["Interrupt"][0]["hooks"][0]
        self.assertEqual(interrupt["timeout"], 3)
        self.assertIn("UserPromptSubmit", config["hooks"])
        proc = run_handoff("--help", env=h_env(self.tmp))
        self.assertIn("arm", proc.stdout)
        self.assertIn("status", proc.stdout)
        self.assertIn("ack", proc.stdout)
        self.assertIn("release", proc.stdout)
        arm_help = run_handoff("arm", "--help", env=h_env(self.tmp))
        self.assertIn("--resume", arm_help.stdout)

    def test_status_without_selectors_reports_error(self):
        proc = run_handoff("status", env=h_env(self.tmp), expect=2)
        self.assertIn("selector", proc.stderr)
        listed = handoff_json("status", "--all", env=h_env(self.tmp))
        self.assertEqual(listed["count"], 0)


if __name__ == "__main__":
    unittest.main()
