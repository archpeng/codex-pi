"""Behavioral tests for the phase contract, structured progress, readiness,
event classification, one auto-continuation and the cross-phase gate.

Everything runs the real CLIs and real temporary git repositories. Pi is the
offline double; no model, network, real Codex queue or MCP is invoked.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from runtime_helpers import (RUNTIME, Repo, base_env, cleanup_repos, cli_json, default_config,
                             run_cli)

sys.path.insert(0, str(RUNTIME))
import pi_board  # noqa: E402
import pi_task  # noqa: E402

BOARD = RUNTIME / "pi_board.py"
TASK = RUNTIME / "pi_task.py"
CHECK = RUNTIME / "pi_check.py"
THREAD_A = "11111111-2222-3333-4444-555555555555"


def run_board(*args, env: dict, expect: int = 0, timeout: float = 60):
    proc = subprocess.run([sys.executable, str(BOARD), *[str(arg) for arg in args]],
                          capture_output=True, text=True, env=env, timeout=timeout)
    if proc.returncode != expect:
        raise AssertionError(f"pi_board {' '.join(str(arg) for arg in args)} exited "
                             f"{proc.returncode}, expected {expect}\n"
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def board_json(*args, env: dict, timeout: float = 60) -> dict:
    return json.loads(run_board(*args, env=env, expect=0, timeout=timeout).stdout)


def make_cli_double(directory: Path, name: str = "codex") -> tuple:
    """Queue-only double: any forbidden capability exits 9; records argv."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{name}.jsonl"
    script = directory / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"with open({str(marker)!r}, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "argv = sys.argv[1:]\n"
        "ok = (len(argv) == 7 and argv[0] == '--disable' and argv[1] == 'daemon_auto_start'\n"
        "      and argv[2] == 'queue' and argv[3] == '--thread' and argv[5] == '--message')\n"
        "if not ok:\n"
        "    raise SystemExit(9)\n"
        "print('Queued message double for thread ' + argv[4])\n"
        "raise SystemExit(0)\n", encoding="utf-8")
    script.chmod(0o755)
    return script, marker


class PhaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-phase-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.fake_bin = self.tmp / "fake-bin"
        _double, self.fake_marker = make_cli_double(self.fake_bin)

    def tearDown(self):
        cleanup_repos()

    def h_env(self, **extra) -> dict:
        env = base_env(**extra)
        env["CODEX_PI_HANDOFF_ROOT"] = str(self.tmp / "handoffs")
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env.pop("CODEX_THREAD_ID", None)
        return env

    def make(self, name: str = "repo"):
        repo = Repo(self.tmp, name=name, config=default_config())
        worktree = repo.worktree("wt")
        return repo, worktree

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def write_design(self, repo: Repo, rel: str = "docs/design.md",
                     text: str = "# phase design\n") -> str:
        path = repo.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        repo._git("add", "-A")
        repo._git("-c", "user.email=test@example.invalid", "-c", "user.name=Test",
                  "commit", "-qm", f"design {rel}")
        head = repo._git("rev-parse", "HEAD")
        # Keep freshly created linked worktrees on the same commit so the frozen
        # baseline and the candidate include the design file.
        listing = subprocess.check_output(
            ["git", "-C", str(repo.root), "worktree", "list", "--porcelain"],
            text=True).strip()
        for line in listing.splitlines():
            if not line.startswith("worktree "):
                continue
            candidate = Path(line[len("worktree "):])
            if candidate.resolve() == repo.root.resolve():
                continue
            if candidate.exists():
                subprocess.run(["git", "-C", str(candidate), "reset", "--hard", head],
                               check=True, capture_output=True)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def contract(self, repo: Repo, phase_id: str = "P1", *, budget: float = 3600,
                 design_rel: str = "docs/design.md", design_sha: str | None = None,
                 items=None, scope=None, extra=None) -> dict:
        data = {
            "schemaVersion": 1, "phaseId": phase_id, "goal": "Overall goal",
            "result": "Complete phase result", "baseline": "HEAD",
            "scope": scope if scope is not None else ["."],
            "designRef": design_rel,
            "designSha256": design_sha or self.write_design(repo, design_rel),
            "acceptanceItems": items if items is not None else [{
                "id": "A1", "description": "the behavior works",
                "command": "python3 -c pass", "passCondition": "exit 0",
                "evidence": "pi_check receipt for A1"}],
            "budgetSeconds": budget, "autonomousRepair": ["fix red tests"],
            "escalateWhen": ["design contradiction"],
            "commandTimeoutSeconds": 900, "resourceLimits": [],
        }
        if extra:
            data.update(extra)
        return data

    def write_contract(self, name: str, contract: dict) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(contract, indent=2), encoding="utf-8")
        return path

    def start(self, repo: Repo, worktree, task: str, contract_path: Path | None,
              env: dict, prompt: str = "Implement the phase.", expect: int | None = 0):
        args = ["start", "--repo", str(repo.root), "--task", task,
                "--worktree", str(worktree), "--prompt", prompt]
        if contract_path is not None:
            args += ["--contract-file", str(contract_path)]
        return run_cli(*args, env=env, expect=expect)

    def synth_receipt(self, checks: Path, check_id: str, exit_code: int, head: str, *,
                      dirty: bool = False, timed_out: bool = False, cancelled: bool = False,
                      counts=None, command: str = "python3 -c pass",
                      timeout_seconds: float = 900, argv=None, deadline_at=None) -> Path:
        checks.mkdir(parents=True, exist_ok=True)
        log = checks / f"{check_id}-{uuid.uuid4().hex[:8]}.log"
        log.write_text("synthetic evidence\n", encoding="utf-8")
        receipt = checks / f"{check_id}-{uuid.uuid4().hex[:8]}.json"
        started = time.time() - 1
        data = {"schema_version": 1, "id": check_id,
                "argv": shlex.split(command) if argv is None else argv, "cwd": str(checks),
                "head": head, "dirty": dirty, "tracked_diff_sha256": None,
                "started_at": started, "ended_at": time.time(),
                "deadline_at": started + timeout_seconds if deadline_at is None else deadline_at,
                "exit_code": exit_code, "timed_out": timed_out, "cancelled": cancelled,
                "error": None, "test_counts": counts, "log": log.name,
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "acceptance": "not_verified"}
        receipt.write_text(json.dumps(data), encoding="utf-8")
        return receipt

    def head(self, worktree: Path) -> str:
        return subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                                       text=True).strip()

    def wait_for(self, predicate, timeout: float = 30, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}")

    def rounds(self, repo: Repo, task: str) -> list:
        directory = repo.task_dir(task) / "rounds"
        if not directory.is_dir():
            return []
        return sorted(int(entry.name) for entry in directory.iterdir()
                      if entry.is_dir() and entry.name.isdigit())

    def wait_rounds(self, repo: Repo, task: str, count: int, timeout: float = 30) -> list:
        def ready():
            numbers = self.rounds(repo, task)
            if len(numbers) < count:
                return None
            return numbers
        return self.wait_for(ready, timeout=timeout, what=f"{count} rounds for {task}")

    def wait_terminal(self, repo: Repo, task: str, round_number: int | None = None,
                      timeout: float = 30) -> dict:
        return repo.wait_terminal(task, timeout=timeout, round=round_number)

    def register(self, repo: Repo, task: str, env: dict, transport: str = "offline",
                 thread: str | None = None, codex_bin: Path | None = None) -> dict:
        args = ["register", "--repo", str(repo.root), "--task", task, "--transport", transport]
        if thread is not None:
            args += ["--thread", thread]
        if codex_bin is not None:
            args += ["--codex-bin", str(codex_bin)]
        return board_json(*args, env=env)

    def refresh(self, repo: Repo, task: str, env: dict) -> dict:
        return board_json("refresh", "--repo", str(repo.root), "--task", task, env=env)

    def card(self, repo: Repo, task: str) -> dict:
        return json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))["cards"][task]

    def phase_state(self, repo: Repo, task: str) -> dict:
        return json.loads((repo.task_dir(task) / "phase.state.json").read_text(encoding="utf-8"))

    def phase_auto(self, repo: Repo, task: str) -> dict:
        path = repo.task_dir(task) / "phase-auto.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get("phases", {})

    def pending(self, repo: Repo, task: str, kind: str | None = None) -> list:
        events = json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))[
            "cards"][task]["events"]
        return [event for event in events
                if not event.get("handled") and (kind is None or event.get("kind") == kind)]

    # ------------------------------------------------------------------
    # O1-1 phase contract
    # ------------------------------------------------------------------
    def test_contract_is_frozen_into_task_and_brief(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="ok")
        sha = self.write_design(repo)
        contract = self.contract(repo, "P-CONTRACT", design_sha=sha)
        path = self.write_contract("p.json", contract)
        response = self.start(repo, worktree, "contract-task", path, env)
        data = json.loads(response.stdout)
        task_dir = repo.task_dir("contract-task")
        frozen = json.loads((task_dir / "phase.json").read_text(encoding="utf-8"))
        recorded = frozen["contract"]
        self.assertEqual(recorded["phaseId"], "P-CONTRACT")
        self.assertEqual(frozen["contractSha256"], pi_task.contract_hash(recorded))
        self.assertEqual(data["phase"]["contractSha256"], frozen["contractSha256"])
        self.assertTrue(frozen["baselineCommit"])
        self.assertTrue((task_dir / "tools" / "pi_phase.py").is_file())
        task_json = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        self.assertIn("pi_phase.py", task_json["helperHashes"])
        brief = (task_dir / "rounds" / "1" / "brief.md").read_text(encoding="utf-8")
        self.assertIn("phase_id=P-CONTRACT", brief)
        self.assertIn(f"contract_sha256={frozen['contractSha256']}", brief)
        self.assertIn("id=A1", brief)
        self.assertIn("--completed-criteria", brief)
        self.assertIn("readiness", brief)
        self.assertIn("design_sha256=" + sha, brief)
        self.wait_terminal(repo, "contract-task")

    def test_invalid_contracts_are_rejected_before_any_task_evidence(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="ok")
        good = self.contract(repo, "P-BAD")
        cases = []
        bad = dict(good, designSha256="0" * 64)
        cases.append(("design-hash", bad))
        cases.append(("traversal", dict(good, scope=["../outside"])))
        cases.append(("bad-budget", dict(good, budgetSeconds=0)))
        cases.append(("unknown-key", dict(good, surprise="x")))
        duplicate = [dict(good["acceptanceItems"][0]), dict(good["acceptanceItems"][0])]
        cases.append(("duplicate-items", dict(good, acceptanceItems=duplicate)))
        cases.append(("bad-id", dict(good, phaseId="bad id")))
        missing_limits = {key: value for key, value in good.items()
                          if key not in ("commandTimeoutSeconds", "resourceLimits")}
        cases.append(("missing-limits", missing_limits))
        for index, (name, contract) in enumerate(cases):
            path = self.write_contract(f"bad-{index}.json", contract)
            task = f"invalid-{index}"
            proc = self.start(repo, worktree, task, path, env, expect=2)
            self.assertTrue(proc.stderr.strip(), f"{name}: expected a validation error")
            self.assertFalse(repo.task_dir(task).exists(),
                             f"{name}: no task evidence may exist after rejection")

    def test_legacy_task_stays_compatible(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="ok")
        self.start(repo, worktree, "legacy", None, env)
        self.wait_terminal(repo, "legacy")
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "legacy", env=env)
        self.assertEqual(readiness["status"], "no_contract")
        status = cli_json("phase-status", "--repo", str(repo.root), "--task", "legacy", env=env)
        self.assertTrue(status["legacy"])
        self.assertFalse(status["phaseInstalled"])
        self.register(repo, "legacy", env)
        self.refresh(repo, "legacy", env)
        reviews = self.pending(repo, "legacy", "review_required")
        self.assertEqual(len(reviews), 1)
        self.assertIsNone(reviews[0].get("phaseId"))

    # ------------------------------------------------------------------
    # O1-2 structured progress
    # ------------------------------------------------------------------
    def test_progress_is_validated_atomic_and_never_queued(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="hang")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-PROGRESS", design_sha=sha))
        self.start(repo, worktree, "progress-task", path, env)
        repo.wait_round_state("progress-task", "running")
        try:
            self.register(repo, "progress-task", env, transport="cli-queue", thread=THREAD_A)
            # The first update goes through the task's frozen helper snapshot so
            # the shipped runtime is what Pi actually runs.
            frozen_task = repo.task_dir("progress-task") / "tools" / "pi_task.py"
            proc = subprocess.run(
                [sys.executable, str(frozen_task), "progress", "--repo", str(worktree),
                 "--task", "progress-task", "--round", "1", "--activity", "implementing",
                 "--step", "implemented the parser", "--completed-criteria", "A1",
                 "--next", "run the acceptance check", "--evidence-ref", "runtime/pi_task.py"],
                capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["activity"], "implementing")
            progress_file = repo.task_dir("progress-task") / "rounds" / "1" / "progress.json"
            stored = json.loads(progress_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["activity"], "implementing")
            self.assertEqual(stored["completedCriteria"], ["A1"])
            self.assertEqual(stored["reportedBy"], "pi")
            self.assertFalse(stored["verified"])
            status = cli_json("status", "--repo", str(repo.root), "--task", "progress-task",
                              env=env)
            self.assertEqual(status["progress"]["activity"], "implementing")
            self.assertEqual(status["progress"]["completedCriteria"], ["A1"])
            # Ordinary progress produces no event and no queue message.
            first = self.refresh(repo, "progress-task", env)
            self.assertEqual(first["newEvents"], [])
            self.assertEqual(self.pending(repo, "progress-task"), [])
            self.assertFalse(self.fake_marker.exists(),
                             "ordinary progress must never invoke the queue CLI")
            revision = json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))["revision"]
            # A repairing update with evidence is a real milestone; it may
            # notify once, but repeating the identical write must not queue a
            # second time and refresh itself never dispatches.
            run_cli("progress", "--repo", str(worktree), "--task", "progress-task",
                    "--activity", "repairing", "--next", "retry the check", "--blocker", "",
                    env=env, expect=0)
            self.refresh(repo, "progress-task", env)
            self.assertGreater(json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))["revision"],
                               revision)
            milestones = self.pending(repo, "progress-task", "progress_update")
            self.assertEqual(len(milestones), 1)
            run_cli("progress", "--repo", str(worktree), "--task", "progress-task",
                    "--activity", "repairing", "--next", "retry the check", "--blocker", "",
                    env=env, expect=0)
            self.refresh(repo, "progress-task", env)
            self.assertEqual(len(self.pending(repo, "progress-task", "progress_update")), 1)
            self.assertEqual(len(self.pending(repo, "progress-task")), 1,
                             "repeated identical milestones must not add events")
            self.assertFalse(self.fake_marker.exists())
            # Invalid input is refused without touching evidence.
            before = progress_file.read_bytes()
            run_cli("progress", "--repo", str(worktree), "--task", "progress-task",
                    "--activity", "sleeping", env=env, expect=2)
            run_cli("progress", "--repo", str(worktree), "--task", "progress-task",
                    "--activity", "checking", "--completed-criteria", "NOT-AN-ID", env=env, expect=2)
            run_cli("progress", "--repo", str(worktree), "--task", "progress-task",
                    "--activity", "checking", "--evidence-ref", "../../etc/passwd", env=env, expect=2)
            self.assertEqual(progress_file.read_bytes(), before)
            shown = cli_json("progress", "--repo", str(repo.root), "--task", "progress-task",
                             "--show", env=env)
            self.assertEqual(shown["progress"]["activity"], "repairing")
        finally:
            repo.cancel("progress-task", env=env)
            repo.wait_terminal("progress-task", env=env, timeout=25)

    # ------------------------------------------------------------------
    # O1-3 readiness
    # ------------------------------------------------------------------
    def test_readiness_requires_applicable_receipts_for_the_candidate(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="4")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-READY", design_sha=sha))
        self.start(repo, worktree, "readiness-task", path, env)
        repo.wait_round_state("readiness-task", "running")
        # Register and pause so the missing-evidence auto-continuation does not
        # move the task to a second round; readiness facts are about round 1.
        self.register(repo, "readiness-task", env)
        board_json("pause", "--repo", str(repo.root), "--task", "readiness-task",
                   "--note", "hold for the check", env=env)
        checks = repo.task_dir("readiness-task") / "rounds" / "1" / "round.checks"
        candidate = self.head(worktree)
        parent = subprocess.check_output(["git", "-C", str(worktree), "rev-parse", "HEAD^"],
                                         text=True).strip()
        # Stale-head and dirty receipts are not applicable; a terminal round with
        # only those is not ready.
        self.synth_receipt(checks, "A1", 0, parent)
        self.synth_receipt(checks, "A1", 0, candidate, dirty=True)
        repo.wait_terminal("readiness-task")
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "readiness-task",
                             "--round", "1", env=env)
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(readiness["items"][0]["status"], "missing")
        self.assertIn("bind", readiness["items"][0]["reason"])
        # A passing applicable receipt makes coverage ready; readiness never
        # becomes acceptance.
        self.synth_receipt(checks, "A1", 0, candidate)
        fresh = cli_json("readiness", "--repo", str(repo.root), "--task", "readiness-task",
                         "--round", "1", env=env)
        self.assertEqual(fresh["status"], "ready")
        self.assertEqual(fresh["acceptance"], "not_verified")
        self.refresh(repo, "readiness-task", env)
        reviews = self.pending(repo, "readiness-task", "review_required")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["phaseId"], "P-READY")
        self.assertEqual(reviews[0]["candidate"]["head"], candidate)

    def test_brief_uses_contract_command_timeout_and_keeps_round_timeout(self):
        repo, worktree = self.make(name="brief-phase")
        env = self.h_env(PI_DOUBLE_MODE="hang")
        try:
            sha = self.write_design(repo)
            path = self.write_contract(
                "brief.json", self.contract(repo, "P-BRIEF", design_sha=sha))
            self.start(repo, worktree, "brief-phase", path, env)
            repo.wait_round_state("brief-phase", "running")
            brief = (repo.task_dir("brief-phase") / "rounds" / "1" / "brief.md").read_text(
                encoding="utf-8")
            self.assertIn("command_timeout_seconds=900", brief)
            self.assertIn("--timeout-seconds 900", brief)
            self.assertNotIn("--timeout-seconds 14400", brief)
            self.assertIn("whole-round", brief)
            self.assertIn("declared command", brief)
        finally:
            repo.cancel("brief-phase", env=env)
            repo.wait_terminal("brief-phase", env=env, timeout=25)
        legacy, legacy_wt = self.make(name="brief-legacy")
        try:
            self.start(legacy, legacy_wt, "brief-legacy", None, env)
            legacy.wait_round_state("brief-legacy", "running")
            legacy_brief = (legacy.task_dir("brief-legacy") / "rounds" / "1"
                            / "brief.md").read_text(encoding="utf-8")
            self.assertIn("--timeout-seconds 14400", legacy_brief)
            self.assertNotIn("command_timeout_seconds", legacy_brief)
        finally:
            legacy.cancel("brief-legacy", env=env)
            legacy.wait_terminal("brief-legacy", env=env, timeout=25)

    def test_receipt_command_identity_gates_readiness_board_and_accept(self):
        fixture = self.ready_accept_fixture("cmd-gate", "P-CMDGATE")
        repo, env = fixture["repo"], fixture["env"]
        task, candidate = "cmd-gate", fixture["candidate"]
        checks = repo.task_dir(task) / "rounds" / "1" / "round.checks"
        review = fixture["review"]

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task", task,
                            "--round", "1", env=env)

        def refuse_accept():
            proc = run_board("decide", "--repo", str(repo.root), "--task", task,
                             "--event-id", review["id"], "--decision", "accept",
                             "--reviewed-head", candidate, "--phase", "P-CMDGATE",
                             "--contract-hash", fixture["contractSha256"], env=env, expect=2)
            self.assertIn("readiness", proc.stderr)

        # Known mismatch: same check id and head, different argv -> failed. The
        # live accept gate re-reads the same normalized verdict and refuses.
        mismatched = self.synth_receipt(checks, "A1", 0, candidate,
                                        argv=["python3", "-c", "other"])
        item = readiness()["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("argv", item["reason"])
        refuse_accept()

        # Malformed identity: argv is not a list -> unknown, never a pass.
        malformed = self.synth_receipt(checks, "A1", 0, candidate, argv="not-a-list")
        item = readiness()["items"][0]
        self.assertEqual(item["status"], "unknown")
        self.assertIn("argv", item["reason"])
        refuse_accept()

        # The board consumes the same verdict: the old review event is
        # superseded and the blocked event carries the failed item.
        self.refresh(repo, task, env)
        self.assertEqual(self.pending(repo, task, "review_required"), [])
        blocked = self.pending(repo, task, "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "required_check_failed")
        # Gate-failed attempts stay on disk as preserved evidence.
        self.assertTrue(mismatched.is_file())
        self.assertTrue(malformed.is_file())

        # A fresh matching receipt covers again on the same candidate; the
        # superseded review event is not re-published for the same fingerprint.
        self.synth_receipt(checks, "A1", 0, candidate, command=fixture["command"])
        self.assertEqual(readiness()["status"], "ready")
        self.assertEqual(readiness()["items"][0]["status"], "covered")

    def test_receipt_timeout_bound_gates_readiness_board_and_accept(self):
        fixture = self.ready_accept_fixture("timeout-gate", "P-TIMEOUTGATE")
        repo, env = fixture["repo"], fixture["env"]
        task, candidate = "timeout-gate", fixture["candidate"]
        checks = repo.task_dir(task) / "rounds" / "1" / "round.checks"
        review = fixture["review"]

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task", task,
                            "--round", "1", env=env)

        def refuse_accept():
            proc = run_board("decide", "--repo", str(repo.root), "--task", task,
                             "--event-id", review["id"], "--decision", "accept",
                             "--reviewed-head", candidate, "--phase", "P-TIMEOUTGATE",
                             "--contract-hash", fixture["contractSha256"], env=env, expect=2)
            self.assertIn("readiness", proc.stderr)

        # Over-cap wrapper deadline -> failed.
        over = self.synth_receipt(checks, "A1", 0, candidate, command=fixture["command"],
                                  timeout_seconds=901)
        item = readiness()["items"][0]
        self.assertEqual(item["status"], "failed")
        self.assertIn("timeout", item["reason"])
        refuse_accept()

        # Missing deadline -> unknown, never a pass.
        missing = self.synth_receipt(checks, "A1", 0, candidate, command=fixture["command"])
        data = json.loads(missing.read_text(encoding="utf-8"))
        del data["deadline_at"]
        missing.write_text(json.dumps(data), encoding="utf-8")
        item = readiness()["items"][0]
        self.assertEqual(item["status"], "unknown")
        self.assertIn("timing", item["reason"])
        refuse_accept()

        # Contradictory deadline -> unknown, never a pass.
        self.synth_receipt(checks, "A1", 0, candidate, command=fixture["command"],
                           deadline_at=time.time() - 100)
        item = readiness()["items"][0]
        self.assertEqual(item["status"], "unknown")
        self.assertIn("contradictory", item["reason"])
        refuse_accept()

        # The board shares the verdict; failed attempts stay on disk.
        self.refresh(repo, task, env)
        self.assertEqual(self.pending(repo, task, "review_required"), [])
        self.assertEqual(len(self.pending(repo, task, "phase_blocked")), 1)
        self.assertTrue(over.is_file())
        self.assertTrue(missing.is_file())

        # A within-cap matching receipt restores coverage on the same candidate.
        self.synth_receipt(checks, "A1", 0, candidate, command=fixture["command"])
        self.assertEqual(readiness()["status"], "ready")
        self.assertEqual(readiness()["items"][0]["status"], "covered")

    def test_failed_required_check_escalates_without_auto_continue(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="3")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-FAILED", design_sha=sha))
        self.start(repo, worktree, "failed-check-task", path, env)
        repo.wait_round_state("failed-check-task", "running")
        checks = repo.task_dir("failed-check-task") / "rounds" / "1" / "round.checks"
        self.synth_receipt(checks, "A1", 3, self.head(worktree))
        repo.wait_terminal("failed-check-task")
        self.assertEqual(self.rounds(repo, "failed-check-task"), [1])
        self.assertEqual(self.phase_auto(repo, "failed-check-task"), {})
        self.register(repo, "failed-check-task", env)
        self.refresh(repo, "failed-check-task", env)
        blocked = self.pending(repo, "failed-check-task", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "required_check_failed")

    # ------------------------------------------------------------------
    # O1-4 / O1-5 classification and one auto-continuation
    # ------------------------------------------------------------------
    def test_running_check_timeout_stays_local_and_never_queues(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="hang")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-LOCAL", design_sha=sha))
        self.start(repo, worktree, "local-timeout", path, env)
        repo.wait_round_state("local-timeout", "running")
        try:
            self.register(repo, "local-timeout", env, transport="cli-queue", thread=THREAD_A)
            checks = repo.task_dir("local-timeout") / "rounds" / "1" / "round.checks"
            self.synth_receipt(checks, "A1", 124, self.head(worktree), timed_out=True)
            self.refresh(repo, "local-timeout", env)
            self.refresh(repo, "local-timeout", env)
            kinds = {event["kind"] for event in
                     json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))[
                         "cards"]["local-timeout"]["events"] if not event.get("handled")}
            self.assertEqual(kinds, set(), "a running self-repairable timeout must not wake GPT")
            self.assertFalse(self.fake_marker.exists(),
                             "a local check failure never invokes the queue CLI")
        finally:
            repo.cancel("local-timeout", env=env)
            repo.wait_terminal("local-timeout", env=env, timeout=25)

    def test_one_auto_continuation_reuses_session_worktree_and_budget(self):
        repo, worktree = self.make()
        trace = self.tmp / "trace.jsonl"
        env = self.h_env(PI_DOUBLE_MODE="session-trace", PI_DOUBLE_TRACE=str(trace))
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-AUTO", design_sha=sha,
                                                           budget=3600))
        self.start(repo, worktree, "auto-task", path, env)
        self.wait_rounds(repo, "auto-task", 2)
        # Both rounds are terminal and the single quota is spent.
        self.wait_for(lambda: all(
            json.loads((repo.task_dir("auto-task") / "rounds" / str(n) / "round.state.json")
                       .read_text(encoding="utf-8"))["state"] == "completed" for n in (1, 2)),
            timeout=30, what="both auto rounds completed")
        recorded = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(recorded), 2, "exactly one automatic continuation round ran")
        self.assertEqual(recorded[0]["sessionId"], recorded[1]["sessionId"])
        self.assertEqual(recorded[0]["sessionDir"], recorded[1]["sessionDir"])
        self.assertEqual(recorded[0]["cwd"], recorded[1]["cwd"])
        self.assertEqual(Path(recorded[1]["cwd"]).resolve(), worktree.resolve())
        brief = (repo.task_dir("auto-task") / "rounds" / "2" / "brief.md").read_text(encoding="utf-8")
        self.assertIn("delivery gap repair", brief)
        self.assertIn("A1", brief)
        state = self.phase_state(repo, "auto-task")
        self.assertAlmostEqual(state["budgetSeconds"], 3600, delta=1)
        self.assertAlmostEqual(state["deadlineAt"] - state["startedAt"], 3600, delta=1)
        self.wait_for(lambda: self.phase_auto(repo, "auto-task").get("P-AUTO", {}).get(
            "status") == "exhausted", timeout=20, what="auto ledger exhausted")
        state = self.phase_state(repo, "auto-task")
        self.assertTrue(state["autoContinue"]["used"])
        self.assertEqual(state["autoContinue"]["status"], "exhausted")
        self.register(repo, "auto-task", env)
        self.refresh(repo, "auto-task", env)
        blocked = self.pending(repo, "auto-task", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "auto_continue_used")
        self.assertEqual(self.pending(repo, "auto-task", "review_required"), [])
        revision = json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))["revision"]
        self.refresh(repo, "auto-task", env)
        self.assertEqual(json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))["revision"],
                         revision, "a repeated refresh must not add duplicate events")
        # The spent quota is durable: no second automatic round can appear.
        self.assertEqual(self.rounds(repo, "auto-task"), [1, 2])

    def test_pause_blocks_the_automatic_continuation(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="4")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-PAUSED", design_sha=sha))
        self.start(repo, worktree, "paused-task", path, env)
        repo.wait_round_state("paused-task", "running")
        self.register(repo, "paused-task", env)
        board_json("pause", "--repo", str(repo.root), "--task", "paused-task",
                   "--note", "user paused", env=env)
        repo.wait_terminal("paused-task")
        self.assertEqual(self.rounds(repo, "paused-task"), [1], "a paused phase never auto-continues")
        self.assertEqual(self.phase_auto(repo, "paused-task"), {})
        self.refresh(repo, "paused-task", env)
        blocked = self.pending(repo, "paused-task", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "paused")
        # Resuming explicitly does not resurrect the spent automatic decision.
        board_json("resume", "--repo", str(repo.root), "--task", "paused-task", env=env)
        self.refresh(repo, "paused-task", env)
        self.assertEqual(self.rounds(repo, "paused-task"), [1])

    def test_unknown_auto_continue_start_escalates_without_retry(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="ok")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-UNKNOWN", design_sha=sha))
        self.start(repo, worktree, "unknown-start", path, env)
        self.wait_rounds(repo, "unknown-start", 2)
        self.wait_for(lambda: self.phase_auto(repo, "unknown-start").get("P-UNKNOWN", {}).get(
            "status") == "exhausted", timeout=20, what="auto ledger exhausted")
        # Simulate a crash after the claim but before the continuation started.
        ledger = json.loads((repo.task_dir("unknown-start") / "phase-auto.json").read_text(encoding="utf-8"))
        ledger["phases"]["P-UNKNOWN"]["status"] = "claimed"
        (repo.task_dir("unknown-start") / "phase-auto.json").write_text(
            json.dumps(ledger), encoding="utf-8")
        self.register(repo, "unknown-start", env)
        self.refresh(repo, "unknown-start", env)
        blocked = self.pending(repo, "unknown-start", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "auto_continue_unknown")
        self.assertEqual(self.rounds(repo, "unknown-start"), [1, 2],
                         "an unknown start result is never retried")

    def test_budget_exhaustion_blocks_auto_continue(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="ok")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-BUDGET", design_sha=sha,
                                                           budget=1))
        self.start(repo, worktree, "budget-task", path, env)
        repo.wait_terminal("budget-task")
        self.assertEqual(self.rounds(repo, "budget-task"), [1])
        self.assertEqual(self.phase_auto(repo, "budget-task"), {})
        self.register(repo, "budget-task", env)
        self.refresh(repo, "budget-task", env)
        blocked = self.pending(repo, "budget-task", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "budget_exhausted")

    def test_nonzero_exit_never_auto_continues(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="fail")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-FAIL-EXIT", design_sha=sha))
        self.start(repo, worktree, "fail-task", path, env)
        repo.wait_terminal("fail-task")
        self.assertEqual(self.rounds(repo, "fail-task"), [1])
        self.assertEqual(self.phase_auto(repo, "fail-task"), {})
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "fail-task",
                             "--round", "1", env=env)
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(readiness["evidenceLevels"]["deliveryReadiness"], "not_ready")
        self.register(repo, "fail-task", env)
        self.refresh(repo, "fail-task", env)
        self.assertEqual(self.pending(repo, "fail-task", "review_required"), [])
        blocked = self.pending(repo, "fail-task", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "round_execution_failed")

    def test_out_of_scope_change_escalates_instead_of_auto_continuing(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="4")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-SCOPE", design_sha=sha,
                                                           scope=["docs/"]))
        self.start(repo, worktree, "scope-task", path, env)
        repo.wait_round_state("scope-task", "running")
        (worktree / "outside.txt").write_text("out of scope\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@e.invalid",
                        "-c", "user.name=T", "commit", "-qm", "outside scope"],
                       check=True, capture_output=True)
        repo.wait_terminal("scope-task")
        self.assertEqual(self.rounds(repo, "scope-task"), [1])
        self.register(repo, "scope-task", env)
        self.refresh(repo, "scope-task", env)
        blocked = self.pending(repo, "scope-task", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "scope_violation")

    def test_auto_continue_claim_is_single_per_phase(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="hang")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-QUOTA", design_sha=sha))
        self.start(repo, worktree, "quota-task", path, env)
        repo.wait_round_state("quota-task", "running")
        try:
            task_dir = repo.task_dir("quota-task")
            task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
            first, problem = pi_task._claim_auto_continue(task_dir, "P-QUOTA", "a" * 64, 1,
                                                          "missing_checks", task)
            self.assertIsNotNone(first, problem)
            second, existing = pi_task._claim_auto_continue(task_dir, "P-QUOTA", "a" * 64, 1,
                                                            "missing_checks", task)
            self.assertIsNone(second)
            self.assertTrue(existing["used"])
            ledger = pi_task.read_phase_auto(task_dir)[0]
            self.assertEqual(list(ledger["phases"]), ["P-QUOTA"])
            self.assertEqual(ledger["phases"]["P-QUOTA"]["round"], 2)
        finally:
            repo.cancel("quota-task", env=env)
            repo.wait_terminal("quota-task", env=env, timeout=25)

    def test_scope_check_covers_all_changed_files_beyond_a_prefix(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="6")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-501", design_sha=sha,
                                                           scope=["docs/"]))
        self.start(repo, worktree, "scope-501", path, env)
        repo.wait_round_state("scope-501", "running")
        # 500 sorted in-scope files plus one out-of-scope file that sorts after
        # them: a prefix-only check would miss the violation.
        docs = worktree / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        for index in range(500):
            (docs / f"f{index:04d}.txt").write_text("x\n", encoding="utf-8")
        (worktree / "zzz-outside.txt").write_text("outside\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@e.invalid",
                        "-c", "user.name=T", "commit", "-qm", "many files"], check=True,
                       capture_output=True)
        repo.wait_terminal("scope-501")
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "scope-501",
                             "--round", "1", env=env)
        self.assertEqual(readiness["scope"]["status"], "violation")
        self.assertIn("zzz-outside.txt", readiness["scope"]["outOfScope"])
        self.assertEqual(readiness["status"], "not_ready")
        self.assertFalse(readiness["readyForReview"])
        self.register(repo, "scope-501", env)
        self.refresh(repo, "scope-501", env)
        self.assertEqual(self.pending(repo, "scope-501", "review_required"), [])
        blocked = self.pending(repo, "scope-501", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "scope_violation")

    def test_scope_check_overflow_is_unknown_and_blocks_ready(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="6")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-CAP", design_sha=sha,
                                                           scope=["."]))
        self.start(repo, worktree, "scope-cap", path, env)
        repo.wait_round_state("scope-cap", "running")
        docs = worktree / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        for index in range(pi_task.MAX_SCOPE_DIFF_FILES + 10):
            (docs / f"c{index:05d}.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@e.invalid",
                        "-c", "user.name=T", "commit", "-qm", "overflow files"], check=True,
                       capture_output=True)
        candidate = self.head(worktree)
        checks = repo.task_dir("scope-cap") / "rounds" / "1" / "round.checks"
        self.synth_receipt(checks, "A1", 0, candidate)
        repo.wait_terminal("scope-cap")
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "scope-cap",
                             "--round", "1", env=env)
        self.assertEqual(readiness["scope"]["status"], "unknown")
        self.assertIn("bounded scope check", readiness["scope"]["reason"])
        self.assertEqual(readiness["status"], "unknown")
        self.assertFalse(readiness["readyForReview"])

    def test_count_rules_cover_and_block_correctly(self):
        # Positive: every declared rule combination reaches covered/ready.
        repo, worktree = self.make(name="counts-ready")
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="5")
        sha = self.write_design(repo)
        positive = [
            {"id": "A1", "description": "skip-free tests", "command": "go test ./...",
             "passCondition": "exit 0, no skip", "evidence": "receipt", "forbidSkip": True},
            {"id": "A2", "description": "minimum test count", "command": "go test ./...",
             "passCondition": "exit 0, run>=2", "evidence": "receipt", "minRun": 2},
            {"id": "A3", "description": "both rules", "command": "go test ./...",
             "passCondition": "exit 0, run>=2, no skip", "evidence": "receipt",
             "forbidSkip": True, "minRun": 2},
        ]
        path = self.write_contract("ok.json", self.contract(repo, "P-COUNTS-OK", design_sha=sha,
                                                               items=positive))
        self.start(repo, worktree, "counts-ready", path, env)
        repo.wait_round_state("counts-ready", "running")
        candidate = self.head(worktree)
        checks = repo.task_dir("counts-ready") / "rounds" / "1" / "round.checks"
        counts = {"run": 3, "pass": 3, "fail": 0, "skip": 0,
                  "format": "go_verbose_top_level"}
        for item in ("A1", "A2", "A3"):
            self.synth_receipt(checks, item, 0, candidate, counts=dict(counts),
                               command="go test ./...")
        repo.wait_terminal("counts-ready")
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "counts-ready",
                             "--round", "1", env=env)
        by_id = {item["id"]: item for item in readiness["items"]}
        self.assertTrue(all(by_id[item]["status"] == "covered" for item in ("A1", "A2", "A3")),
                        [item["status"] for item in readiness["items"]])
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["coverage"]["covered"], 3)

        # Negative: missing fields, skips, short runs and combined rules all
        # stay blocked with the exact classification.
        repo2, worktree2 = self.make(name="counts-block")
        env2 = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="5")
        sha2 = self.write_design(repo2)
        negative = [
            {"id": "B1", "description": "skip-free", "command": "go test",
             "passCondition": "no skip", "evidence": "receipt", "forbidSkip": True},
            {"id": "B2", "description": "skip-free", "command": "go test",
             "passCondition": "no skip", "evidence": "receipt", "forbidSkip": True},
            {"id": "B3", "description": "min run", "command": "go test",
             "passCondition": "run>=5", "evidence": "receipt", "minRun": 5},
            {"id": "B4", "description": "both", "command": "go test",
             "passCondition": "run>=5 no skip", "evidence": "receipt",
             "forbidSkip": True, "minRun": 5},
            {"id": "B5", "description": "both skip", "command": "go test",
             "passCondition": "run>=2 no skip", "evidence": "receipt",
             "forbidSkip": True, "minRun": 2},
            {"id": "B6", "description": "both no counts", "command": "go test",
             "passCondition": "run>=2 no skip", "evidence": "receipt",
             "forbidSkip": True, "minRun": 2},
        ]
        path2 = self.write_contract("block.json", self.contract(repo2, "P-COUNTS-BLOCK",
                                                                 design_sha=sha2, items=negative))
        self.start(repo2, worktree2, "counts-block", path2, env2)
        repo2.wait_round_state("counts-block", "running")
        candidate2 = self.head(worktree2)
        checks2 = repo2.task_dir("counts-block") / "rounds" / "1" / "round.checks"
        self.synth_receipt(checks2, "B1", 0, candidate2, command="go test")  # no counts -> unknown
        self.synth_receipt(checks2, "B2", 0, candidate2, command="go test",
                           counts={"run": 3, "pass": 2, "fail": 0, "skip": 1})
        self.synth_receipt(checks2, "B3", 0, candidate2, command="go test",
                           counts={"run": 3, "pass": 3, "fail": 0, "skip": 0})
        # Both rules declared: minRun must still be checked after forbidSkip.
        self.synth_receipt(checks2, "B4", 0, candidate2, command="go test",
                           counts={"run": 3, "pass": 3, "fail": 0, "skip": 0})
        self.synth_receipt(checks2, "B5", 0, candidate2, command="go test",
                           counts={"run": 3, "pass": 1, "fail": 0, "skip": 2})
        self.synth_receipt(checks2, "B6", 0, candidate2, command="go test")  # no counts -> unknown
        repo2.wait_terminal("counts-block")
        readiness2 = cli_json("readiness", "--repo", str(repo2.root), "--task", "counts-block",
                              "--round", "1", env=env2)
        by_id2 = {item["id"]: item["status"] for item in readiness2["items"]}
        self.assertEqual(by_id2, {"B1": "unknown", "B2": "skipped", "B3": "failed",
                                  "B4": "failed", "B5": "skipped", "B6": "unknown"})
        self.assertEqual(readiness2["status"], "not_ready")
        self.assertEqual(self.rounds(repo2, "counts-block"), [1],
                         "blocked count rules never auto-continue")

    def test_active_candidate_tracks_a_mid_round_commit(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="15")
        sha = self.write_design(repo)
        command = self.python_command("-c", "print('mid')")
        item = [{"id": "A1", "description": "the behavior works", "command": command,
                 "passCondition": "exit 0", "evidence": "pi_check receipt for A1"}]
        path = self.write_contract("p.json", self.contract(repo, "P-MID", design_sha=sha,
                                                            items=item))
        self.start(repo, worktree, "mid-head", path, env)
        repo.wait_round_state("mid-head", "running")
        start_head = self.head(worktree)
        checks = repo.task_dir("mid-head") / "rounds" / "1" / "round.checks"
        old = subprocess.run(
            [sys.executable, str(CHECK), "--output-dir", str(checks), "--id", "A1",
             "--timeout-seconds", "900", "--", *shlex.split(command)], cwd=str(worktree),
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(old.returncode, 0, old.stderr)
        old_receipt = Path(json.loads(old.stdout)["receipt"]).name
        # A real commit while the round is active; the new HEAD becomes the
        # candidate and the old-head receipt must not be treated as current.
        (worktree / "mid.txt").write_text("mid\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@e.invalid",
                        "-c", "user.name=T", "commit", "-qm", "mid round commit"],
                       check=True, capture_output=True)
        new_head = self.head(worktree)
        self.assertNotEqual(new_head, start_head)
        new = subprocess.run(
            [sys.executable, str(CHECK), "--output-dir", str(checks), "--id", "A1",
             "--timeout-seconds", "900", "--", *shlex.split(command)], cwd=str(worktree),
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(new.returncode, 0, new.stderr)
        new_receipt = Path(json.loads(new.stdout)["receipt"]).name
        status = cli_json("status", "--repo", str(repo.root), "--task", "mid-head", env=env)
        self.assertEqual(status["currentHead"], new_head)
        self.assertIsNone(status["endHead"])
        self.register(repo, "mid-head", env, transport="cli-queue", thread=THREAD_A)
        board_json("refresh", "--repo", str(repo.root), "--task", "mid-head", env=env)
        card = self.card(repo, "mid-head")
        self.assertEqual(card["phase"]["candidate"], new_head)
        self.assertEqual(card["evidence"]["candidateHead"], new_head,
                         "the short board candidate and the phase candidate share one source")
        self.assertNotEqual(card["evidence"]["candidateHead"], start_head)
        milestones = self.pending(repo, "mid-head", "progress_update")
        self.assertEqual(len(milestones), 1)
        event = milestones[0]
        self.assertEqual(event["candidate"]["head"], new_head)
        self.assertEqual(event["evidence"]["factSource"], "verified_receipt")
        self.assertEqual(event["evidence"]["logVerified"], True)
        self.assertEqual(Path(event["evidence"]["receiptRef"]).name, new_receipt)
        self.assertNotIn(old_receipt, event["evidence"]["receiptRef"])
        repo.wait_terminal("mid-head", env=env, timeout=30)

    def test_active_head_probe_failure_keeps_old_evidence_out_of_board_and_queue(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="12")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-HEADFAIL", design_sha=sha))
        self.start(repo, worktree, "head-fail", path, env)
        repo.wait_round_state("head-fail", "running")
        start_head = self.head(worktree)
        checks = repo.task_dir("head-fail") / "rounds" / "1" / "round.checks"
        old = subprocess.run(
            [sys.executable, str(CHECK), "--output-dir", str(checks), "--id", "A1",
             "--", sys.executable, "-c", "print('old')"], cwd=str(worktree),
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(old.returncode, 0, old.stderr)
        old_receipt = Path(json.loads(old.stdout)["receipt"]).name
        self.assertEqual(json.loads((checks / old_receipt).read_text())["head"], start_head)
        (worktree / "new.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@e.invalid",
                        "-c", "user.name=T", "commit", "-qm", "new head"], check=True,
                       capture_output=True)
        new_head = self.head(worktree)
        self.assertNotEqual(new_head, start_head)
        self.register(repo, "head-fail", env, transport="cli-queue", thread=THREAD_A)
        board_path = repo.state_dir / "board.json"
        with mock.patch.object(pi_task, "_head_probe",
                               return_value=(None, "simulated probe failure")):
            status = pi_task.build_status(str(repo.root), "head-fail")
        self.assertIsNone(status["currentHead"])
        self.assertIsNone(status["endHead"])
        self.assertTrue(any("current HEAD could not be read" in note
                            for note in status["notes"]))
        refresh = pi_board.refresh_with_status(board_path, "head-fail", status, now=100,
                                               block=False)
        card = self.card(repo, "head-fail")
        self.assertIsNone(card["phase"]["candidate"],
                          "a failed active HEAD probe must not fall back to startHead")
        self.assertIsNone(card["evidence"]["candidateHead"],
                          "the short board must report the unknown candidate honestly")
        self.assertEqual([event for event in card["events"]
                          if event.get("kind") == "progress_update"], [])
        self.assertEqual(refresh["newEvents"], [])
        calls = []

        def runner(argv, timeout):
            calls.append(argv)
            return {"status": "queued", "exitCode": 0, "timedOut": False,
                    "outputSha256": "0" * 64, "outputExcerpt": "", "argv0": argv[0]}

        with mock.patch.object(pi_board, "_resolve_codex_bin", return_value="/bin/true"):
            dispatched = pi_board.dispatch_task(board_path, "head-fail", cli_runner=runner)
        self.assertFalse(dispatched["dispatched"])
        self.assertEqual(calls, [], "an unknown active candidate must never emit a queue message")
        self.assertEqual(card["events"], [])
        self.assertIsNone(card["phase"]["candidate"])
        self.assertIsNone(card["evidence"]["candidateHead"])
        repo.cancel("head-fail", env=env)
        repo.wait_terminal("head-fail", env=env, timeout=25)

    def test_forbid_skip_requires_parseable_counts(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="4")
        sha = self.write_design(repo)
        items = [
            {"id": "A1", "description": "counted tests", "command": "go test ./...",
             "passCondition": "exit 0 and zero skips", "evidence": "receipt",
             "forbidSkip": True},
            {"id": "A2", "description": "non-test check", "command": "git diff --check",
             "passCondition": "exit 0", "evidence": "receipt"},
        ]
        path = self.write_contract("p.json", self.contract(repo, "P-COUNTS", design_sha=sha,
                                                            items=items))
        self.start(repo, worktree, "counts-task", path, env)
        repo.wait_round_state("counts-task", "running")
        candidate = self.head(worktree)
        checks = repo.task_dir("counts-task") / "rounds" / "1" / "round.checks"
        self.synth_receipt(checks, "A1", 0, candidate, command="go test ./...")  # no counts
        self.synth_receipt(checks, "A2", 0, candidate,
                           command="git diff --check")  # non-test, no counts needed
        repo.wait_terminal("counts-task")
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "counts-task",
                             "--round", "1", env=env)
        by_id = {item["id"]: item for item in readiness["items"]}
        self.assertEqual(by_id["A1"]["status"], "unknown")
        self.assertIn("counts", by_id["A1"]["reason"])
        self.assertEqual(by_id["A2"]["status"], "covered")
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(self.rounds(repo, "counts-task"), [1],
                         "unverifiable counts must not auto-continue")

    def test_explicit_continue_is_blocked_while_paused(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="ok")
        self.start(repo, worktree, "pause-continue", None, env)
        self.wait_terminal(repo, "pause-continue")
        self.register(repo, "pause-continue", env)
        board_json("pause", "--repo", str(repo.root), "--task", "pause-continue",
                   "--note", "user paused", env=env)
        proc = run_cli("continue", "--repo", str(repo.root), "--task", "pause-continue",
                       "--prompt", "more", env=env, expect=2)
        self.assertIn("paused", proc.stderr)
        self.assertEqual(len(self.rounds(repo, "pause-continue")), 1)
        board_json("resume", "--repo", str(repo.root), "--task", "pause-continue", env=env)
        run_cli("continue", "--repo", str(repo.root), "--task", "pause-continue",
                "--prompt", "more", env=env, expect=0)
        repo.wait_terminal("pause-continue", round=2)
        self.assertEqual(len(self.rounds(repo, "pause-continue")), 2)

    def test_explicit_continue_is_blocked_by_a_route_pause(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="ok")
        os.environ["CODEX_PI_HANDOFF_ROOT"] = str(self.tmp / "handoffs")
        self.addCleanup(os.environ.pop, "CODEX_PI_HANDOFF_ROOT", None)
        self.start(repo, worktree, "route-continue", None, env)
        self.wait_terminal(repo, "route-continue")
        self.register(repo, "route-continue", env, transport="cli-queue", thread=THREAD_A)
        pi_board.pause_route(THREAD_A, now=time.time())
        proc = run_cli("continue", "--repo", str(repo.root), "--task", "route-continue",
                       "--prompt", "more", env=env, expect=2)
        self.assertIn("paused", proc.stderr)
        self.assertEqual(len(self.rounds(repo, "route-continue")), 1)
        pi_board.resume_route(THREAD_A)
        run_cli("continue", "--repo", str(repo.root), "--task", "route-continue",
                "--prompt", "more", env=env, expect=0)
        repo.wait_terminal("route-continue", round=2)

    def test_late_pause_between_claim_and_start_fails_closed(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="hang")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-LATE", design_sha=sha))
        self.start(repo, worktree, "late-pause", path, env)
        repo.wait_round_state("late-pause", "running")
        # Registering creates the board whose lock the start decision now holds;
        # the mocked pause check reports "not paused" for the decision and
        # "late pause" for the locked re-check.
        self.register(repo, "late-pause", env)
        task_dir = repo.task_dir("late-pause")
        round_dir = task_dir / "rounds" / "1"
        task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        head = self.head(worktree)
        round_state = json.loads((round_dir / "round.state.json").read_text(encoding="utf-8"))
        round_state.update({"state": "completed", "exitCode": 0, "endHead": head,
                            "endedAt": time.time(), "timedOut": False, "cancelled": False})
        (round_dir / "round.state.json").write_text(json.dumps(round_state), encoding="utf-8")
        try:
            with mock.patch.object(pi_task, "board_pause_active",
                                   side_effect=[(False, None), (True, "late pause")]) as pause:
                result = pi_task.post_round_phase(task, task_dir, 1, round_dir,
                                                  {"exitCode": 0, "endHead": head,
                                                   "endedAt": time.time()}, "completed")
            self.assertIsNone(result, "a late pause must not start the continuation")
            self.assertEqual(pause.call_count, 2)
            self.assertFalse((task_dir / "rounds" / "2").exists())
            ledger = self.phase_auto(repo, "late-pause")
            self.assertEqual(ledger["P-LATE"]["status"], "blocked")
            self.assertEqual(ledger["P-LATE"]["reason"], "paused")
            state = self.phase_state(repo, "late-pause")
            self.assertEqual(state["lastDecision"]["reason"], "paused")
        finally:
            repo.cancel("late-pause", env=env)
            repo.wait_terminal("late-pause", env=env, timeout=25)

    def test_unavailable_pause_lock_fails_closed(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="hang")
        sha = self.write_design(repo)
        path = self.write_contract("p.json", self.contract(repo, "P-LOCK", design_sha=sha))
        self.start(repo, worktree, "pause-lock", path, env)
        repo.wait_round_state("pause-lock", "running")
        self.register(repo, "pause-lock", env)
        task_dir = repo.task_dir("pause-lock")
        round_dir = task_dir / "rounds" / "1"
        task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        head = self.head(worktree)
        round_state = json.loads((round_dir / "round.state.json").read_text(encoding="utf-8"))
        round_state.update({"state": "completed", "exitCode": 0, "endHead": head,
                            "endedAt": time.time(), "timedOut": False, "cancelled": False})
        (round_dir / "round.state.json").write_text(json.dumps(round_state), encoding="utf-8")
        import fcntl
        lock_path = repo.state_dir / "board.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            result = pi_task.post_round_phase(task, task_dir, 1, round_dir,
                                              {"exitCode": 0, "endHead": head,
                                               "endedAt": time.time()}, "completed")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertIsNone(result, "an unverifiable pause state must not start a continuation")
        self.assertFalse((task_dir / "rounds" / "2").exists())
        ledger = self.phase_auto(repo, "pause-lock")
        self.assertEqual(ledger["P-LOCK"]["status"], "blocked")
        self.assertEqual(ledger["P-LOCK"]["reason"], "pause_state_unknown")
        repo.cancel("pause-lock", env=env)
        repo.wait_terminal("pause-lock", env=env, timeout=25)

    # ------------------------------------------------------------------
    # O1-6 acceptance gate and stale evidence
    # ------------------------------------------------------------------
    def ready_phase(self, task: str = "gate-task", phase_id: str = "P-GATE"):
        repo, worktree = self.make(name=f"repo-{task}")
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="4")
        sha = self.write_design(repo)
        path = self.write_contract(f"{task}.json", self.contract(repo, phase_id, design_sha=sha))
        self.start(repo, worktree, task, path, env)
        repo.wait_round_state(task, "running")
        checks = repo.task_dir(task) / "rounds" / "1" / "round.checks"
        self.synth_receipt(checks, "A1", 0, self.head(worktree))
        repo.wait_terminal(task)
        self.register(repo, task, env)
        self.refresh(repo, task, env)
        return repo, worktree, env

    def test_accept_binds_phase_contract_and_candidate(self):
        repo, worktree, env = self.ready_phase()
        review = self.pending(repo, "gate-task", "review_required")
        self.assertEqual(len(review), 1)
        event = review[0]
        head = self.head(worktree)
        self.assertEqual(event["candidate"]["head"], head)
        frozen = json.loads((repo.task_dir("gate-task") / "phase.json").read_text(encoding="utf-8"))
        contract_hash = frozen["contractSha256"]
        base = ["decide", "--repo", str(repo.root), "--task", "gate-task",
                "--event-id", event["id"], "--decision", "accept", "--reviewed-head", head]
        run_board(*base, env=env, expect=2)
        run_board(*base, "--phase", "P-GATE", "--contract-hash", "0" * 64, env=env, expect=2)
        run_board(*base, "--phase", "WRONG", "--contract-hash", contract_hash, env=env, expect=2)
        decided = board_json(*base, "--phase", "P-GATE", "--contract-hash", contract_hash, env=env)
        self.assertEqual(decided["decision"], "accepted")
        self.assertEqual(decided["phaseId"], "P-GATE")
        self.refresh(repo, "gate-task", env)
        card = self.card(repo, "gate-task")
        self.assertEqual(card["phase"]["status"], "accepted")
        self.assertEqual(card["phase"]["acceptedHead"], head)
        self.assertEqual(self.pending(repo, "gate-task"), [])

    def python_command(self, *args) -> str:
        """Declared command string matching a receipt run with the same argv."""
        return shlex.join([sys.executable, *args])

    def ready_accept_fixture(self, name: str, phase_id: str, *,
                             command: str | None = None,
                             command_timeout: float = 900,
                             resource_limits: list | None = None) -> dict:
        """Real passing receipt, pending review event and writer-free terminal state."""
        repo, worktree = self.make(name=f"repo-{name}")
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="6")
        sha = self.write_design(repo)
        command = command or self.python_command("-c", "print('ok')")
        item = {"id": "A1", "description": "the behavior works", "command": command,
                "passCondition": "exit 0", "evidence": "pi_check receipt for A1"}
        extra = {"commandTimeoutSeconds": command_timeout}
        if resource_limits is not None:
            extra["resourceLimits"] = resource_limits
        path = self.write_contract(f"{name}.json", self.contract(
            repo, phase_id, design_sha=sha, items=[item], extra=extra))
        self.start(repo, worktree, name, path, env)
        repo.wait_round_state(name, "running")
        candidate = self.head(worktree)
        checks = repo.task_dir(name) / "rounds" / "1" / "round.checks"
        check = subprocess.run(
            [sys.executable, str(CHECK), "--output-dir", str(checks), "--id", "A1",
             "--timeout-seconds", f"{command_timeout:g}",
             "--", *shlex.split(command)], cwd=str(worktree),
            capture_output=True, text=True, env=env, timeout=60)
        if check.returncode != 0:
            raise AssertionError(check.stderr)
        self.register(repo, name, env, transport="cli-queue", thread=THREAD_A)
        board_json("refresh", "--repo", str(repo.root), "--task", name, env=env)
        repo.wait_terminal(name)

        def writer_free():
            live = cli_json("status", "--repo", str(repo.root), "--task", name, env=env)
            if not live["ownership"]["activeWorker"] and not live["ownership"]["supervisorAlive"]:
                return live
            return None

        self.wait_for(writer_free, timeout=20, what=f"writer-free status for {name}")
        board_json("refresh", "--repo", str(repo.root), "--task", name, env=env)
        review = self.pending(repo, name, "review_required")
        if len(review) != 1:
            raise AssertionError(f"expected one review event for {name}, got {len(review)}")
        frozen = json.loads((repo.task_dir(name) / "phase.json").read_text(encoding="utf-8"))
        return {"repo": repo, "worktree": worktree, "env": env, "candidate": candidate,
                "command": command, "review": review[0],
                "contractSha256": frozen["contractSha256"]}

    def test_terminal_execution_facts_are_enforced_by_the_gate(self):
        # Known failure (nonzero exit, cancelled, timed out) is not_ready;
        # a missing exit code is unknown. None of them may pass the gate.
        cases = [
            ("nonzero-exit", {"exitCode": 7}, "not_ready"),
            ("missing-exit", {"exitCode": None}, "unknown"),
            ("cancelled", {"cancelled": True}, "not_ready"),
            ("timed-out", {"timedOut": True}, "not_ready"),
        ]
        for label, mutation, expected in cases:
            with self.subTest(case=label):
                fixture = self.ready_accept_fixture(f"exec-{label}",
                                                    "P-EXEC-" + label.upper().replace("-", ""))
                repo, env = fixture["repo"], fixture["env"]
                task = f"exec-{label}"
                candidate = fixture["candidate"]
                state_path = repo.task_dir(task) / "rounds" / "1" / "round.state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state.update(mutation)
                state_path.write_text(json.dumps(state), encoding="utf-8")
                status = cli_json("status", "--repo", str(repo.root), "--task", task, env=env)
                self.assertEqual(status["phase"]["readiness"]["status"], expected, label)
                self.assertFalse(status["phase"]["readiness"].get("readyForReview"), label)
                readiness = cli_json("readiness", "--repo", str(repo.root), "--task", task,
                                     "--round", "1", env=env)
                self.assertEqual(readiness["status"], expected, label)
                self.assertFalse(readiness["readyForReview"], label)
                # The old review event predates the mutation; the live accept
                # gate must refuse it before any board refresh.
                proc = run_board("decide", "--repo", str(repo.root), "--task", task,
                                 "--event-id", fixture["review"]["id"], "--decision", "accept",
                                 "--reviewed-head", candidate,
                                 "--phase", fixture["review"]["phaseId"],
                                 "--contract-hash", fixture["contractSha256"], env=env, expect=2)
                self.assertIn("stale", proc.stderr, label)
                # A refresh must not claim ready and must supersede the obsolete
                # review event instead of leaving it current.
                board_json("refresh", "--repo", str(repo.root), "--task", task, env=env)
                card = self.card(repo, task)
                self.assertNotEqual(card["phase"]["status"], "review_ready", label)
                self.assertEqual(self.pending(repo, task, "review_required"), [], label)
                self.assertTrue(self.pending(repo, task, "phase_blocked"), label)

    def test_snapshot_identity_and_grades_are_consistent_across_components(self):
        repo, worktree = self.make()
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="6")
        sha = self.write_design(repo)
        command = self.python_command("-c", "print('ok')")
        item = [{"id": "A1", "description": "the behavior works", "command": command,
                 "passCondition": "exit 0", "evidence": "pi_check receipt for A1"}]
        path = self.write_contract("p.json", self.contract(repo, "P-SNAP", design_sha=sha,
                                                            items=item))
        self.start(repo, worktree, "snap-task", path, env)
        repo.wait_round_state("snap-task", "running")
        candidate = self.head(worktree)
        checks = repo.task_dir("snap-task") / "rounds" / "1" / "round.checks"
        check = subprocess.run(
            [sys.executable, str(CHECK), "--output-dir", str(checks), "--id", "A1",
             "--timeout-seconds", "900", "--", *shlex.split(command)], cwd=str(worktree),
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(check.returncode, 0, check.stderr)
        # While the round is active the verified snapshot item produces one
        # bounded progress echo bound to the same candidate as the board.
        self.register(repo, "snap-task", env, transport="cli-queue", thread=THREAD_A)
        board_json("refresh", "--repo", str(repo.root), "--task", "snap-task", env=env)
        active_card = self.card(repo, "snap-task")
        self.assertEqual(active_card["phase"]["candidate"], candidate)
        self.assertEqual(active_card["evidence"]["candidateHead"], candidate)
        milestones = self.pending(repo, "snap-task", "progress_update")
        self.assertEqual(len(milestones), 1)
        self.assertEqual(milestones[0]["candidate"]["head"], candidate)
        self.assertEqual(milestones[0]["evidence"]["factSource"], "verified_receipt")
        repo.wait_terminal("snap-task")

        def writer_free():
            live = cli_json("status", "--repo", str(repo.root), "--task", "snap-task", env=env)
            if not live["ownership"]["activeWorker"] and not live["ownership"]["supervisorAlive"]:
                return live
            return None

        status = self.wait_for(writer_free, timeout=20, what="writer-free status")
        self.assertEqual(status["candidate"], {"status": "known", "head": candidate,
                                               "source": "endHead", "reason": None})
        evidence = status["phase"]["evidence"]
        self.assertEqual(evidence["candidate"]["head"], candidate)
        self.assertEqual(status["phase"]["candidate"], candidate)
        self.assertEqual(evidence["items"][0]["status"], "covered")
        self.assertTrue(evidence["items"][0]["logVerified"])
        self.assertEqual(evidence["items"][0]["evidenceLevel"], "verified_result")
        self.assertEqual(evidence["evidenceLevels"]["deliveryReadiness"], "ready")
        self.assertFalse(evidence["evidenceLevels"]["gptAcceptance"])
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "snap-task",
                             "--round", "1", env=env)
        self.assertEqual(readiness["candidate"], candidate)
        self.assertEqual(readiness["candidateSource"]["head"], candidate)
        self.assertEqual(readiness["items"][0]["status"], "covered")
        self.assertEqual(readiness["status"], "ready")
        board_json("refresh", "--repo", str(repo.root), "--task", "snap-task", env=env)
        card = self.card(repo, "snap-task")
        self.assertEqual(card["phase"]["candidate"], candidate)
        self.assertEqual(card["evidence"]["candidateHead"], candidate)
        self.assertEqual(len(self.pending(repo, "snap-task", "progress_update")), 1)
        review = self.pending(repo, "snap-task", "review_required")
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["candidate"]["head"], candidate)
        frozen = json.loads((repo.task_dir("snap-task") / "phase.json").read_text(encoding="utf-8"))
        decided = board_json("decide", "--repo", str(repo.root), "--task", "snap-task",
                             "--event-id", review[0]["id"], "--decision", "accept",
                             "--reviewed-head", candidate, "--phase", "P-SNAP",
                             "--contract-hash", frozen["contractSha256"], env=env)
        self.assertEqual(decided["decision"], "accepted")
        self.refresh(repo, "snap-task", env)
        card = self.card(repo, "snap-task")
        self.assertEqual(card["phase"]["status"], "accepted")
        self.assertEqual(card["phase"]["acceptedHead"], candidate)

    def test_stale_phase_event_cannot_be_accepted(self):
        repo, worktree, env = self.ready_phase(task="stale-task", phase_id="P-STALE")
        event = self.pending(repo, "stale-task", "review_required")[0]
        head = self.head(worktree)
        frozen = json.loads((repo.task_dir("stale-task") / "phase.json").read_text(encoding="utf-8"))
        contract_sha = frozen["contractSha256"]
        # 1) A real commit after the terminal round moves the worktree HEAD away
        #    from the reviewed candidate; the live accept gate must refuse.
        (worktree / "late.txt").write_text("late\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@e.invalid",
                        "-c", "user.name=T", "commit", "-qm", "late commit"], check=True,
                       capture_output=True)
        late_head = self.head(worktree)
        self.assertNotEqual(late_head, head)
        decide = ["decide", "--repo", str(repo.root), "--task", "stale-task",
                  "--event-id", event["id"]]
        proc = run_board(*decide, "--decision", "accept", "--reviewed-head", head,
                         "--phase", "P-STALE", "--contract-hash", contract_sha,
                         env=env, expect=2)
        self.assertIn("stale", proc.stderr)
        # 2) Installing a live contract revision invalidates the old event even
        #    when the candidate is unchanged.
        revised = dict(frozen["contract"], result="revised complete result")
        phase_record, _problem = pi_task.read_phase_record(repo.task_dir("stale-task"))
        pi_task.install_phase_contract(repo.task_dir("stale-task"), revised, repo.root,
                                       worktree, prior=phase_record)
        proc = run_board(*decide, "--decision", "accept", "--reviewed-head", head,
                         "--phase", "P-STALE", "--contract-hash", contract_sha,
                         env=env, expect=2)
        self.assertIn("stale", proc.stderr)
        # 3) A new round also makes the old round's review event stale.
        revised_path = self.write_contract("revised.json", revised)
        run_cli("continue", "--repo", str(repo.root), "--task", "stale-task",
                "--prompt", "next round", "--contract-file", str(revised_path),
                env=env, expect=0)
        proc = run_board(*decide, "--decision", "accept", "--reviewed-head", head,
                         "--phase", "P-STALE", "--contract-hash", contract_sha,
                         env=env, expect=2)
        self.assertIn("stale", proc.stderr)
        repo.wait_terminal("stale-task", env=env, round=2, timeout=30)

    def test_stale_event_is_not_dispatched_and_is_marked_superseded(self):
        repo, _worktree = self.make()
        board = {"schemaVersion": 1, "revision": 1, "createdAt": 1, "updatedAt": 1, "cards": {}}
        card = pi_board._new_card("stale-dispatch", THREAD_A, "t", "g", None, None,
                                  str(repo.root), str(repo.state_dir), str(repo.root),
                                  pi_board.TRANSPORT_CLI_QUEUE, None, 1)
        card["pi"] = {"round": 2, "state": "completed", "stage": "review", "updatedAt": 1}
        card["phase"] = {"phaseId": "P", "contractHash": "a" * 64, "candidate": "b" * 40,
                         "status": "blocked", "readiness": {"status": "not_ready"}}
        event = pi_board.add_event(card, "phase_blocked", 2, "p:old", "old", {"head": "c" * 40},
                                   {}, "q", 1)
        event["phaseId"] = "P"
        event["contractHash"] = "a" * 64
        board["cards"]["stale-dispatch"] = card
        path = repo.state_dir / "board.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(board), encoding="utf-8")
        result = pi_board.dispatch_task(path, "stale-dispatch", cli_runner=lambda argv, timeout: {
            "status": "queued", "exitCode": 0, "timedOut": False, "outputSha256": "0" * 64,
            "outputExcerpt": "", "argv0": argv[0]})
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["reason"], "no dispatchable events")

    def test_previous_phase_must_be_accepted_before_the_next_phase(self):
        repo, worktree, env = self.ready_phase(task="cross-task", phase_id="P-CROSS1")
        design_sha = hashlib.sha256((repo.root / "docs" / "design.md").read_bytes()).hexdigest()
        second = self.contract(repo, "P-CROSS2", design_sha=design_sha)
        second_path = self.write_contract("p2.json", second)
        proc = run_cli("continue", "--repo", str(repo.root), "--task", "cross-task",
                       "--prompt", "phase two", "--contract-file", str(second_path),
                       env=env, expect=2)
        self.assertIn("cannot dispatch the next phase", proc.stderr)
        self.assertEqual(self.rounds(repo, "cross-task"), [1])
        # The exact phase/contract/candidate acceptance opens the gate.
        event = self.pending(repo, "cross-task", "review_required")[0]
        head = self.head(worktree)
        frozen = json.loads((repo.task_dir("cross-task") / "phase.json").read_text(encoding="utf-8"))
        board_json("decide", "--repo", str(repo.root), "--task", "cross-task",
                   "--event-id", event["id"], "--decision", "accept", "--reviewed-head", head,
                   "--phase", "P-CROSS1", "--contract-hash", frozen["contractSha256"], env=env)
        proc = run_cli("continue", "--repo", str(repo.root), "--task", "cross-task",
                       "--prompt", "phase two", "--contract-file", str(second_path),
                       env=env, expect=0)
        self.assertEqual(json.loads(proc.stdout)["phase"]["phaseId"], "P-CROSS2")
        frozen2 = json.loads((repo.task_dir("cross-task") / "phase.json").read_text(encoding="utf-8"))
        self.assertEqual(frozen2["contract"]["phaseId"], "P-CROSS2")
        state = self.phase_state(repo, "cross-task")
        self.assertEqual(state["phaseId"], "P-CROSS2")

    def test_same_phase_revision_keeps_budget_anchor_and_quota(self):
        repo, worktree, env = self.ready_phase(task="revision-task", phase_id="P-REV")
        original = self.phase_state(repo, "revision-task")
        event = self.pending(repo, "revision-task", "review_required")[0]
        head = self.head(worktree)
        frozen = json.loads((repo.task_dir("revision-task") / "phase.json").read_text(encoding="utf-8"))
        board_json("decide", "--repo", str(repo.root), "--task", "revision-task",
                   "--event-id", event["id"], "--decision", "accept", "--reviewed-head", head,
                   "--phase", "P-REV", "--contract-hash", frozen["contractSha256"], env=env)
        revised = dict(frozen["contract"], budgetSeconds=1800,
                       result="revised complete result")
        revised_path = self.write_contract("revised.json", revised)
        run_cli("continue", "--repo", str(repo.root), "--task", "revision-task",
                "--prompt", "apply the revision", "--contract-file", str(revised_path),
                env=env, expect=0)
        state = self.phase_state(repo, "revision-task")
        self.assertEqual(state["phaseId"], "P-REV")
        self.assertAlmostEqual(state["startedAt"], original["startedAt"], delta=0.001)
        self.assertAlmostEqual(state["budgetSeconds"], 1800, delta=1)
        self.assertAlmostEqual(state["deadlineAt"] - state["startedAt"], 1800, delta=1)

    # ------------------------------------------------------------------
    # O4 review-episode liveness, resource limits and Python counts
    # ------------------------------------------------------------------
    def test_review_event_renews_after_invalidation_with_a_fresh_receipt(self):
        fixture = self.ready_accept_fixture("renew-receipt", "P-RENEW1")
        repo, env = fixture["repo"], fixture["env"]
        task, candidate = "renew-receipt", fixture["candidate"]
        checks = repo.task_dir(task) / "rounds" / "1" / "round.checks"
        first = fixture["review"]

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task", task,
                            "--round", "1", env=env)

        # Invalidate: a newer receipt with a different argv fails the item, so
        # the pending review event is superseded and the ready episode closes.
        self.synth_receipt(checks, "A1", 0, candidate, argv=["python3", "-c", "other"])
        self.assertEqual(readiness()["items"][0]["status"], "failed")
        self.refresh(repo, task, env)
        self.assertEqual(self.pending(repo, task, "review_required"), [])
        self.assertTrue(self.pending(repo, task, "phase_blocked"))

        # A fresh valid receipt on the SAME round/candidate renews one episode.
        self.synth_receipt(checks, "A1", 0, candidate, command=fixture["command"])
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, task, env)
        renewed = self.pending(repo, task, "review_required")
        self.assertEqual(len(renewed), 1)
        self.assertNotEqual(renewed[0]["id"], first["id"])
        self.assertEqual(self.card(repo, task)["handled"][first["id"]]["decision"],
                         "superseded")

        # Unchanged repeated refreshes stay idempotent.
        revision = json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))["revision"]
        self.refresh(repo, task, env)
        self.assertEqual(len(self.pending(repo, task, "review_required")), 1)
        self.assertEqual(json.loads((repo.state_dir / "board.json").read_text(
            encoding="utf-8"))["revision"], revision)

        # The superseded event can never bind an accept; the renewed one can.
        run_board("decide", "--repo", str(repo.root), "--task", task,
                  "--event-id", first["id"], "--decision", "accept",
                  "--reviewed-head", candidate, "--phase", "P-RENEW1",
                  "--contract-hash", fixture["contractSha256"], env=env, expect=2)
        decided = board_json("decide", "--repo", str(repo.root), "--task", task,
                             "--event-id", renewed[0]["id"], "--decision", "accept",
                             "--reviewed-head", candidate, "--phase", "P-RENEW1",
                             "--contract-hash", fixture["contractSha256"], env=env)
        self.assertEqual(decided["decision"], "accepted")

    def test_review_event_renews_after_transient_unknown_log_recovers(self):
        fixture = self.ready_accept_fixture("renew-log", "P-RENEW2")
        repo, env = fixture["repo"], fixture["env"]
        task, candidate = "renew-log", fixture["candidate"]
        checks = repo.task_dir(task) / "rounds" / "1" / "round.checks"
        first = fixture["review"]
        receipts = sorted(checks.glob("A1-*.json"),
                          key=lambda path: path.stat().st_mtime, reverse=True)
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        log = checks / receipt["log"]
        original = log.read_bytes()

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task", task,
                            "--round", "1", env=env)

        # The receipt bytes are unchanged; only log readability is transient.
        log.unlink()
        self.assertEqual(readiness()["items"][0]["status"], "unknown")
        self.refresh(repo, task, env)
        self.assertEqual(self.pending(repo, task, "review_required"), [])
        self.assertTrue(self.pending(repo, task, "phase_blocked"))

        log.write_bytes(original)  # exact same bytes -> same verified hash
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, task, env)
        renewed = self.pending(repo, task, "review_required")
        self.assertEqual(len(renewed), 1)
        self.assertNotEqual(renewed[0]["id"], first["id"])
        decided = board_json("decide", "--repo", str(repo.root), "--task", task,
                             "--event-id", renewed[0]["id"], "--decision", "accept",
                             "--reviewed-head", candidate, "--phase", "P-RENEW2",
                             "--contract-hash", fixture["contractSha256"], env=env)
        self.assertEqual(decided["decision"], "accepted")

    def test_phase_contract_rejects_escaping_resource_limit(self):
        repo, worktree = self.make(name="bad-res")
        env = self.h_env(PI_DOUBLE_MODE="ok")
        sha = self.write_design(repo)
        outside = self.tmp / "outside-res"
        outside.mkdir(exist_ok=True)
        (worktree / "link").symlink_to(outside)
        for label, declared in (("traversal", "../outside-res"),
                                ("symlink-parent", "link/inside")):
            contract = self.contract(
                repo, f"P-BADRES-{label}", design_sha=sha,
                extra={"resourceLimits": [{"path": declared, "maxBytes": 10}]})
            path = self.write_contract(f"badres-{label}.json", contract)
            proc = self.start(repo, worktree, f"bad-res-{label}", path, env, expect=2)
            self.assertIn("resourceLimits", proc.stderr)
            self.assertFalse(repo.task_dir(f"bad-res-{label}").exists())

    def test_declared_phase_resource_breach_stops_pi_and_blocks(self):
        repo, worktree = self.make(name="res-breach")
        sha = self.write_design(repo)
        baseline = pi_task.measure(worktree)
        self.assertTrue(baseline["complete"])
        cap = int(baseline["bytes"]) + 8
        env = self.h_env(PI_DOUBLE_MODE="hang",
                         CODEX_PI_RESOURCE_SCAN_SECONDS="0.1",
                         CODEX_PI_RESOURCE_UNKNOWN_SECONDS="5")
        contract = self.contract(
            repo, "P-RESBREACH", design_sha=sha,
            extra={"resourceLimits": [{"path": ".", "maxBytes": cap}]})
        path = self.write_contract("res-breach.json", contract)
        self.start(repo, worktree, "res-breach", path, env)
        repo.wait_round_state("res-breach", "running")
        self.register(repo, "res-breach", env)
        (worktree / "bloat.bin").write_bytes(b"x" * (cap + 64))
        result = repo.wait_terminal("res-breach", env=env, timeout=30)
        self.assertEqual(result["state"], "failed")
        state = json.loads((repo.task_dir("res-breach") / "rounds" / "1" / "round.state.json")
                           .read_text(encoding="utf-8"))
        self.assertTrue(state.get("resourceBreached"))
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "res-breach",
                             "--round", "1", env=env)
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(readiness["resource"]["status"], "breached")
        self.assertIn("resource", readiness["readinessReason"])
        self.refresh(repo, "res-breach", env)
        blocked = self.pending(repo, "res-breach", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "resource_breached")
        limit = blocked[0]["evidence"]["resource"]["limits"][0]
        self.assertGreater(limit["observedBytes"], cap)

    def test_declared_phase_resource_unknown_escalates_and_blocks(self):
        repo, worktree = self.make(name="res-unknown")
        guarded = worktree / "guarded"
        guarded.mkdir()
        sha = self.write_design(repo)
        env = self.h_env(PI_DOUBLE_MODE="hang",
                         CODEX_PI_RESOURCE_SCAN_SECONDS="0.05",
                         CODEX_PI_RESOURCE_UNKNOWN_SECONDS="0.2")
        contract = self.contract(
            repo, "P-RESUNKNOWN", design_sha=sha,
            extra={"resourceLimits": [{"path": "guarded", "maxBytes": 10 ** 7}]})
        path = self.write_contract("res-unknown.json", contract)
        result = None
        try:
            self.start(repo, worktree, "res-unknown", path, env)
            repo.wait_round_state("res-unknown", "running")
            self.register(repo, "res-unknown", env)
            os.chmod(guarded, 0)
            result = repo.wait_terminal("res-unknown", env=env, timeout=30)
        finally:
            os.chmod(guarded, 0o755)
        self.assertEqual(result["state"], "failed")
        state = json.loads((repo.task_dir("res-unknown") / "rounds" / "1" / "round.state.json")
                           .read_text(encoding="utf-8"))
        self.assertTrue(state.get("resourceUnknown"))
        readiness = cli_json("readiness", "--repo", str(repo.root), "--task", "res-unknown",
                             "--round", "1", env=env)
        self.assertEqual(readiness["status"], "not_ready")
        self.assertEqual(readiness["resource"]["status"], "escalated")
        self.refresh(repo, "res-unknown", env)
        blocked = self.pending(repo, "res-unknown", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "resource_unknown")

    def test_python_count_rules_follow_board_readiness_and_accept(self):
        repo, worktree = self.make(name="counts-gate")
        sha = self.write_design(repo)
        (worktree / "sample_gate.py").write_text(
            "import os\nimport unittest\n\nclass Sample(unittest.TestCase):\n"
            "    def test_one(self):\n"
            "        if os.environ.get('GATE_MODE') == 'skip':\n"
            "            self.skipTest('gate skip')\n"
            "        self.assertTrue(True)\n"
            "    def test_two(self):\n"
            "        if os.environ.get('GATE_MODE') == 'fail':\n"
            "            self.assertEqual(1, 2)\n"
            "        self.assertEqual(2, 2)\n",
            encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(worktree), "-c", "user.email=t@e.invalid",
                        "-c", "user.name=T", "commit", "-qm", "sample tests"], check=True,
                       capture_output=True)
        command = self.python_command("-m", "unittest", "-v", "sample_gate")
        item = {"id": "A1", "description": "python tests", "command": command,
                "passCondition": "exit 0, run>=2, no skips", "evidence": "receipt",
                "minRun": 2, "forbidSkip": True}
        path = self.write_contract("counts-gate.json",
                                   self.contract(repo, "P-PYCOUNT", design_sha=sha, items=[item]))
        # Importing the sample must not create an untracked __pycache__ that
        # makes this otherwise-clean candidate receipt dirty.
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="6",
                         PYTHONDONTWRITEBYTECODE="1")
        self.start(repo, worktree, "counts-gate", path, env)
        repo.wait_round_state("counts-gate", "running")
        checks = repo.task_dir("counts-gate") / "rounds" / "1" / "round.checks"

        def real_receipt(mode=None):
            run_env = dict(env)
            if mode:
                run_env["GATE_MODE"] = mode
            run = subprocess.run(
                [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(checks),
                 "--id", "A1", "--timeout-seconds", "900", "--", *shlex.split(command)],
                cwd=str(worktree), capture_output=True, text=True, env=run_env, timeout=120)
            receipt = json.loads(Path(json.loads(run.stdout)["receipt"]).read_text(encoding="utf-8"))
            return run, receipt

        run, real = real_receipt()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(real["test_counts"], {"run": 2, "pass": 2, "fail": 0, "skip": 0,
                                                "format": "python_unittest_summary"})
        self.register(repo, "counts-gate", env)
        repo.wait_terminal("counts-gate", env=env)

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task", "counts-gate",
                            "--round", "1", env=env)

        def contract_sha():
            return json.loads((repo.task_dir("counts-gate") / "phase.json").read_text(
                encoding="utf-8"))["contractSha256"]

        self.assertEqual(readiness()["items"][0]["status"], "covered")
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, "counts-gate", env)
        review = self.pending(repo, "counts-gate", "review_required")
        self.assertEqual(len(review), 1)

        # A real skipped unittest run is refused by forbidSkip even when minRun passes
        # (unittest itself exits 0 when tests are only skipped).
        run, skipped = real_receipt("skip")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(skipped["test_counts"]["skip"], 1)
        self.assertEqual(readiness()["items"][0]["status"], "skipped")
        proc = run_board("decide", "--repo", str(repo.root), "--task", "counts-gate",
                         "--event-id", review[0]["id"], "--decision", "accept",
                         "--reviewed-head", self.head(worktree), "--phase", "P-PYCOUNT",
                         "--contract-hash", contract_sha(), env=env, expect=2)
        self.assertIn("readiness", proc.stderr)
        self.refresh(repo, "counts-gate", env)
        self.assertEqual(self.pending(repo, "counts-gate", "review_required"), [])
        blocked = self.pending(repo, "counts-gate", "phase_blocked")
        self.assertEqual(blocked[0]["evidence"]["reason"], "required_check_failed")

        # A real failing run stays failed under the declared rules.
        run, failed = real_receipt("fail")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(failed["test_counts"]["fail"], 1)
        self.assertEqual(readiness()["items"][0]["status"], "failed")

        # Missing counts stay unknown under declared rules; never a pass.
        self.synth_receipt(checks, "A1", 0, self.head(worktree), command=command)
        self.assertEqual(readiness()["items"][0]["status"], "unknown")

        # A restored real receipt covers again and the renewed event is accepted.
        real_receipt()
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, "counts-gate", env)
        renewed = self.pending(repo, "counts-gate", "review_required")
        self.assertEqual(len(renewed), 1)
        decided = board_json("decide", "--repo", str(repo.root), "--task", "counts-gate",
                             "--event-id", renewed[0]["id"], "--decision", "accept",
                             "--reviewed-head", self.head(worktree), "--phase", "P-PYCOUNT",
                             "--contract-hash", contract_sha(), env=env)
        self.assertEqual(decided["decision"], "accepted")

    def test_resource_evidence_missing_corrupt_stale_and_incomplete_cannot_pass(self):
        fixture = self.ready_accept_fixture(
            "res-evidence", "P-RESEVAL",
            resource_limits=[{"path": ".", "maxBytes": 10 ** 9}])
        repo, env = fixture["repo"], fixture["env"]
        task, candidate = "res-evidence", fixture["candidate"]
        state_path = repo.task_dir(task) / "rounds" / "1" / pi_task.RESOURCE_STATE_FILE
        original = state_path.read_bytes()
        first = fixture["review"]

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task", task,
                            "--round", "1", env=env)

        self.assertEqual(readiness()["resource"]["status"], "ok")
        self.assertEqual(readiness()["status"], "ready")

        # Missing evidence is unknown and the pending review becomes stale.
        state_path.unlink()
        self.assertEqual(readiness()["resource"]["status"], "unknown")
        self.assertEqual(readiness()["status"], "not_ready")
        self.refresh(repo, task, env)
        self.assertEqual(self.pending(repo, task, "review_required"), [])
        blocked = self.pending(repo, task, "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "resource_unknown")

        # Corrupt evidence is unknown, never under budget.
        state_path.write_text("not json", encoding="utf-8")
        self.assertEqual(readiness()["resource"]["status"], "unknown")

        # Stale or contradictory evidence cannot satisfy the current contract.
        state = json.loads(original)
        state["round"] = 99
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertIn("another round", readiness()["resource"]["reason"])
        state["round"] = 1
        state["limitsSignature"] = "0" * 64
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertIn("declared limits", readiness()["resource"]["reason"])

        # No completed final scan is unknown.
        state = json.loads(original)
        state["finalScannedAt"] = None
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertIn("completed final observation", readiness()["resource"]["reason"])

        # A contradictory persisted summary cannot force ok.
        state = json.loads(original)
        state["status"] = "ok"
        state["limits"][0]["breached"] = True
        state["limits"][0]["observedBytes"] = 10 ** 12
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(readiness()["status"], "not_ready")
        self.assertEqual(readiness()["resource"]["status"], "unknown")

        # Restoring the exact valid evidence opens a fresh review episode.
        state_path.write_bytes(original)
        self.assertEqual(readiness()["resource"]["status"], "ok")
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, task, env)
        renewed = self.pending(repo, task, "review_required")
        self.assertEqual(len(renewed), 1)
        self.assertNotEqual(renewed[0]["id"], first["id"])

        # A complete observation without a measured byte count cannot cover,
        # and the live accept gate refuses the pending event.
        state = json.loads(original)
        state["limits"][0]["observedBytes"] = None
        state["status"] = "unknown"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(readiness()["resource"]["status"], "unknown")
        self.assertIn("byte count", readiness()["resource"]["reason"])
        run_board("decide", "--repo", str(repo.root), "--task", task,
                  "--event-id", renewed[0]["id"], "--decision", "accept",
                  "--reviewed-head", candidate, "--phase", "P-RESEVAL",
                  "--contract-hash", fixture["contractSha256"], env=env, expect=2)

        # A completed observation without scan evidence is unknown as well.
        state = json.loads(original)
        state["limits"][0]["scans"] = 0
        state["status"] = "unknown"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(readiness()["resource"]["status"], "unknown")
        self.assertIn("scan evidence", readiness()["resource"]["reason"])

        # A consistent persisted overage blocks readiness, the board and accept.
        state = json.loads(original)
        state["status"] = "breached"
        state["limits"][0]["breached"] = True
        state["limits"][0]["observedBytes"] = 10 ** 12
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(readiness()["resource"]["status"], "breached")
        self.assertEqual(readiness()["status"], "not_ready")
        run_board("decide", "--repo", str(repo.root), "--task", task,
                  "--event-id", renewed[0]["id"], "--decision", "accept",
                  "--reviewed-head", candidate, "--phase", "P-RESEVAL",
                  "--contract-hash", fixture["contractSha256"], env=env, expect=2)
        self.refresh(repo, task, env)
        self.assertTrue(any(event["evidence"].get("reason") == "resource_breached"
                            for event in self.pending(repo, task, "phase_blocked")))

        # Exact restore recovers one more episode; old events cannot accept.
        state_path.write_bytes(original)
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, task, env)
        accepted = self.pending(repo, task, "review_required")
        self.assertEqual(len(accepted), 1)
        self.assertNotEqual(accepted[0]["id"], renewed[0]["id"])
        run_board("decide", "--repo", str(repo.root), "--task", task,
                  "--event-id", first["id"], "--decision", "accept",
                  "--reviewed-head", candidate, "--phase", "P-RESEVAL",
                  "--contract-hash", fixture["contractSha256"], env=env, expect=2)
        decided = board_json("decide", "--repo", str(repo.root), "--task", task,
                             "--event-id", accepted[0]["id"], "--decision", "accept",
                             "--reviewed-head", candidate, "--phase", "P-RESEVAL",
                             "--contract-hash", fixture["contractSha256"], env=env)
        self.assertEqual(decided["decision"], "accepted")

    def test_resource_scan_budget_bounds_many_limits_and_escalates(self):
        tmp = self.tmp / "budget-monitor"
        tmp.mkdir()
        worktree = tmp / "wt"
        worktree.mkdir()
        for index in range(40):
            (worktree / f"d{index}").mkdir()
        limits = [{"path": f"d{index}", "maxBytes": 1000} for index in range(40)]

        def slow_ok(path, max_entries=None, max_seconds=None):
            time.sleep(0.05)
            return {"bytes": 0, "complete": True, "unknown": False, "exists": True,
                    "reason": None}

        with mock.patch.object(pi_task, "measure", side_effect=slow_ok) as fake:
            monitor = pi_task.PhaseResourceMonitor(
                tmp, worktree, limits, "P", "a" * 64, 1,
                unknown_seconds=999, scan_budget_seconds=0.2)
            started = time.monotonic()
            self.assertIsNone(monitor.scan())
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertLess(fake.call_count, 40)
        unvisited = [entry for entry in monitor.state()["limits"] if entry.get("unknown")]
        self.assertGreaterEqual(len(unvisited), 30)
        self.assertEqual(monitor.snapshot()["status"], "unknown")

        # A known breach returns promptly after the first owned measurement.
        def breach(path, max_entries=None, max_seconds=None):
            time.sleep(0.05)
            return {"bytes": 10 ** 9, "complete": True, "unknown": False, "exists": True,
                    "reason": None}

        with mock.patch.object(pi_task, "measure", side_effect=breach) as fake:
            monitor2 = pi_task.PhaseResourceMonitor(
                tmp, worktree, limits, "P", "a" * 64, 1,
                unknown_seconds=999, scan_budget_seconds=10)
            started = time.monotonic()
            stop = monitor2.scan()
            elapsed = time.monotonic() - started
        self.assertTrue(stop)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(monitor2.snapshot()["status"], "breached")

        # Sustained unknown escalates under the same bounded scan.
        def unreadable(path, max_entries=None, max_seconds=None):
            return {"bytes": 0, "complete": False, "unknown": True, "exists": True,
                    "reason": "unreadable"}

        with mock.patch.object(pi_task, "measure", side_effect=unreadable):
            monitor3 = pi_task.PhaseResourceMonitor(
                tmp, worktree, limits[:2], "P", "a" * 64, 1,
                unknown_seconds=0.05, scan_budget_seconds=5)
            self.assertIsNone(monitor3.scan())
            time.sleep(0.06)
            stop = monitor3.scan()
        self.assertTrue(stop)
        self.assertEqual(monitor3.snapshot()["status"], "escalated")

    def test_resource_missing_declared_path_is_known_zero(self):
        round_dir = self.tmp / "missing-path-monitor"
        round_dir.mkdir()
        worktree = round_dir / "wt"
        worktree.mkdir()
        limits = [{"path": "not-created-yet", "maxBytes": 100}]
        monitor = pi_task.PhaseResourceMonitor(round_dir, worktree, limits, "P-MISS",
                                               "a" * 64, 1, scan_budget_seconds=5)
        monitor.final_scan()
        verdict = pi_task.evaluate_resource_evidence(
            round_dir, {"phaseId": "P-MISS", "resourceLimits": limits}, 1, "a" * 64)
        self.assertEqual(verdict["status"], "ok")
        self.assertEqual(verdict["limits"][0]["observedBytes"], 0)
        self.assertTrue(verdict["limits"][0]["complete"])

    def test_tampered_contract_digest_cannot_pass_and_restores(self):
        fixture = self.ready_accept_fixture("tamper", "P-TAMPER")
        repo, env = fixture["repo"], fixture["env"]
        task, candidate = "tamper", fixture["candidate"]
        phase_file = repo.task_dir(task) / "phase.json"
        original = phase_file.read_bytes()
        first = fixture["review"]

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task", task,
                            "--round", "1", env=env)

        self.assertEqual(readiness()["status"], "ready")
        # Weaken only the frozen contract content; the stored digest stays.
        tampered = json.loads(original)
        tampered["contract"]["acceptanceItems"] = []
        phase_file.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertNotEqual(readiness()["status"], "ready")
        phase_status = cli_json("phase-status", "--repo", str(repo.root), "--task", task,
                                env=env)
        self.assertEqual(phase_status["phaseProblem"], "digest-mismatch")
        # The board keeps a phase binding with unknown readiness instead of
        # falling back to a legacy (unbound) review event.
        self.refresh(repo, task, env)
        card = self.card(repo, task)
        self.assertEqual(card["phase"]["phaseId"], "P-TAMPER")
        self.assertEqual(card["phase"]["readiness"]["status"], "unknown")
        self.assertEqual(self.pending(repo, task, "review_required"), [])
        self.assertTrue(self.pending(repo, task, "phase_blocked"))
        run_board("decide", "--repo", str(repo.root), "--task", task,
                  "--event-id", first["id"], "--decision", "accept",
                  "--reviewed-head", candidate, "--phase", "P-TAMPER",
                  "--contract-hash", fixture["contractSha256"], env=env, expect=2)
        # Exact restore recovers exactly one fresh review episode.
        phase_file.write_bytes(original)
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, task, env)
        renewed = self.pending(repo, task, "review_required")
        self.assertEqual(len(renewed), 1)
        self.assertNotEqual(renewed[0]["id"], first["id"])
        decided = board_json("decide", "--repo", str(repo.root), "--task", task,
                             "--event-id", renewed[0]["id"], "--decision", "accept",
                             "--reviewed-head", candidate, "--phase", "P-TAMPER",
                             "--contract-hash", fixture["contractSha256"], env=env)
        self.assertEqual(decided["decision"], "accepted")

    def test_contradictory_count_summary_cannot_pass_real_receipt_gate(self):
        repo, worktree = self.make(name="count-contradiction")
        sha = self.write_design(repo)
        script = ("import os; print('Ran 2 tests in 0.010s'); print(); "
                  "print(os.environ.get('COUNT_MODE', 'OK'))")
        command = self.python_command("-c", script)
        item = {"id": "A1", "description": "counted tests", "command": command,
                "passCondition": "exit 0, run>=2, no skips", "evidence": "receipt",
                "minRun": 2, "forbidSkip": True}
        path = self.write_contract("count-contradiction.json",
                                   self.contract(repo, "P-CONTRA", design_sha=sha, items=[item]))
        env = self.h_env(PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="5")
        self.start(repo, worktree, "count-contradiction", path, env)
        repo.wait_round_state("count-contradiction", "running")
        checks = repo.task_dir("count-contradiction") / "rounds" / "1" / "round.checks"

        def receipt(mode=None):
            run_env = dict(env)
            if mode:
                run_env["COUNT_MODE"] = mode
            run = subprocess.run(
                [sys.executable, str(RUNTIME / "pi_check.py"), "--output-dir", str(checks),
                 "--id", "A1", "--timeout-seconds", "900", "--", *shlex.split(command)],
                cwd=str(worktree), capture_output=True, text=True, env=run_env, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(Path(json.loads(run.stdout)["receipt"]).read_text(encoding="utf-8"))

        self.assertEqual(receipt()["test_counts"]["run"], 2)
        self.register(repo, "count-contradiction", env)
        repo.wait_terminal("count-contradiction", env=env)

        def readiness():
            return cli_json("readiness", "--repo", str(repo.root), "--task",
                            "count-contradiction", "--round", "1", env=env)

        def contract_sha():
            return json.loads((repo.task_dir("count-contradiction") / "phase.json").read_text(
                encoding="utf-8"))["contractSha256"]

        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, "count-contradiction", env)
        review = self.pending(repo, "count-contradiction", "review_required")
        self.assertEqual(len(review), 1)

        # A contradictory zero-exit log cannot satisfy the declared count rules.
        for mode in ("FAILED", "OK (skipped=1, skipped=0)"):
            contradictory = receipt(mode)
            self.assertIsNone(contradictory["test_counts"])
            self.assertEqual(readiness()["items"][0]["status"], "unknown")
            run_board("decide", "--repo", str(repo.root), "--task", "count-contradiction",
                      "--event-id", review[0]["id"], "--decision", "accept",
                      "--reviewed-head", self.head(worktree), "--phase", "P-CONTRA",
                      "--contract-hash", contract_sha(), env=env, expect=2)
        self.refresh(repo, "count-contradiction", env)
        self.assertEqual(self.pending(repo, "count-contradiction", "review_required"), [])
        self.assertTrue(self.pending(repo, "count-contradiction", "phase_blocked"))

        # A genuine summary recovers a fresh review episode and acceptance.
        self.assertIsNotNone(receipt()["test_counts"])
        self.assertEqual(readiness()["status"], "ready")
        self.refresh(repo, "count-contradiction", env)
        renewed = self.pending(repo, "count-contradiction", "review_required")
        self.assertEqual(len(renewed), 1)
        decided = board_json("decide", "--repo", str(repo.root), "--task", "count-contradiction",
                             "--event-id", renewed[0]["id"], "--decision", "accept",
                             "--reviewed-head", self.head(worktree), "--phase", "P-CONTRA",
                             "--contract-hash", contract_sha(), env=env)
        self.assertEqual(decided["decision"], "accepted")


if __name__ == "__main__":
    unittest.main()
