"""Pinned review-limit policy: default one, explicit two, legacy three.

Every case runs against a temporary Git repository. The direct cases publish
real board events and call the real ``pi_board.decide`` authority; the lifecycle
cases start/continue the real CLI with the offline Pi double and real board
registration. No model, network or real Codex queue is invoked.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime_helpers import (RUNTIME, Repo, base_env, cleanup_repos, default_config,
                             make_pi_trap, run_cli)

sys.path.insert(0, str(RUNTIME))
import pi_board
import pi_task
from pi_takeover import (EXPLICIT_REVIEW_LIMITS, LEGACY_FAILED_DELIVERY_LIMIT,
                         NEW_TASK_DEFAULT_LIMIT, normalize_review_limit, review_policy)


def board_cli(*args, env: dict, expect: int = 0, timeout: float = 60):
    proc = subprocess.run([sys.executable, str(RUNTIME / "pi_board.py"),
                           *[str(arg) for arg in args]],
                          capture_output=True, text=True, env=env, timeout=timeout)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(f"pi_board {' '.join(str(arg) for arg in args)} exited "
                             f"{proc.returncode}, expected {expect}\n"
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
    return json.loads(proc.stdout) if expect == 0 else proc


def pin(limit: int) -> dict:
    return {"schemaVersion": 1, "qualityFailureLimit": limit, "pinnedAt": 1,
            "pinnedBy": "start"}


class TakeoverPolicyTest(unittest.TestCase):
    """Real board decisions against a temporary Git repository."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi-takeover-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(cleanup_repos)
        self.repo = Repo(Path(self.temp.name), config=default_config())
        self.wt = self.repo.worktree("worker")
        self.head = pi_task.git(self.wt, "rev-parse", "HEAD")
        self.file = self.repo.state_dir / "board.json"
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.board = None
        self.card = None

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def new_card(self, *, review_pin=None, phase=None, contract="a" * 64):
        card = pi_board._new_card("task", None, "task", "outcome", None, None,
                                  self.repo.root, self.repo.state_dir, str(self.wt),
                                  "offline", None, 1, review_pin=review_pin)
        if phase is not None:
            card["phase"] = {"phaseId": phase, "contractHash": contract,
                             "candidate": self.head, "status": "review_ready"}
        self.board = {"schemaVersion": 1, "revision": 1, "createdAt": 1, "updatedAt": 1,
                      "cards": {"task": card}}
        self.card = card
        self.write()

    def write(self):
        self.file.write_text(json.dumps(self.board))

    def reload(self):
        self.board = json.loads(self.file.read_text())
        self.card = self.board["cards"]["task"]

    def publish(self, number, *, kind="review_required", phase=None, contract="a" * 64,
                fingerprint=None, head=None):
        head = head or self.head
        self.card["pi"] = {"round": number, "state": "completed"}
        if phase is None:
            self.card.pop("phase", None)
        else:
            self.card["phase"] = {"phaseId": phase, "contractHash": contract,
                                  "candidate": head, "status": "review_ready"}
        event = pi_board.add_event(self.card, kind, number,
                                   fingerprint or f"event-{number}-{kind}",
                                   "delivery", {"head": head}, {}, "review", number * 10)
        self.assertIsNotNone(event, "event publication must be new")
        if phase is not None:
            event.update(phaseId=phase, contractHash=contract)
        self.write()
        return event

    def decide(self, event, decision="changes_requested", **kwargs):
        out = pi_board.decide(self.repo.root, "task", event["id"], decision, **kwargs)
        self.reload()
        return out

    def takeover_events(self):
        return [event for event in self.card["events"]
                if event["kind"] == "codex_takeover_required"]

    # ------------------------------------------------------------------
    # default one / explicit two / legacy three
    # ------------------------------------------------------------------
    def test_default_one_failure_takes_over_and_stays_taken_over(self):
        self.new_card(review_pin=pin(NEW_TASK_DEFAULT_LIMIT))
        event = self.publish(1, phase="P1")
        out = self.decide(event)
        policy = out["reviewPolicy"]
        self.assertEqual(policy["limit"], 1)
        self.assertEqual(policy["limitSource"], "task-pin")
        self.assertEqual(policy["failedDeliveries"], 1)
        self.assertTrue(policy["takeoverRequired"])
        self.assertEqual(policy["implementationOwner"], "codex")
        self.assertIn("limit 1", policy["reason"])
        self.assertEqual(policy["outcome"]["phaseId"], "P1")
        self.assertIn("Codex takeover required", policy["instruction"])
        self.assertEqual(len(self.takeover_events()), 1)
        stored = self.takeover_events()[0]["evidence"]["reviewPolicy"]
        self.assertEqual(stored["limit"], 1)
        self.assertEqual(stored["failedDeliveries"], 1)
        self.assertEqual(stored["implementationOwner"], "codex")
        handled = self.card["handled"][event["id"]]
        self.assertEqual(handled["eventKind"], "review_required")
        self.assertEqual(handled["failureKind"], "quality")
        self.assertTrue(self.card["codex"]["takeover"]["required"])
        # A duplicate decision for this exact event never double-counts.
        self.decide(event)
        self.assertEqual(review_policy(self.card)["failedDeliveries"], 1)

    def test_explicit_two_counts_two_distinct_failed_rounds(self):
        self.new_card(review_pin=pin(2))
        first = self.decide(self.publish(1, phase="P1"))
        self.assertEqual(first["reviewPolicy"]["limit"], 2)
        self.assertEqual(first["reviewPolicy"]["failedDeliveries"], 1)
        self.assertFalse(first["reviewPolicy"]["takeoverRequired"])
        self.assertEqual(first["reviewPolicy"]["implementationOwner"], "pi")
        self.assertIsNone(first["reviewPolicy"]["instruction"])
        second = self.decide(self.publish(2, phase="P1", contract="b" * 64))
        self.assertEqual(second["reviewPolicy"]["failedDeliveries"], 2)
        self.assertTrue(second["reviewPolicy"]["takeoverRequired"])
        self.assertEqual(len(self.takeover_events()), 1)

    def test_legacy_card_without_a_pin_keeps_three(self):
        self.new_card(review_pin=None)
        self.assertEqual(review_policy(self.card)["limit"], LEGACY_FAILED_DELIVERY_LIMIT)
        self.assertEqual(review_policy(self.card)["limitSource"], "legacy-default")
        for number in (1, 2):
            out = self.decide(self.publish(number, phase="P1"))
            self.assertEqual(out["reviewPolicy"]["failedDeliveries"], number)
            self.assertFalse(out["reviewPolicy"]["takeoverRequired"])
        out = self.decide(self.publish(3, phase="P1"))
        self.assertEqual(out["reviewPolicy"]["failedDeliveries"], 3)
        self.assertTrue(out["reviewPolicy"]["takeoverRequired"])
        self.assertEqual(out["reviewPolicy"]["limit"], 3)

    def test_dispatch_time_limit_validation_accepts_only_one_or_two(self):
        self.assertEqual(normalize_review_limit(NEW_TASK_DEFAULT_LIMIT), 1)
        self.assertEqual(normalize_review_limit(2), 2)
        for value in (0, 3, 4, -1, True, "2", None):
            self.assertIsNone(normalize_review_limit(value))
        self.assertEqual(tuple(EXPLICIT_REVIEW_LIMITS), (1, 2))

    # ------------------------------------------------------------------
    # counting resistance
    # ------------------------------------------------------------------
    def test_duplicate_events_for_one_round_count_once(self):
        self.new_card(review_pin=pin(2))
        self.decide(self.publish(1, phase="P1", fingerprint="first"))
        self.decide(self.publish(1, phase="P1", kind="phase_blocked", fingerprint="second"))
        self.assertEqual(review_policy(self.card)["failedDeliveries"], 1)
        out = self.decide(self.publish(2, phase="P1", fingerprint="third"))
        self.assertEqual(out["reviewPolicy"]["failedDeliveries"], 2)
        self.assertTrue(out["reviewPolicy"]["takeoverRequired"])

    def test_contract_revision_does_not_reset_counted_failures(self):
        self.new_card(review_pin=pin(2), phase="P1", contract="a" * 64)
        self.decide(self.publish(1, phase="P1", contract="a" * 64))
        self.decide(self.publish(2, phase="P1", contract="b" * 64))
        policy = review_policy(self.card)
        self.assertEqual(policy["limit"], 2)
        self.assertEqual(policy["failedDeliveries"], 2)
        self.assertTrue(policy["takeoverRequired"])
        self.assertEqual([report["contractHash"] for report in policy["failedReports"]],
                         ["a" * 64, "b" * 64])

    def test_phase_rename_cannot_reset_or_clear_a_takeover(self):
        self.new_card(review_pin=pin(1), phase="P1")
        self.decide(self.publish(1, phase="P1"))
        self.assertTrue(review_policy(self.card)["takeoverRequired"])
        # Rename the failed outcome without any acceptance.
        self.card["phase"] = {"phaseId": "P2", "contractHash": "c" * 64,
                              "candidate": self.head, "status": "review_ready"}
        self.write()
        self.reload()
        policy = review_policy(self.card)
        self.assertEqual(policy["limit"], 1)
        self.assertTrue(policy["takeoverRequired"])
        # Even when visible history is pruned, the persisted latch refuses Pi.
        self.card["handled"] = {}
        self.card["events"] = []
        self.write()
        self.reload()
        policy = review_policy(self.card)
        self.assertTrue(policy["takeoverRequired"])
        self.assertEqual(policy["limit"], 1)
        self.assertEqual(policy["failedDeliveries"], 1)
        self.assertEqual(policy["implementationOwner"], "codex")

    def test_accepted_outcome_resets_count_before_takeover(self):
        self.new_card(review_pin=pin(2))
        self.decide(self.publish(1))
        self.assertFalse(review_policy(self.card)["takeoverRequired"])
        accepted = self.decide(self.publish(2), decision="accept", reviewed_head=self.head)
        self.assertEqual(accepted["decision"], "accepted")
        policy = accepted["reviewPolicy"]
        self.assertEqual(policy["failedDeliveries"], 0)
        self.assertFalse(policy["takeoverRequired"])
        self.assertEqual(policy["implementationOwner"], "pi")
        # A fresh accepted outcome starts its own pinned count.
        out = self.decide(self.publish(3))
        self.assertEqual(out["reviewPolicy"]["failedDeliveries"], 1)
        self.assertFalse(out["reviewPolicy"]["takeoverRequired"])

    def test_reached_latch_survives_a_later_injected_acceptance(self):
        self.new_card(review_pin=pin(1))
        self.decide(self.publish(1))
        self.assertTrue(review_policy(self.card)["takeoverRequired"])
        # A real Pi continuation is refused here. Even an injected later
        # acceptance must not silently return this task's work to Pi.
        accepted = self.decide(self.publish(2), decision="accept", reviewed_head=self.head)
        self.assertEqual(accepted["decision"], "accepted")
        self.assertTrue(accepted["reviewPolicy"]["takeoverRequired"])
        self.assertTrue(self.card["codex"]["takeover"]["required"])

    def test_invalid_task_pin_never_relaxes_to_legacy_three(self):
        self.new_card(review_pin=pin(1))
        self.card["reviewPolicyPin"] = {"qualityFailureLimit": "invalid"}
        self.write()
        self.reload()
        policy = review_policy(self.card)
        self.assertEqual(policy["limit"], 1)
        self.assertEqual(policy["limitSource"], "invalid-task-pin-fail-closed")
        out = self.decide(self.publish(1))
        self.assertTrue(out["reviewPolicy"]["takeoverRequired"])

    def test_external_blocker_never_counts_and_classification_is_immutable(self):
        self.new_card(review_pin=pin(1))
        event = self.publish(1, kind="phase_blocked")
        with self.assertRaisesRegex(ValueError, "external blockers require"):
            pi_board.decide(self.repo.root, "task", event["id"], "changes_requested",
                            failure_kind="external")
        out = self.decide(event, failure_kind="external",
                          note="needs the user's dashboard export; unlock when provided")
        self.assertEqual(out["reviewPolicy"]["failedDeliveries"], 0)
        self.assertFalse(out["reviewPolicy"]["takeoverRequired"])
        with self.assertRaisesRegex(ValueError, "classification is immutable"):
            self.decide(event, failure_kind="quality")

    def test_pause_and_resume_never_clear_the_count_or_latch(self):
        self.new_card(review_pin=pin(1))
        self.decide(self.publish(1))
        pi_board.set_paused(self.repo.root, "task", True, note="user paused")
        self.reload()
        self.assertTrue(review_policy(self.card)["takeoverRequired"])
        pi_board.set_paused(self.repo.root, "task", False)
        self.reload()
        policy = review_policy(self.card)
        self.assertTrue(policy["takeoverRequired"])
        self.assertEqual(policy["failedDeliveries"], 1)
        self.assertEqual(len(self.takeover_events()), 1)

    def test_non_delivery_decisions_never_count(self):
        self.new_card(review_pin=pin(1))
        self.card["handled"] = {
            str(number): {"decision": "changes_requested", "phaseId": "P1",
                          "eventKind": "ownership_unknown", "round": number, "at": number}
            for number in range(1, 6)}
        self.assertEqual(review_policy(self.card)["failedDeliveries"], 0)
        self.assertFalse(review_policy(self.card)["takeoverRequired"])


