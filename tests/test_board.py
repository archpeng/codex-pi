"""Behavioral tests for the opt-in board projection and pre-model gate.

Everything runs the real CLIs as subprocesses against real temporary git
repositories and the offline Pi double. No Codex binary, provider or model is
ever invoked; the no-Codex trap and an explicit PI_BIN trap cover the hook path.
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
import uuid
from pathlib import Path

from runtime_helpers import (RUNTIME, Repo, base_env, cleanup_repos, default_config,
                             make_pi_trap)

BOARD = RUNTIME / "pi_board.py"
HOOK = RUNTIME / "pi_handoff.py"
MODEL = "deepseek/deepseek-flash"


def h_env(tmp: Path, **extra) -> dict:
    env = base_env(**extra)
    env["CODEX_PI_HANDOFF_ROOT"] = str(Path(tmp) / "handoffs")
    env.pop("CODEX_THREAD_ID", None)
    return env


def run_board(*args, env: dict, expect: int | None = None, timeout: float = 60,
              stdin: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, str(BOARD), *[str(arg) for arg in args]],
                          input=stdin, capture_output=True, text=True, env=env, timeout=timeout)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(f"pi_board {' '.join(str(arg) for arg in args)} exited "
                             f"{proc.returncode}, expected {expect}\n"
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def board_json(*args, env: dict, timeout: float = 60) -> dict:
    proc = run_board(*args, env=env, expect=0, timeout=timeout)
    return json.loads(proc.stdout)


def run_hook(payload: dict, env: dict, timeout: float = 15,
             cmd: str = "hook") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOK), cmd], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=timeout)


def gate_payload(session: str, cwd, prompt) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(cwd),
            "prompt": prompt}


def envelope(automation_id: str, instructions: str, time_iso: str = "2026-09-26T00:00:00Z") -> str:
    return (f"<heartbeat>\n  <automation_id>{automation_id}</automation_id>\n"
            f"  <current_time_iso>{time_iso}</current_time_iso>\n  <instructions>\n"
            f"{instructions}\n  </instructions>\n</heartbeat>\n")


def synth_receipt(checks: Path, check_id: str, exit_code: int, *, timed_out=False,
                  resource_limit=None) -> Path:
    checks.mkdir(parents=True, exist_ok=True)
    log = checks / f"{check_id}-{uuid.uuid4().hex[:8]}.log"
    log.write_text("synthetic evidence\n", encoding="utf-8")
    receipt = checks / f"{check_id}-{uuid.uuid4().hex[:8]}.json"
    data = {"schema_version": 1, "id": check_id, "argv": ["synthetic"], "cwd": str(checks),
            "head": None, "dirty": None, "tracked_diff_sha256": None, "started_at": 1,
            "ended_at": 2, "exit_code": exit_code, "timed_out": timed_out, "error": None,
            "test_counts": None, "log": log.name,
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "acceptance": "not_verified"}
    if resource_limit is not None:
        data["resource_limit"] = resource_limit
        data["resourceLimit"] = resource_limit
    receipt.write_text(json.dumps(data), encoding="utf-8")
    return receipt


class BoardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-board-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        cleanup_repos()

    def make(self, name: str = "repo", config: dict | None = None):
        repo = Repo(self.tmp, name=name, config=config if config is not None else default_config())
        worktree = repo.worktree("wt")
        return repo, worktree

    def register(self, repo, task: str, env: dict, session: str = "session-A",
                 automation: str = "pi-harness", **extra) -> dict:
        args = ["register", "--repo", str(repo.root), "--task", task,
                "--session-id", session, "--automation-id", automation]
        for key, value in extra.items():
            flag = f"--{key.replace('_', '-')}"
            if value is True:
                args.append(flag)
            elif value is not None:
                args += [flag, str(value)]
        return board_json(*args, env=env)

    def refresh(self, repo, task: str, env: dict) -> dict:
        return board_json("refresh", "--repo", str(repo.root), "--task", task, env=env)

    def read_board(self, repo) -> dict:
        return json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))

    def board_bytes(self, repo) -> bytes:
        return (repo.state_dir / "board.json").read_bytes()

    def card(self, repo, task: str) -> dict:
        return self.read_board(repo)["cards"][task]

    def make_synthetic_task(self, repo, task: str, state: str = "running") -> Path:
        """A frozen-looking task with no live owner; deterministic lock-free state."""
        task_dir = repo.task_dir(task)
        checks = task_dir / "rounds" / "1" / "round.checks"
        (task_dir / "session").mkdir(parents=True, exist_ok=True)
        checks.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(json.dumps({
            "schemaVersion": 1, "task": task, "taskDir": str(task_dir), "repo": str(repo.root),
            "commonDir": str(repo.state_dir), "worktree": str(repo.root), "readOnly": False,
            "model": MODEL, "thinking": "max", "constraints": [], "checks": [],
            "maxWorkers": 1, "timeoutSeconds": 14400, "createdAt": time.time(),
            "startHead": None, "runtimeVersion": "test", "helperHashes": {},
            "sessionId": task, "sessionDir": str(task_dir / "session"),
        }), encoding="utf-8")
        (task_dir / "rounds" / "1" / "round.jsonl").touch()
        (task_dir / "rounds" / "1" / "round.err").touch()
        (task_dir / "rounds" / "1" / "round.meta").write_text("exit=0\n", encoding="utf-8")
        (task_dir / "rounds" / "1" / "round.state.json").write_text(json.dumps({
            "schemaVersion": 1, "round": 1, "state": state, "startedAt": time.time(),
            "exitCode": None, "timedOut": False, "cancelled": False, "taskDir": str(task_dir),
        }), encoding="utf-8")
        return task_dir

    def wait_for(self, predicate, timeout: float = 10, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    # ------------------------------------------------------------------
    def test_register_returns_exact_prompt_and_writes_card_and_gate(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-task", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-task", env=env)["state"], "completed")
        reg = self.register(repo, "board-task", env, title="T", goal="G", brief_ref="brief.md")
        self.assertEqual(reg["taskId"], "board-task")
        self.assertEqual(reg["automationId"], "pi-harness")
        self.assertIn("codex-pi-board-tick ", reg["automationPrompt"])
        self.assertIn(reg["nonce"], reg["automationPrompt"])
        self.assertIn(reg["automationPrompt"], reg["automationEnvelope"])
        self.assertEqual(len(reg["testCommands"]), 2)
        self.assertTrue(any("gate" in command for command in reg["testCommands"]))
        self.assertTrue(any("hook" in command for command in reg["testCommands"]))
        board_file = Path(reg["boardPath"])
        self.assertEqual(board_file.resolve(), (repo.state_dir / "board.json").resolve())
        board = self.read_board(repo)
        card = board["cards"]["board-task"]
        self.assertEqual(card["taskId"], "board-task")
        self.assertEqual(card["codexTaskId"], "session-A")
        self.assertEqual(card["ownerSession"], "session-A")
        self.assertEqual(card["title"], "T")
        self.assertEqual(card["goal"], "G")
        self.assertEqual(card["briefRef"], "brief.md")
        self.assertEqual(card["pi"]["state"], "completed")
        self.assertEqual(card["codex"]["review"], "pending")
        self.assertIsInstance(board["revision"], int)
        gate = json.loads(Path(reg["gatePath"]).read_text(encoding="utf-8"))
        self.assertEqual(gate["sessionId"], "session-A")
        self.assertEqual(gate["automationId"], "pi-harness")
        self.assertEqual(gate["instructions"], reg["automationPrompt"])
        self.assertEqual(gate["boardPath"], str(board_file))

    def test_refresh_projects_progress_and_publishes_terminal_event_once(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-terminal", worktree, env=env)
        repo.wait_round_state("board-terminal", "running")
        reg = self.register(repo, "board-terminal", env)
        self.assertEqual(self.card(repo, "board-terminal")["pi"]["state"], "running")
        first = self.refresh(repo, "board-terminal", env)
        self.assertFalse(first["refreshed"],
                         "registration already projected this state; nothing should rewrite")
        self.assertEqual(self.card(repo, "board-terminal")["pi"]["stage"], "implementing")
        self.assertEqual([event for event in self.card(repo, "board-terminal")["events"]
                          if event["kind"] == "review_required"], [])
        repo.cancel("board-terminal", env=env)
        self.assertEqual(repo.wait_terminal("board-terminal", env=env, timeout=25)["state"], "cancelled")

        def terminal_projected():
            try:
                card = self.card(repo, "board-terminal")
            except (KeyError, FileNotFoundError):
                return False
            return any(event["kind"] == "review_required" for event in card["events"])

        self.wait_for(terminal_projected, timeout=10,
                      what="supervisor terminal projection without an external refresh")
        card = self.card(repo, "board-terminal")
        events = [event for event in card["events"] if event["kind"] == "review_required"]
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["handled"])
        self.assertEqual(card["codex"]["review"], "pending")
        self.assertEqual(card["pi"]["stage"], "cancelled")
        revision = self.read_board(repo)["revision"]
        again = self.refresh(repo, "board-terminal", env)
        self.assertFalse(again["refreshed"], "an unchanged projection must not rewrite the board")
        self.assertEqual(again["newEvents"], [])
        self.assertEqual(self.read_board(repo)["revision"], revision)
        self.assertEqual(len([event for event in self.card(repo, "board-terminal")["events"]
                              if event["kind"] == "review_required"]), 1)

    def test_failed_check_alone_is_not_an_event_but_timeout_and_breach_are(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-checks", worktree, env=env)
        repo.wait_round_state("board-checks", "running")
        checks = repo.task_dir("board-checks") / "rounds" / "1" / "round.checks"
        self.register(repo, "board-checks", env)
        plain = synth_receipt(checks, "plain-failure", 3)
        self.refresh(repo, "board-checks", env)
        card = self.card(repo, "board-checks")
        self.assertEqual([event for event in card["events"] if event["kind"] == "check_failed"], [])
        self.assertTrue(card["evidence"]["receiptRef"].endswith(plain.name),
                        "plain failure evidence stays visible as a check reference")
        synth_receipt(checks, "timeout-check", 124, timed_out=True)
        breach = {"path": str(self.tmp / "watch"), "max_bytes": 100, "observed_bytes": 9999,
                  "breached": True, "unknown": False, "complete": True,
                  "reason": "observed 9999 bytes exceed the declared max_bytes 100"}
        synth_receipt(checks, "breaching-check", 75, resource_limit=breach)
        events = self.refresh(repo, "board-checks", env)["newEvents"]
        kinds = {event["kind"] for event in self.card(repo, "board-checks")["events"]}
        self.assertIn("check_timeout", kinds)
        self.assertIn("resource_breach", kinds)
        self.assertEqual(len([event for event in self.card(repo, "board-checks")["events"]
                              if event["kind"] == "check_timeout"]), 1)
        revision = self.read_board(repo)["revision"]
        self.refresh(repo, "board-checks", env)
        self.assertEqual(self.read_board(repo)["revision"], revision,
                         "deadline/resource dedup must not republish or rewrite")
        repo.cancel("board-checks", env=env)
        repo.wait_terminal("board-checks", env=env, timeout=25)

    def test_unknown_ownership_event_and_decision_without_head(self):
        repo, _worktree = self.make()
        env = h_env(self.tmp)
        self.make_synthetic_task(repo, "ghost-task", state="running")
        self.register(repo, "ghost-task", env)
        card = self.card(repo, "ghost-task")
        self.assertEqual(len(card["events"]), 1)
        event = card["events"][0]
        self.assertEqual(event["kind"], "ownership_unknown")
        again = self.refresh(repo, "ghost-task", env)
        self.assertEqual(again["newEvents"], [], "ownership events are deduplicated")
        self.assertFalse(event["handled"])
        accepted = run_board("decide", "--repo", repo.root, "--task", "ghost-task",
                             "--event-id", event["id"], "--decision", "accept",
                             env=env, expect=2)
        self.assertIn("reviewed-head", accepted.stderr)
        decided = board_json("decide", "--repo", repo.root, "--task", "ghost-task",
                             "--event-id", event["id"], "--decision", "reject",
                             "--note", "inspect owner", env=env)
        self.assertEqual(decided["decision"], "rejected")
        self.assertTrue(self.card(repo, "ghost-task")["events"][0]["handled"])
        self.assertEqual(self.card(repo, "ghost-task")["codex"]["review"], "rejected")

    def test_decide_binds_exact_event_and_never_auto_accepts(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-decide", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-decide", env=env)["state"], "completed")
        self.register(repo, "board-decide", env)
        card = self.card(repo, "board-decide")
        event = next(item for item in card["events"] if item["kind"] == "review_required")
        self.assertEqual(card["codex"]["review"], "pending",
                         "exit 0 never implies acceptance")
        self.assertFalse(event["handled"])
        missing = run_board("decide", "--repo", repo.root, "--task", "board-decide",
                            "--event-id", event["id"], "--decision", "accept", env=env, expect=2)
        self.assertIn("reviewed-head", missing.stderr)
        wrong = run_board("decide", "--repo", repo.root, "--task", "board-decide",
                          "--event-id", event["id"], "--decision", "accept",
                          "--reviewed-head", "0" * 40, env=env, expect=2)
        self.assertIn("does not match", wrong.stderr)
        head = event["candidate"]["head"]
        self.assertTrue(head)
        accepted = board_json("decide", "--repo", repo.root, "--task", "board-decide",
                              "--event-id", event["id"], "--decision", "accept",
                              "--reviewed-head", head, "--note", "reviewed", env=env)
        self.assertEqual(accepted["decision"], "accepted")
        card = self.card(repo, "board-decide")
        self.assertEqual(card["codex"]["review"], "accepted")
        self.assertEqual(card["codex"]["reviewedHead"], head)
        self.assertTrue(card["events"][0]["handled"])
        idempotent = board_json("decide", "--repo", repo.root, "--task", "board-decide",
                                "--event-id", event["id"], "--decision", "accept",
                                "--reviewed-head", head, env=env)
        self.assertTrue(idempotent["idempotent"])
        overwrite = run_board("decide", "--repo", repo.root, "--task", "board-decide",
                              "--event-id", event["id"], "--decision", "reject",
                              env=env, expect=2)
        self.assertIn("already handled", overwrite.stderr)

    def test_out_of_order_decisions_do_not_clear_other_events(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-order", worktree, env=env)
        repo.wait_round_state("board-order", "running")
        checks = repo.task_dir("board-order") / "rounds" / "1" / "round.checks"
        self.register(repo, "board-order", env)
        synth_receipt(checks, "timed-out-check", 124, timed_out=True)
        self.refresh(repo, "board-order", env)
        repo.cancel("board-order", env=env)
        self.assertEqual(repo.wait_terminal("board-order", env=env, timeout=25)["state"], "cancelled")
        self.refresh(repo, "board-order", env)
        card = self.card(repo, "board-order")
        pending = [event for event in card["events"] if not event["handled"]]
        kinds = {event["kind"] for event in pending}
        self.assertIn("check_timeout", kinds)
        self.assertIn("review_required", kinds)
        newer = next(event for event in pending if event["kind"] == "review_required")
        older = next(event for event in pending if event["kind"] == "check_timeout")
        self.assertGreater(newer["seq"], older["seq"])
        board_json("decide", "--repo", repo.root, "--task", "board-order",
                   "--event-id", newer["id"], "--decision", "reject", env=env)
        card = self.card(repo, "board-order")
        older_now = next(event for event in card["events"] if event["id"] == older["id"])
        self.assertFalse(older_now["handled"],
                         "handling a newer event must not clear an older unhandled event")
        board_json("decide", "--repo", repo.root, "--task", "board-order",
                   "--event-id", older["id"], "--decision", "changes_requested", env=env)
        card = self.card(repo, "board-order")
        self.assertTrue(all(event["handled"] for event in card["events"]))

    def test_concurrent_refresh_publications_are_serialized(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-race", worktree, env=env)
        repo.wait_round_state("board-race", "running")
        self.register(repo, "board-race", env)
        repo.cancel("board-race", env=env)
        self.assertEqual(repo.wait_terminal("board-race", env=env, timeout=25)["state"], "cancelled")
        procs = [subprocess.Popen([sys.executable, str(BOARD), "refresh", "--repo",
                                   str(repo.root), "--task", "board-race"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(4)]
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["taskId"], "board-race")
        board = self.read_board(repo)
        events = board["cards"]["board-race"]["events"]
        self.assertEqual(len([event for event in events if event["kind"] == "review_required"]), 1)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["handled"])

    # ------------------------------------------------------------------
    def test_gate_stop_deliver_once_and_passthrough_variants(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-gate", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-gate", env=env)["state"], "completed")
        reg = self.register(repo, "board-gate", env)
        prompt = envelope(reg["automationId"], reg["automationPrompt"])
        before = self.board_bytes(repo)
        delivered = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
        context = delivered["hookSpecificOutput"]["additionalContext"]
        self.assertIn("board-gate", context)
        self.assertIn("review_required", context)
        self.assertIn("candidate_head=", context)
        self.assertIn("decide=python3", context)
        self.assertNotIn("continue", delivered)
        self.assertEqual(self.board_bytes(repo), before,
                         "delivery must not write the board (no per-tick board write)")
        card = self.card(repo, "board-gate")
        review = next(event for event in card["events"] if event["kind"] == "review_required")
        self.assertFalse(review["handled"], "delivery never handles or accepts")
        self.assertEqual(card["codex"]["review"], "pending")

        second = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
        self.assertFalse(second.get("continue", True),
                         "an already delivered, unhandled event must not wake the model again")
        self.assertNotIn("hookSpecificOutput", second)
        self.assertEqual(self.board_bytes(repo), before)

        block_env = dict(env)
        block_env["CODEX_PI_GATE_STOP_MODE"] = "block"
        blocked = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), block_env).stdout)
        self.assertEqual(blocked["decision"], "block")

        # Ordinary and forged prompts pass through with the old behavior.
        cases = [
            gate_payload("session-A", repo.root, "please review the board"),
            gate_payload("session-A", repo.root, "text with <heartbeat> inside"),
            gate_payload("session-A", repo.root, "  " + prompt),
            gate_payload("session-A", repo.root, envelope("other-automation", reg["automationPrompt"])),
            gate_payload("session-A", repo.root, envelope(reg["automationId"], "wrong instructions")),
            gate_payload("session-A", repo.root, prompt + "extra"),
            gate_payload("unknown-session", repo.root, prompt),
            gate_payload("session-A", repo.root, "<heartbeat>\n</heartbeat>\n"),
            {"hook_event_name": "UserPromptSubmit", "session_id": "session-A", "cwd": str(repo.root)},
        ]
        for payload in cases:
            proc = run_hook(payload, env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), {}, f"prompt should pass through: {payload}")
        self.assertEqual(self.board_bytes(repo), before)

    def test_gate_corrupt_missing_and_oversized_board_fail_open(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-corrupt", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-corrupt", env=env)["state"], "completed")
        reg = self.register(repo, "board-corrupt", env)
        prompt = envelope(reg["automationId"], reg["automationPrompt"])
        board_file = Path(reg["boardPath"])
        original = board_file.read_bytes()
        try:
            board_file.write_text("{not json", encoding="utf-8")
            proc = run_hook(gate_payload("session-A", repo.root, prompt), env)
            data = json.loads(proc.stdout)
            self.assertIn("diagnostic", data["hookSpecificOutput"]["additionalContext"])
            self.assertNotIn("continue", data)

            board_file.unlink()
            data = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
            self.assertIn("diagnostic", data["hookSpecificOutput"]["additionalContext"])

            board_file.write_bytes(b'{"schemaVersion": 1, "padding": "' + b"x" * 300000 + b'"}')
            data = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
            self.assertIn("oversized", data["hookSpecificOutput"]["additionalContext"])
        finally:
            board_file.write_bytes(original)

    def test_pause_stops_ticks_and_resume_is_explicit(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-paused", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-paused", env=env)["state"], "completed")
        reg = self.register(repo, "board-paused", env)
        prompt = envelope(reg["automationId"], reg["automationPrompt"])
        board_json("pause", "--repo", repo.root, "--task", "board-paused",
                   "--note", "user asked to wait", env=env)
        before = self.board_bytes(repo)
        paused = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
        self.assertFalse(paused.get("continue", True),
                         "a paused card must stop automatic ticks even with pending events")
        self.assertNotIn("hookSpecificOutput", paused)
        self.assertEqual(self.board_bytes(repo), before)
        self.refresh(repo, "board-paused", env)
        self.assertTrue(self.card(repo, "board-paused")["paused"],
                        "progress refresh must not clear an explicit pause")

        board_json("resume", "--repo", repo.root, "--task", "board-paused", env=env)
        resumed = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
        self.assertIn("hookSpecificOutput", resumed)

    def test_gate_never_starts_pi_or_waits_on_board_lock(self):
        repo, worktree = self.make()
        trap_env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-trap", worktree, env=trap_env)
        self.assertEqual(repo.wait_terminal("board-trap", env=trap_env)["state"], "completed")
        reg = self.register(repo, "board-trap", trap_env)
        trap, marker = make_pi_trap(self.tmp, "pi-trap")
        env = dict(trap_env)
        env["PI_BIN"] = str(trap)
        prompt = envelope(reg["automationId"], reg["automationPrompt"])
        proc = run_hook(gate_payload("session-A", repo.root, prompt), env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker.exists(), "the gate must never start Pi or a model")

        # Hold the board write lock while the hook performs one tick: the gate
        # reads the atomic snapshot without taking the board lock.
        lock_path = repo.state_dir / "board.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys, time; stream = open(sys.argv[1], 'a+'); "
             "fcntl.flock(stream, fcntl.LOCK_EX); print('held', flush=True); time.sleep(8)",
             str(lock_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            started = time.monotonic()
            proc = run_hook(gate_payload("session-A", repo.root, prompt), env, timeout=10)
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertLess(elapsed, 4.0, f"gate blocked on the board write lock for {elapsed:.2f}s")
        finally:
            holder.kill()
            holder.wait()
            for stream in (holder.stdout, holder.stderr):
                if stream is not None:
                    stream.close()
        self.assertFalse(marker.exists())

    def test_gate_does_not_require_git_or_board_for_ordinary_prompt(self):
        # An ordinary prompt with no registration must not resolve git at all:
        # point --cwd at a path that is not a repository.
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        proc = run_hook(gate_payload("session-none", self.tmp / "not-a-repo",
                                     "plain human prompt"), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {})

    # ------------------------------------------------------------------
    def test_supervisor_cadence_refreshes_registered_card_only(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang", CODEX_PI_BOARD_REFRESH_SECONDS="0.3")
        repo.start("board-cadence", worktree, env=env)
        repo.wait_round_state("board-cadence", "running")
        try:
            reg = self.register(repo, "board-cadence", env)
            initial_revision = reg["revision"]
            checks = repo.task_dir("board-cadence") / "rounds" / "1" / "round.checks"
            checks.mkdir(parents=True, exist_ok=True)
            log = checks / "cadence-check-abc.log"
            log.write_text("check output\n", encoding="utf-8")
            (checks / "cadence-check-abc.running").write_text(json.dumps({
                "schema_version": 1, "id": "cadence-check", "pid": os.getpid(),
                "started_at": time.time(), "deadline_at": time.time() + 600,
                "timeout_seconds": 600, "deadline_scope": "wrapper timeout only",
                "log": log.name, "receipt": "cadence-check-abc.json"}), encoding="utf-8")

            def projected():
                card = self.card(repo, "board-cadence")
                check = card.get("check") or {}
                return check.get("id") == "cadence-check" and \
                    self.read_board(repo)["revision"] > initial_revision

            self.wait_for(projected, timeout=10, what="supervisor board refresh")
            card = self.card(repo, "board-cadence")
            self.assertEqual(card["check"]["id"], "cadence-check")
            self.assertEqual(card["pi"]["stage"], "checking")
        finally:
            repo.cancel("board-cadence", env=env)
            repo.wait_terminal("board-cadence", env=env, timeout=25)

        other, other_worktree = self.make("repo-unregistered")
        other_env = h_env(self.tmp / "other", PI_DOUBLE_MODE="hang",
                          CODEX_PI_BOARD_REFRESH_SECONDS="0.2")
        other.start("not-registered", other_worktree, env=other_env)
        other.wait_round_state("not-registered", "running")
        try:
            time.sleep(1.0)
            self.assertFalse((other.state_dir / "codex-pi" / "board.json").exists(),
                             "unregistered runtime must not create a board")
        finally:
            other.cancel("not-registered", env=other_env)
            other.wait_terminal("not-registered", env=other_env, timeout=25)

    def test_refresh_for_unregistered_task_is_a_noop(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-noop", worktree, env=env)
        repo.wait_round_state("board-noop", "running")
        try:
            self.assertFalse((repo.state_dir / "codex-pi" / "board.json").exists())
            proc = run_board("refresh", "--repo", repo.root, "--task", "board-noop", env=env)
            self.assertEqual(proc.returncode, 0)
            self.assertFalse(json.loads(proc.stdout)["refreshed"])
            self.assertFalse((repo.state_dir / "codex-pi" / "board.json").exists(),
                             "one-shot refresh must not create a board")
        finally:
            repo.cancel("board-noop", env=env)
            repo.wait_terminal("board-noop", env=env, timeout=25)

    def test_registered_timeout_projects_deadline_event_without_poller(self):
        repo, worktree = self.make(name="repo-timeout", config=default_config(timeoutSeconds=1))
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-timeout", worktree, env=env)
        repo.wait_round_state("board-timeout", "running")
        self.register(repo, "board-timeout", env)
        result = repo.wait_terminal("board-timeout", env=env, timeout=30)
        self.assertEqual(result["state"], "timed_out")

        def projected():
            try:
                card = self.card(repo, "board-timeout")
            except (KeyError, FileNotFoundError):
                return False
            return any(event["kind"] == "task_timeout" for event in card["events"])

        self.wait_for(projected, timeout=10, what="timeout projection")
        card = self.card(repo, "board-timeout")
        kinds = {event["kind"] for event in card["events"]}
        self.assertIn("task_timeout", kinds)
        self.assertIn("review_required", kinds)
        revision = self.read_board(repo)["revision"]
        self.refresh(repo, "board-timeout", env)
        self.assertEqual(self.read_board(repo)["revision"], revision,
                         "deadline events must not be republished")
        self.assertEqual(len([event for event in self.card(repo, "board-timeout")["events"]
                              if event["kind"] == "task_timeout"]), 1)

    def test_reregister_preserves_prompt_and_rearm_redelivers(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-rearm", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-rearm", env=env)["state"], "completed")
        first = self.register(repo, "board-rearm", env)
        prompt = envelope(first["automationId"], first["automationPrompt"])
        second = self.register(repo, "board-rearm", env, title="Updated title")
        self.assertEqual(second["automationPrompt"], first["automationPrompt"],
                         "re-registration must not silently change the automation prompt")
        self.assertEqual(second["nonce"], first["nonce"])
        delivered = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
        self.assertIn("hookSpecificOutput", delivered)
        stopped = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
        self.assertFalse(stopped.get("continue", True))
        rearmed = board_json("rearm", "--repo", repo.root, "--task", "board-rearm",
                             "--session-id", "session-A", env=env)
        self.assertTrue(rearmed["deliveredCleared"])
        again = json.loads(run_hook(gate_payload("session-A", repo.root, prompt), env).stdout)
        self.assertIn("hookSpecificOutput", again,
                      "an explicit re-arm redelivers delivered but still unhandled events")
        rotated = self.register(repo, "board-rearm", env, new_nonce=True)
        self.assertNotEqual(rotated["nonce"], first["nonce"])

    def test_monitor_error_is_visible_and_never_kills_pi(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="hang", CODEX_PI_BOARD_REFRESH_SECONDS="0.2")
        repo.start("board-monitor", worktree, env=env)
        repo.wait_round_state("board-monitor", "running")
        try:
            reg = self.register(repo, "board-monitor", env)
            Path(reg["boardPath"]).write_text("{broken", encoding="utf-8")
            repo.cancel("board-monitor", env=env)
            result = repo.wait_terminal("board-monitor", env=env, timeout=25)
            self.assertEqual(result["state"], "cancelled",
                             "a board monitor error must not kill or change the Pi run")
            log = repo.state_dir / "board-monitor.log"
            self.assertTrue(log.is_file(), "monitor errors leave bounded local evidence")
            text = log.read_text(encoding="utf-8", errors="replace")
            self.assertIn("board-monitor", text)
            self.assertIn("invalid", text)
        finally:
            repo.kill_leftovers()

    def test_show_and_packet_are_bounded_to_selected_owner(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-show", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-show", env=env)["state"], "completed")
        self.register(repo, "board-show", env, session="session-show", title="Visible task")
        show = board_json("show", "--repo", repo.root, "--owner", "session-show", env=env)
        self.assertEqual(show["count"], 1)
        self.assertEqual(show["cards"][0]["taskId"], "board-show")
        self.assertEqual(show["cards"][0]["pendingCount"], 1)
        other = board_json("show", "--repo", repo.root, "--owner", "session-other", env=env)
        self.assertEqual(other["count"], 0)
        packet = board_json("packet", "--repo", repo.root, "--task", "board-show", env=env)
        self.assertEqual(packet["pendingCount"], 1)
        self.assertEqual(packet["events"][0]["kind"], "review_required")
        self.assertNotIn("summaryText", json.dumps(packet))
        self.assertNotIn("round.jsonl", json.dumps(packet))

    def test_malformed_board_cli_is_unknown_not_success(self):
        repo, worktree = self.make()
        env = h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-bad", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-bad", env=env)["state"], "completed")
        reg = self.register(repo, "board-bad", env)
        board_file = Path(reg["boardPath"])
        board_file.write_text("{broken", encoding="utf-8")
        failed = run_board("show", "--repo", repo.root, "--all", env=env, expect=2)
        self.assertIn("invalid", failed.stderr)
        failed = run_board("refresh", "--repo", repo.root, "--task", "board-bad", env=env, expect=2)
        self.assertIn("invalid", failed.stderr)


if __name__ == "__main__":
    unittest.main()
