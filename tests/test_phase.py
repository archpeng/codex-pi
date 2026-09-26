"""Behavioral tests for the phase contract, structured progress, readiness,
event classification, one auto-continuation and the cross-phase gate.

Everything runs the real CLIs and real temporary git repositories. Pi is the
offline double; no model, network, real Codex queue or MCP is invoked.
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
                      counts=None) -> Path:
        checks.mkdir(parents=True, exist_ok=True)
        log = checks / f"{check_id}-{uuid.uuid4().hex[:8]}.log"
        log.write_text("synthetic evidence\n", encoding="utf-8")
        receipt = checks / f"{check_id}-{uuid.uuid4().hex[:8]}.json"
        data = {"schema_version": 1, "id": check_id, "argv": ["synthetic"], "cwd": str(checks),
                "head": head, "dirty": dirty, "tracked_diff_sha256": None,
                "started_at": time.time() - 1, "ended_at": time.time(),
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
        self.register(repo, "fail-task", env)
        self.refresh(repo, "fail-task", env)
        blocked = self.pending(repo, "fail-task", "phase_blocked")
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "round_failed")

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
            self.synth_receipt(checks, item, 0, candidate, counts=dict(counts))
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
        self.synth_receipt(checks2, "B1", 0, candidate2)  # no counts -> unknown
        self.synth_receipt(checks2, "B2", 0, candidate2,
                           counts={"run": 3, "pass": 2, "fail": 0, "skip": 1})
        self.synth_receipt(checks2, "B3", 0, candidate2,
                           counts={"run": 3, "pass": 3, "fail": 0, "skip": 0})
        # Both rules declared: minRun must still be checked after forbidSkip.
        self.synth_receipt(checks2, "B4", 0, candidate2,
                           counts={"run": 3, "pass": 3, "fail": 0, "skip": 0})
        self.synth_receipt(checks2, "B5", 0, candidate2,
                           counts={"run": 3, "pass": 1, "fail": 0, "skip": 2})
        self.synth_receipt(checks2, "B6", 0, candidate2)  # no counts -> unknown
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
        path = self.write_contract("p.json", self.contract(repo, "P-MID", design_sha=sha))
        self.start(repo, worktree, "mid-head", path, env)
        repo.wait_round_state("mid-head", "running")
        start_head = self.head(worktree)
        checks = repo.task_dir("mid-head") / "rounds" / "1" / "round.checks"
        old = subprocess.run(
            [sys.executable, str(CHECK), "--output-dir", str(checks), "--id", "A1",
             "--", sys.executable, "-c", "print('old')"], cwd=str(worktree),
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
             "--", sys.executable, "-c", "print('new')"], cwd=str(worktree),
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
        self.synth_receipt(checks, "A1", 0, candidate)  # no parseable counts
        self.synth_receipt(checks, "A2", 0, candidate)  # non-test command, no counts needed
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

    def test_stale_phase_event_cannot_be_accepted(self):
        repo, worktree, env = self.ready_phase(task="stale-task", phase_id="P-STALE")
        event = self.pending(repo, "stale-task", "review_required")[0]
        head = self.head(worktree)
        # Move the phase candidate forward without a new review event: the old
        # event must no longer bind.
        board_path = repo.state_dir / "board.json"
        board = json.loads(board_path.read_text(encoding="utf-8"))
        board["cards"]["stale-task"]["phase"]["candidate"] = "f" * 40
        board_path.write_text(json.dumps(board), encoding="utf-8")
        frozen = json.loads((repo.task_dir("stale-task") / "phase.json").read_text(encoding="utf-8"))
        proc = run_board("decide", "--repo", str(repo.root), "--task", "stale-task",
                         "--event-id", event["id"], "--decision", "accept",
                         "--reviewed-head", head, "--phase", "P-STALE",
                         "--contract-hash", frozen["contractSha256"], env=env, expect=2)
        self.assertIn("stale", proc.stderr)
        proc = run_board("decide", "--repo", str(repo.root), "--task", "stale-task",
                         "--event-id", event["id"], "--decision", "changes_requested",
                         env=env, expect=2)
        self.assertIn("stale", proc.stderr)
        # A contract revision mismatch is equally stale even when the candidate
        # is unchanged.
        board = json.loads(board_path.read_text(encoding="utf-8"))
        board["cards"]["stale-task"]["phase"]["candidate"] = head
        board["cards"]["stale-task"]["phase"]["contractHash"] = "0" * 64
        board_path.write_text(json.dumps(board), encoding="utf-8")
        proc = run_board("decide", "--repo", str(repo.root), "--task", "stale-task",
                         "--event-id", event["id"], "--decision", "accept",
                         "--reviewed-head", head, "--phase", "P-STALE",
                         "--contract-hash", frozen["contractSha256"], env=env, expect=2)
        self.assertIn("stale", proc.stderr)

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


if __name__ == "__main__":
    unittest.main()