class TakeoverLifecycleTest(unittest.TestCase):
    """Real start/continue/register/decide cycles with the offline Pi double."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi-takeover-life-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(cleanup_repos)
        self.repo = Repo(Path(self.temp.name), config=default_config())
        self.wt = self.repo.worktree("worker")
        self.env = base_env(CODEX_PI_HANDOFF_ROOT=str(Path(self.temp.name) / "handoff"))

    def start(self, task, *extra, expect=0):
        return run_cli("start", "--repo", self.repo.root, "--task", task,
                       "--worktree", self.wt, "--prompt", "Implement the outcome.", *extra,
                       env=self.env, expect=expect)

    def register(self, task, *extra):
        return board_cli("register", "--repo", self.repo.root, "--task", task,
                         "--transport", "offline", *extra, env=self.env)

    def refresh(self, task):
        return board_cli("refresh", "--repo", self.repo.root, "--task", task, env=self.env)

    def decide(self, task, event_id, decision, *extra):
        return board_cli("decide", "--repo", self.repo.root, "--task", task,
                         "--event-id", event_id, "--decision", decision, *extra, env=self.env)

    def review_event(self, task, number=None, kind="review_required"):
        card = json.loads((self.repo.state_dir / "board.json").read_text())["cards"][task]
        events = [event for event in card["events"] if event["kind"] == kind
                  and (number is None or event["round"] == number)]
        self.assertTrue(events, f"no {kind} event for round {number} in {task}")
        return events[-1]

    def card_view(self, task):
        shown = board_cli("show", "--repo", self.repo.root, "--task", task, env=self.env)
        self.assertEqual(shown["count"], 1)
        return shown["cards"][0]

    def test_new_task_pins_one_and_refuses_pi_after_one_quality_failure(self):
        started = json.loads(self.start("one-failure").stdout)
        self.assertEqual(started["reviewPolicy"]["qualityFailureLimit"], 1)
        frozen = json.loads((self.repo.task_dir("one-failure") / "task.json").read_text())
        self.assertEqual(frozen["reviewPolicy"]["qualityFailureLimit"], 1)
        self.assertEqual(self.repo.wait_terminal("one-failure")["state"], "completed")
        registered = self.register("one-failure")
        self.assertEqual(registered["mode"], "offline")
        self.refresh("one-failure")
        raw = json.loads((self.repo.state_dir / "board.json").read_text())["cards"]["one-failure"]
        self.assertEqual(raw["reviewPolicyPin"]["qualityFailureLimit"], 1)
        card = self.card_view("one-failure")
        self.assertEqual(card["reviewPolicy"]["limit"], 1)
        self.assertEqual(card["reviewPolicy"]["failedDeliveries"], 0)
        event = self.review_event("one-failure")
        review_packet = board_cli("packet", "--repo", self.repo.root, "--task", "one-failure",
                                  env=self.env)
        self.assertIn("reviewPolicy=limit:1,failed:0,owner:pi", review_packet["packet"])
        self.assertIn("0 of 1 pinned reviewed quality failure allowance used",
                      review_packet["packet"])
        decided = self.decide("one-failure", event["id"], "changes_requested")
        self.assertEqual(decided["reviewPolicy"]["failedDeliveries"], 1)
        self.assertTrue(decided["reviewPolicy"]["takeoverRequired"])
        self.assertEqual(decided["reviewPolicy"]["implementationOwner"], "codex")
        # The compact board view and the queued packet both show the pinned
        # limit, the counted failures, the owner and the reason.
        packet = board_cli("packet", "--repo", self.repo.root, "--task", "one-failure",
                           env=self.env)
        self.assertIn("reviewPolicy=limit:1,failed:1,owner:codex", packet["packet"])
        self.assertIn("pinned quality-failure limit 1 reached", packet["packet"])
        # Pi can no longer continue, even after an explicit resume, and no
        # round or worker may be created.
        trap, marker = make_pi_trap(Path(self.temp.name) / "traps")
        self.env["PI_BIN"] = str(trap)
        board_cli("resume", "--repo", self.repo.root, "--task", "one-failure", env=self.env)
        result = run_cli("continue", "--repo", self.repo.root, "--task", "one-failure",
                         "--prompt", "must not run", env=self.env, expect=2)
        self.assertIn("Codex takeover required", result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(sorted((self.repo.task_dir("one-failure") / "rounds").iterdir()),
                         [self.repo.task_dir("one-failure") / "rounds" / "1"])
        # The automatic continuation and the internal worker refuse the same outcome.
        frozen = json.loads((self.repo.task_dir("one-failure") / "task.json").read_text())
        auto = pi_task.evaluate_auto_continue(
            frozen, self.repo.task_dir("one-failure"), 1,
            {"contract": {"phaseId": "P1"}, "contractSha256": "a" * 64},
            {"status": "missing", "items": [{"id": "check", "status": "missing"}],
             "budget": {"remainingSeconds": 600}})
        self.assertEqual(auto["action"], "escalate")
        self.assertIn("Codex takeover required", auto["detail"])
        with self.assertRaisesRegex(ValueError, "Codex takeover required"):
            pi_task.run_worker(SimpleNamespace(task_dir=self.repo.task_dir("one-failure"),
                                               round=1, timeout_seconds=30))

    def test_explicit_two_allows_one_local_repair_then_takes_over(self):
        started = json.loads(self.start("two-failure", "--review-limit", "2").stdout)
        self.assertEqual(started["reviewPolicy"]["qualityFailureLimit"], 2)
        self.assertEqual(self.repo.wait_terminal("two-failure")["state"], "completed")
        self.register("two-failure")
        self.refresh("two-failure")
        first = self.decide("two-failure", self.review_event("two-failure", 1)["id"],
                            "changes_requested")
        self.assertEqual(first["reviewPolicy"]["failedDeliveries"], 1)
        self.assertFalse(first["reviewPolicy"]["takeoverRequired"])
        # The one remaining local repair is allowed.
        run_cli("continue", "--repo", self.repo.root, "--task", "two-failure",
                "--prompt", "repair the complete outcome", env=self.env)
        self.assertEqual(self.repo.wait_terminal("two-failure")["state"], "completed")
        self.refresh("two-failure")
        second = self.decide("two-failure", self.review_event("two-failure", 2)["id"],
                             "changes_requested")
        self.assertEqual(second["reviewPolicy"]["failedDeliveries"], 2)
        self.assertTrue(second["reviewPolicy"]["takeoverRequired"])
        refused = run_cli("continue", "--repo", self.repo.root, "--task", "two-failure",
                          "--prompt", "must not run", env=self.env, expect=2)
        self.assertIn("Codex takeover required", refused.stderr)
        self.assertEqual(sorted(entry.name for entry in
                                (self.repo.task_dir("two-failure") / "rounds").iterdir()),
                         ["1", "2"])

    def test_legacy_task_without_a_pin_registers_as_three(self):
        self.start("legacy-task")
        self.assertEqual(self.repo.wait_terminal("legacy-task")["state"], "completed")
        task_json_path = self.repo.task_dir("legacy-task") / "task.json"
        frozen = json.loads(task_json_path.read_text())
        frozen.pop("reviewPolicy", None)  # simulate a task created before the pin
        task_json_path.write_text(json.dumps(frozen))
        self.register("legacy-task")
        self.refresh("legacy-task")
        raw = json.loads((self.repo.state_dir / "board.json").read_text())["cards"]["legacy-task"]
        self.assertNotIn("reviewPolicyPin", raw)
        card = self.card_view("legacy-task")
        self.assertEqual(card["reviewPolicy"]["limit"], LEGACY_FAILED_DELIVERY_LIMIT)
        self.assertEqual(card["reviewPolicy"]["limitSource"], "legacy-default")
        decided = self.decide("legacy-task", self.review_event("legacy-task")["id"],
                              "changes_requested")
        self.assertEqual(decided["reviewPolicy"]["failedDeliveries"], 1)
        self.assertFalse(decided["reviewPolicy"]["takeoverRequired"])

    def test_registration_never_raises_a_pinned_limit(self):
        self.start("pinned-task")
        self.assertEqual(self.repo.wait_terminal("pinned-task")["state"], "completed")
        self.register("pinned-task")
        task_json_path = self.repo.task_dir("pinned-task") / "task.json"
        frozen = json.loads(task_json_path.read_text())
        frozen["reviewPolicy"]["qualityFailureLimit"] = 2  # tamper after the pin
        task_json_path.write_text(json.dumps(frozen))
        self.register("pinned-task")
        raw = json.loads((self.repo.state_dir / "board.json").read_text())["cards"]["pinned-task"]
        self.assertEqual(raw["reviewPolicyPin"]["qualityFailureLimit"], 1)
        self.assertEqual(self.card_view("pinned-task")["reviewPolicy"]["limit"], 1)

    def test_pause_then_resume_keeps_a_reached_limit(self):
        self.start("paused-takeover")
        self.assertEqual(self.repo.wait_terminal("paused-takeover")["state"], "completed")
        self.register("paused-takeover")
        self.refresh("paused-takeover")
        self.decide("paused-takeover", self.review_event("paused-takeover")["id"],
                    "changes_requested")
        board_cli("pause", "--repo", self.repo.root, "--task", "paused-takeover",
                  "--note", "user paused", env=self.env)
        self.refresh("paused-takeover")
        paused = self.card_view("paused-takeover")["reviewPolicy"]
        self.assertTrue(paused["takeoverRequired"])
        board_cli("resume", "--repo", self.repo.root, "--task", "paused-takeover", env=self.env)
        resumed = self.card_view("paused-takeover")["reviewPolicy"]
        self.assertTrue(resumed["takeoverRequired"])
        self.assertEqual(resumed["failedDeliveries"], 1)
        refused = run_cli("continue", "--repo", self.repo.root, "--task", "paused-takeover",
                          "--prompt", "must not run", env=self.env, expect=2)
        self.assertIn("Codex takeover required", refused.stderr)


class AutoContinuePolicyTest(unittest.TestCase):
    """The single pre-review missing-receipt continuation never counts as a failure."""

    PHASE = "P-MISSING"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pi-takeover-auto-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(cleanup_repos)
        self.tmp = Path(self.temp.name)
        self.repo = Repo(self.tmp, config=default_config())
        design = self.repo.root / "docs" / "design.md"
        design.parent.mkdir(parents=True, exist_ok=True)
        design.write_text("# missing receipt design\n", encoding="utf-8")
        self.repo._git("add", "-A")
        self.repo._git("-c", "user.email=test@example.invalid", "-c", "user.name=Test",
                       "commit", "-qm", "design")
        self.design_sha = hashlib.sha256(design.read_bytes()).hexdigest()
        self.wt = self.repo.worktree("worker")
        self.contract_path = self.tmp / "contract.json"
        self.contract_path.write_text(json.dumps({
            "schemaVersion": 1, "phaseId": self.PHASE, "goal": "Implement",
            "result": "Complete the missing-receipt outcome", "baseline": "HEAD",
            "scope": ["."], "designRef": "docs/design.md", "designSha256": self.design_sha,
            "acceptanceItems": [{
                "id": "A1", "description": "behavior works", "command": "python3 -c pass",
                "passCondition": "exit 0", "evidence": "receipt for A1"}],
            "budgetSeconds": 3600, "commandTimeoutSeconds": 900, "resourceLimits": [],
            "autonomousRepair": ["fix red tests"], "escalateWhen": ["design contradiction"]}),
            encoding="utf-8")
        self.env = base_env(CODEX_PI_HANDOFF_ROOT=str(self.tmp / "handoff"))

    def auto_entry(self, task):
        path = self.repo.task_dir(task) / "phase-auto.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text()).get("phases", {}).get(self.PHASE, {})

    def wait_for(self, predicate, timeout=30, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}")

    def test_missing_receipt_continuation_does_not_count_but_later_failure_does(self):
        run_cli("start", "--repo", self.repo.root, "--task", "missing-receipt",
                "--worktree", self.wt, "--prompt", "Implement the phase.",
                "--contract-file", self.contract_path, env=self.env)
        self.wait_for(lambda: self.auto_entry("missing-receipt").get("status") == "exhausted",
                      what="the single auto-continuation quota to be spent")
        self.repo.wait_terminal("missing-receipt", round=1)
        self.repo.wait_terminal("missing-receipt", round=2)
        self.assertEqual(sorted(entry.name for entry in
                                (self.repo.task_dir("missing-receipt") / "rounds").iterdir()),
                         ["1", "2"])
        # The mechanical continuation is not a reviewed quality failure.
        board_cli("register", "--repo", self.repo.root, "--task", "missing-receipt",
                  "--transport", "offline", env=self.env)
        board_cli("refresh", "--repo", self.repo.root, "--task", "missing-receipt", env=self.env)
        raw = json.loads((self.repo.state_dir / "board.json").read_text())[
            "cards"]["missing-receipt"]
        self.assertEqual(raw["reviewPolicyPin"]["qualityFailureLimit"], 1)
        shown = board_cli("show", "--repo", self.repo.root, "--task", "missing-receipt",
                          env=self.env)["cards"][0]
        self.assertEqual(shown["reviewPolicy"]["failedDeliveries"], 0)
        self.assertFalse(shown["reviewPolicy"]["takeoverRequired"])
        blocked = [event for event in raw["events"] if event["kind"] == "phase_blocked"
                   and not event["handled"]]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["evidence"]["reason"], "auto_continue_used")
        self.assertFalse(any(event["kind"] == "review_required" and not event["handled"]
                             for event in raw["events"]))
        # The main reviewer records a real quality failure for that outcome;
        # the pinned limit of one then applies exactly once.
        decided = board_cli("decide", "--repo", self.repo.root, "--task", "missing-receipt",
                            "--event-id", blocked[0]["id"], "--decision", "changes_requested",
                            env=self.env)
        self.assertEqual(decided["reviewPolicy"]["failedDeliveries"], 1)
        self.assertTrue(decided["reviewPolicy"]["takeoverRequired"])
        refused = run_cli("continue", "--repo", self.repo.root, "--task", "missing-receipt",
                          "--prompt", "must not run", env=self.env, expect=2)
        self.assertIn("Codex takeover required", refused.stderr)


if __name__ == "__main__":
    unittest.main()
