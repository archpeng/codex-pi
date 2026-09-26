"""Behavioral tests for the opt-in board projection and CLI queue transport.

Everything runs the real CLIs as subprocesses against real temporary git
repositories, real commit objects and controlled CLI doubles. No real Codex
queue, model or provider is invoked; the double rejects exec/resume/fork/
app-server/model-selection argv.
"""
from __future__ import annotations

import fcntl
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

from runtime_helpers import RUNTIME, Repo, base_env, cleanup_repos, default_config

sys.path.insert(0, str(RUNTIME))
import pi_board  # noqa: E402

BOARD = RUNTIME / "pi_board.py"
HOOK = RUNTIME / "pi_handoff.py"
CLI = RUNTIME / "pi_task.py"
MODEL = "deepseek/deepseek-flash"
THREAD_A = "11111111-2222-3333-4444-555555555555"
THREAD_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


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
    return json.loads(run_board(*args, env=env, expect=0, timeout=timeout).stdout)


def run_cli(*args, env: dict, expect: int | None = None, timeout: float = 90):
    proc = subprocess.run([sys.executable, str(CLI), *[str(arg) for arg in args]],
                          capture_output=True, text=True, env=env, timeout=timeout)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(f"pi_task {' '.join(str(arg) for arg in args)} exited "
                             f"{proc.returncode}, expected {expect}\n{proc.stdout}\n{proc.stderr}")
    return proc


def run_hook(payload: dict, env: dict, timeout: float = 15,
             cmd: str = "hook") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOK), cmd], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=timeout)


def run_handoff(*args, env: dict, expect: int | None = None, timeout: float = 60):
    proc = subprocess.run([sys.executable, str(HOOK), *[str(arg) for arg in args]],
                          capture_output=True, text=True, env=env, timeout=timeout)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(f"pi_handoff {' '.join(str(arg) for arg in args)} exited "
                             f"{proc.returncode}, expected {expect}\n{proc.stdout}\n{proc.stderr}")
    return proc


def make_cli_double(directory: Path, name: str = "codex-double", behavior: str = "ok",
                    seconds: float = 5.0, code: int = 2):
    """A queue-only Codex double. Forbidden capabilities exit 9."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{name}.jsonl"
    if behavior == "ok":
        tail = 'print("Queued message double for thread " + argv[4])\nraise SystemExit(0)\n'
    elif behavior == "spam":
        tail = ('import sys\nsys.stdout.write("x" * 2000000)\nsys.stdout.flush()\n'
                'raise SystemExit(0)\n')
    elif behavior == "crash":
        tail = ('print("Queued message double for thread " + argv[4], flush=True)\n'
                'import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n')
    elif behavior == "slow":
        tail = (f'import time\n'
                f"with open({str(marker) + '.pid'!r}, 'w') as handle: "
                "handle.write(str(os.getpid()))\n"
                f'time.sleep({seconds!r})\n'
                'print("Queued message double for thread " + argv[4])\nraise SystemExit(0)\n')
    else:
        tail = 'print("simulated queue failure", file=sys.stderr)\nraise SystemExit(%d)\n' % code
    script = directory / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(marker)!r}, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n"
        "argv = sys.argv[1:]\n"
        "if argv in (['--help'], ['--disable', 'daemon_auto_start', 'queue', '--help']):\n"
        "    print('codex queue help')\n"
        "    raise SystemExit(0)\n"
        "ok = (len(argv) == 7 and argv[0] == '--disable' and argv[1] == 'daemon_auto_start'\n"
        "      and argv[2] == 'queue' and argv[3] == '--thread' and argv[5] == '--message')\n"
        "if not ok:\n"
        "    raise SystemExit(9)\n"
        + tail, encoding="utf-8")
    script.chmod(0o755)
    return script, marker


def board_only(repo, task_id: str = "unit-task", thread: str | None = None,
               state: str = "running", round_number: int = 3,
               transport: str = pi_board.TRANSPORT_OFFLINE, codex_bin=None) -> dict:
    board = {"schemaVersion": 1, "revision": 1, "createdAt": 1, "updatedAt": 1, "cards": {}}
    card = pi_board._new_card(task_id, thread, "Unit task", "Unit goal", "brief.md", None,
                              repo.root, repo.state_dir, str(repo.root), transport, codex_bin, 1)
    card["pi"] = {"round": round_number, "state": state, "stage": "implementing", "updatedAt": 1}
    board["cards"][task_id] = card
    return board


def write_board(repo, board) -> Path:
    path = repo.state_dir / "board.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(board, indent=2), encoding="utf-8")
    return path


def captured_runner(calls, status="queued"):
    def run(argv, timeout):
        calls.append({"argv": argv, "timeout": timeout})
        code = 0 if status == "queued" else 2
        return {"status": status, "exitCode": code, "timedOut": False,
                "outputSha256": "0" * 64, "outputExcerpt": status, "argv0": argv[0]}
    return run


class BoardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-pi-board4-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # Trap the default binary name too: no test may ever reach a real codex.
        self.fake_bin = self.tmp / "fake-bin"
        make_cli_double(self.fake_bin, name="codex")

    def h_env(self, tmp: Path, **extra) -> dict:
        env = base_env(**extra)
        env["CODEX_PI_HANDOFF_ROOT"] = str(Path(tmp) / "handoffs")
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env.pop("CODEX_THREAD_ID", None)
        return env

    def tearDown(self):
        cleanup_repos()

    def make(self, name: str = "repo", config: dict | None = None):
        repo = Repo(self.tmp, name=name, config=config if config is not None else default_config())
        worktree = repo.worktree("wt")
        return repo, worktree

    def register(self, repo, task: str, env: dict, thread: str | None = None,
                 transport: str = pi_board.TRANSPORT_CLI_QUEUE, **extra) -> dict:
        args = ["register", "--repo", str(repo.root), "--task", task, "--transport", transport]
        if thread is not None:
            args += ["--thread", thread]
        for key, value in extra.items():
            flag = f"--{key.replace('_', '-')}"
            if value is True:
                args.append(flag)
            elif value is not None:
                args += [flag, str(value)]
        return board_json(*args, env=env)

    def refresh(self, repo, task: str, env: dict) -> dict:
        return board_json("refresh", "--repo", str(repo.root), "--task", task, env=env)

    def dispatch(self, repo, task: str, env: dict, **extra) -> dict:
        args = ["dispatch", "--repo", str(repo.root), "--task", task]
        for key, value in extra.items():
            args += [f"--{key.replace('_', '-')}", str(value)]
        return board_json(*args, env=env)

    def read_board(self, repo) -> dict:
        return json.loads((repo.state_dir / "board.json").read_text(encoding="utf-8"))

    def read_queue(self, repo) -> dict:
        path = repo.state_dir / "board.queue.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"tasks": {}}

    def card(self, repo, task: str) -> dict:
        return self.read_board(repo)["cards"][task]

    def marker_lines(self, marker: Path) -> list:
        if not marker.exists():
            return []
        return [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines() if line]

    def wait_for(self, predicate, timeout: float = 10, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    # ------------------------------------------------------------------
    # offline / no-change / registration
    # ------------------------------------------------------------------
    def test_offline_registration_never_invokes_cli(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-offline", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-offline", env=env)["state"], "completed")
        double, marker = make_cli_double(self.tmp)
        result = self.register(repo, "board-offline", env, transport=pi_board.TRANSPORT_OFFLINE,
                               codex_bin=None)
        self.assertEqual(result["mode"], "offline")
        self.assertIsNone(result["queueArgvTemplate"])
        self.assertIsNone(result["routePath"])
        dispatched = self.dispatch(repo, "board-offline", env)
        self.assertFalse(dispatched["dispatched"])
        self.assertEqual(dispatched["reason"], "transport is not cli-queue")
        self.assertFalse(marker.exists(), "offline boards and dispatch never invoke Codex")
        self.assertTrue(self.card(repo, "board-offline")["events"])

    def test_no_pending_events_and_no_change_never_invoke_cli(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="hang")
        double, marker = make_cli_double(self.tmp)
        repo.start("board-quiet", worktree, env=env)
        repo.wait_round_state("board-quiet", "running")
        try:
            self.register(repo, "board-quiet", env, thread=THREAD_A, codex_bin=double)
            self.assertFalse(marker.exists(), "a running task with no events dispatches nothing")
            self.dispatch(repo, "board-quiet", env)
            self.assertFalse(marker.exists())
            self.refresh(repo, "board-quiet", env)
            self.assertFalse(marker.exists(), "unchanged refresh dispatches nothing")
        finally:
            repo.cancel("board-quiet", env=env)
            repo.wait_terminal("board-quiet", env=env, timeout=25)

    def test_invalid_owner_is_rejected_and_not_fuzzy_routed(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-owner", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-owner", env=env)["state"], "completed")
        missing = run_board("register", "--repo", repo.root, "--task", "board-owner",
                            "--transport", "cli-queue", env=env, expect=2)
        self.assertIn("thread UUID is required", missing.stderr)
        bad = run_board("register", "--repo", repo.root, "--task", "board-owner",
                        "--transport", "cli-queue", "--thread", "not-a-uuid",
                        env=env, expect=2)
        self.assertIn("exact UUID", bad.stderr)
        fuzzy = run_board("register", "--repo", repo.root, "--task", "board-owner",
                          "--transport", "cli-queue", "--thread", "11111111-2222-3333-4444-55555555555",
                          env=env, expect=2)
        self.assertIn("exact UUID", fuzzy.stderr)

    # ------------------------------------------------------------------
    # dispatch argv, batching, retries, uncertainty
    # ------------------------------------------------------------------
    def test_register_dispatches_terminal_task_with_exact_argv(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        double, marker = make_cli_double(self.tmp)
        repo.start("board-queue", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-queue", env=env)["state"], "completed")
        result = self.register(repo, "board-queue", env, thread=THREAD_A, codex_bin=double,
                               title="Queue task", goal="Deliver the queue event",
                               brief_ref="brief.md")
        self.assertEqual(result["mode"], "cli-queue")
        self.assertEqual(Path(result["codexBin"]), double.resolve())
        self.assertTrue(result["queueArgvTemplate"])
        self.assertEqual(result["queueArgvTemplate"][1:4], ["--disable", "daemon_auto_start", "queue"])
        lines = self.marker_lines(marker)
        self.assertEqual(len(lines), 1, "registration dispatches an already-terminal task once")
        argv = lines[0]["argv"]
        self.assertEqual(len(argv), 7)
        self.assertEqual(argv[:4], ["--disable", "daemon_auto_start", "queue", "--thread"])
        self.assertEqual(argv[4], THREAD_A)
        self.assertEqual(argv[5], "--message")
        packet = argv[6]
        self.assertNotIn("-m", argv)
        self.assertNotIn("--model", argv)
        self.assertTrue(Path(result["queuePath"]).exists())
        self.assertTrue(Path(result["routePath"]).is_file())
        self.assertTrue(result["limitations"])
        self.assertIn("runtime handoff", packet)
        self.assertIn("never acceptance", packet)
        self.assertIn("title=Queue task", packet)
        self.assertIn("goal=Deliver the queue event", packet)
        self.assertIn("review_required", packet)
        self.assertIn("candidate_head=", packet)
        self.assertIn("decide=python3", packet)
        card = self.card(repo, "board-queue")
        self.assertFalse(card["events"][0]["handled"], "queueing is not handling")
        self.assertEqual(card["codex"]["review"], "pending")
        queue = self.read_queue(repo)["tasks"]["board-queue"]
        self.assertEqual(queue["lastStatus"], "queued")
        self.assertEqual(len(queue["claims"]), 1)
        again = self.dispatch(repo, "board-queue", env)
        self.assertFalse(again["dispatched"], "a queued event is never automatically resent")
        self.assertEqual(len(self.marker_lines(marker)), 1)

    def test_dispatch_batches_four_events_and_keeps_the_remainder(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        board = board_only(repo, task_id="batch-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        card = board["cards"]["batch-task"]
        events = []
        for index in range(4):
            events.append(pi_board.add_event(card, "resource_breach", 3, f"check-{index}:p:100",
                                             f"breach {index}", {"round": 3, "head": None},
                                             {"guardPath": "p"}, "resolve or repair", index))
        write_board(repo, board)
        first = self.dispatch(repo, "batch-task", env)
        self.assertTrue(first["dispatched"])
        self.assertEqual(len(first["eventIds"]), 3, "the packet limit is respected")
        second = self.dispatch(repo, "batch-task", env)
        self.assertTrue(second["dispatched"])
        self.assertEqual(len(second["eventIds"]), 1)
        third = self.dispatch(repo, "batch-task", env)
        self.assertFalse(third["dispatched"], "all four events are queued exactly once")
        lines = self.marker_lines(marker)
        self.assertEqual(len(lines), 2)
        sent = first["eventIds"] + second["eventIds"]
        self.assertEqual(sorted(sent), sorted(event["id"] for event in events))
        self.assertEqual(len(set(sent)), 4)

    def test_dispatch_timeout_is_uncertain_and_requires_rearm(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp, behavior="slow", seconds=8)
        board = board_only(repo, task_id="uncertain-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["uncertain-task"], "task_timeout", 3, "exit=124",
                           "timed out", {"round": 3, "head": None}, {}, "resolve", 1)
        write_board(repo, board)
        first = self.dispatch(repo, "uncertain-task", env, timeout=2.0)
        self.assertEqual(first["status"], "uncertain")
        self.assertTrue(first["timedOut"])
        self.assertIn("uncertain", json.dumps(self.read_queue(repo)))
        pid_path = Path(str(marker) + ".pid")
        self.assertTrue(pid_path.is_file(), "the slow double started")
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("a timed-out queue child must be cleaned up")
        attempts = len(self.marker_lines(marker))
        second = self.dispatch(repo, "uncertain-task", env, timeout=2.0)
        self.assertFalse(second["dispatched"], "uncertain sends are not retried automatically")
        self.assertEqual(len(self.marker_lines(marker)), attempts)
        rearmed = board_json("rearm", "--repo", repo.root, "--task", "uncertain-task", env=env)
        self.assertGreaterEqual(rearmed["clearedClaims"], 1)
        third = self.dispatch(repo, "uncertain-task", env, timeout=2.0)
        self.assertTrue(third["dispatched"], "an explicit rearm enables one more send")
        self.assertGreater(len(self.marker_lines(marker)), attempts)

    def test_delivery_then_crash_is_uncertain_and_never_auto_resent(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp, behavior="crash")
        board = board_only(repo, task_id="crash-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["crash-task"], "resource_breach", 3, "check:p:100",
                           "breach", {"round": 3, "head": None}, {}, "resolve", 1)
        write_board(repo, board)
        first = self.dispatch(repo, "crash-task", env)
        self.assertEqual(first["status"], "uncertain",
                         "a delivery side effect followed by a crash is ambiguous")
        self.assertTrue(first["error"])
        self.assertEqual(len(self.marker_lines(marker)), 1)
        for _ in range(3):
            self.assertFalse(self.dispatch(repo, "crash-task", env)["dispatched"])
        self.assertEqual(len(self.marker_lines(marker)), 1,
                         "an ambiguous spawn outcome is never automatically resent")
        board_json("rearm", "--repo", repo.root, "--task", "crash-task", env=env)
        self.assertTrue(self.dispatch(repo, "crash-task", env)["dispatched"])
        self.assertEqual(len(self.marker_lines(marker)), 2,
                         "explicit recovery may duplicate an uncertain delivery")

    def test_spawned_nonzero_exit_is_uncertain_not_retried(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp, behavior="fail", code=2)
        board = board_only(repo, task_id="nonzero-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["nonzero-task"], "resource_breach", 3, "check:p:100",
                           "breach", {"round": 3, "head": None}, {}, "resolve", 1)
        write_board(repo, board)
        self.assertEqual(self.dispatch(repo, "nonzero-task", env)["status"], "uncertain")
        self.assertFalse(self.dispatch(repo, "nonzero-task", env)["dispatched"])
        self.assertEqual(len(self.marker_lines(marker)), 1)
        board_json("rearm", "--repo", repo.root, "--task", "nonzero-task", env=env)
        self.assertTrue(self.dispatch(repo, "nonzero-task", env)["dispatched"])

    def test_not_started_failure_retries_are_bounded(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        bad = self.tmp / "bad-exec"
        bad.write_text("this is not an executable format\n", encoding="utf-8")
        bad.chmod(0o755)
        board = board_only(repo, task_id="not-started", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(bad))
        pi_board.add_event(board["cards"]["not-started"], "resource_breach", 3, "check:p:100",
                           "breach", {"round": 3, "head": None}, {}, "resolve", 1)
        write_board(repo, board)
        first = self.dispatch(repo, "not-started", env)
        self.assertEqual(first["status"], "failed")
        self.assertIn("could not start", first["error"])
        second = self.dispatch(repo, "not-started", env)
        self.assertEqual(second["status"], "failed")
        third = self.dispatch(repo, "not-started", env)
        self.assertFalse(third["dispatched"], "bounded retries stop after the limit")
        board_json("rearm", "--repo", repo.root, "--task", "not-started", env=env)
        self.assertTrue(self.dispatch(repo, "not-started", env)["dispatched"])

    def test_dispatch_corrupt_or_locked_queue_is_visible_and_silent(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        board = board_only(repo, task_id="bad-queue", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["bad-queue"], "review_required", 3, "completed:abc:0",
                           "terminal", {"round": 3, "head": "a" * 40}, {}, "review", 1)
        write_board(repo, board)
        queue_path = repo.state_dir / "board.queue.json"
        queue_path.write_text("{broken", encoding="utf-8")
        failed = run_board("dispatch", "--repo", repo.root, "--task", "bad-queue", env=env)
        failed_payload = json.loads(failed.stdout)
        self.assertFalse(failed_payload["dispatched"])
        self.assertIn("queue state is invalid", failed_payload["error"])
        self.assertFalse(marker.exists(), "a corrupt queue must not send anything")
        queue_path.unlink()
        lock_path = repo.state_dir / "board.queue.lock"
        stream = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            locked = self.dispatch(repo, "bad-queue", env)
            self.assertFalse(locked["dispatched"])
            self.assertIn("claim lock", locked["reason"])
            self.assertFalse(marker.exists())
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()
        self.assertTrue(self.dispatch(repo, "bad-queue", env)["dispatched"])

    def test_pause_and_interrupt_suppress_dispatch_until_resume(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        board = board_only(repo, task_id="paused-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["paused-task"], "review_required", 3, "completed:abc:0",
                           "terminal", {"round": 3, "head": "a" * 40}, {}, "review", 1)
        write_board(repo, board)
        board_json("pause", "--repo", repo.root, "--task", "paused-task", env=env)
        self.assertFalse(self.dispatch(repo, "paused-task", env)["dispatched"])
        self.assertFalse(marker.exists())
        board_json("resume", "--repo", repo.root, "--task", "paused-task",
                   "--thread", THREAD_A, env=env)
        self.assertTrue(self.dispatch(repo, "paused-task", env)["dispatched"])
        self.assertEqual(len(self.marker_lines(marker)), 1)

        # Interrupt route pause: only an explicit resume clears it.
        board = board_only(repo, task_id="interrupt-task", thread=THREAD_B,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["interrupt-task"], "review_required", 3, "completed:def:0",
                           "terminal", {"round": 3, "head": "b" * 40}, {}, "review", 1)
        write_board(repo, board)
        run_hook({"hook_event_name": "Interrupt", "session_id": THREAD_B,
                  "cwd": str(repo.root), "turn_id": "t1"}, env)
        before = len(self.marker_lines(marker))
        self.assertFalse(self.dispatch(repo, "interrupt-task", env)["dispatched"])
        human = run_hook({"hook_event_name": "UserPromptSubmit", "session_id": THREAD_B,
                          "cwd": str(repo.root), "prompt": "ordinary question"}, env)
        self.assertEqual(json.loads(human.stdout), {}, "a human prompt is never blocked")
        self.assertFalse(self.dispatch(repo, "interrupt-task", env)["dispatched"])
        self.assertEqual(len(self.marker_lines(marker)), before)
        board_json("resume", "--repo", repo.root, "--task", "interrupt-task",
                   "--thread", THREAD_B, env=env)
        self.assertTrue(self.dispatch(repo, "interrupt-task", env)["dispatched"])

    # ------------------------------------------------------------------
    # final acceptance defect: exact real commit validation
    # ------------------------------------------------------------------
    def _review_board(self, repo, task_id: str, head: str) -> Path:
        board = board_only(repo, task_id=task_id, thread=THREAD_A, state="completed", round_number=2)
        pi_board.add_event(board["cards"][task_id], "review_required", 2, f"completed:{head}:0",
                           "terminal", {"round": 2, "head": head}, {"stateRef": "s"},
                           "review the candidate", 1)
        return write_board(repo, board)

    def test_accept_requires_a_real_commit_in_the_registered_repo(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        repo.commit_file("one.txt", "one\n")
        first = repo._git("rev-parse", "HEAD")
        repo.commit_file("two.txt", "two\n")
        second = repo._git("rev-parse", "HEAD")
        self.assertNotEqual(first, second)
        board_file = self._review_board(repo, "commit-task", first)
        board, _ = pi_board.read_board(board_file)
        event = pi_board.pending_events(board["cards"]["commit-task"])[0]
        # Historical candidate: an older commit than HEAD is still reviewable.
        accepted = board_json("decide", "--repo", repo.root, "--task", "commit-task",
                              "--event-id", event["id"], "--decision", "accept",
                              "--reviewed-head", first, env=env)
        self.assertTrue(accepted["aggregate"])
        self.assertEqual(accepted["reviewedHead"], first.lower())
        board, _ = pi_board.read_board(board_file)
        self.assertEqual(board["cards"]["commit-task"]["codex"]["review"], "accepted")

    def test_accept_rejects_nonexistent_short_non_commit_and_wrong_commits(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        repo.commit_file("file.txt", "content\n")
        real = repo._git("rev-parse", "HEAD")
        blob = repo._git("rev-parse", "HEAD:file.txt")
        board_file = self._review_board(repo, "oid-task", real)
        board, _ = pi_board.read_board(board_file)
        event = pi_board.pending_events(board["cards"]["oid-task"])[0]
        cases = [
            (blob, "not a commit"),
            ("a" * 40, "not a commit"),
            ("abcdef1", "full 40- or 64-hex"),
            ("g" * 40, "full 40- or 64-hex"),
        ]
        for head, expected in cases:
            proc = run_board("decide", "--repo", repo.root, "--task", "oid-task",
                             "--event-id", event["id"], "--decision", "accept",
                             "--reviewed-head", head, env=env, expect=2)
            self.assertIn(expected, proc.stderr, (head, proc.stderr))
        missing_short = run_board("decide", "--repo", repo.root, "--task", "oid-task",
                                  "--event-id", event["id"], "--decision", "accept",
                                  "--reviewed-head", "abcdef1", env=env, expect=2)
        self.assertIn("full 40- or 64-hex", missing_short.stderr)
        # A different real commit must not match the event candidate.
        repo.commit_file("second.txt", "second\n")
        other = repo._git("rev-parse", "HEAD")
        mismatch = run_board("decide", "--repo", repo.root, "--task", "oid-task",
                             "--event-id", event["id"], "--decision", "accept",
                             "--reviewed-head", other, env=env, expect=2)
        self.assertIn("does not resolve to the event candidate", mismatch.stderr)
        # Faults cannot be accepted and the event remains unhandled.
        board, _ = pi_board.read_board(board_file)
        self.assertFalse(board["cards"]["oid-task"]["events"][0]["handled"])

    def test_old_round_decision_is_history_only_and_conflicting_replay_conflicts(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        repo.commit_file("one.txt", "one\n")
        old_head = repo._git("rev-parse", "HEAD")
        repo.commit_file("two.txt", "two\n")
        new_head = repo._git("rev-parse", "HEAD")
        board = board_only(repo, task_id="round-task", thread=THREAD_A, round_number=4)
        card = board["cards"]["round-task"]
        old_event = pi_board.add_event(card, "review_required", 3, "old",
                                       "round 3 terminal", {"round": 3, "head": old_head},
                                       {}, "review", 1)
        new_event = pi_board.add_event(card, "review_required", 4, "new",
                                       "round 4 terminal", {"round": 4, "head": new_head},
                                       {}, "review", 2)
        write_board(repo, board)
        old = board_json("decide", "--repo", repo.root, "--task", "round-task",
                         "--event-id", old_event["id"], "--decision", "accept",
                         "--reviewed-head", old_head, env=env)
        self.assertFalse(old["aggregate"])
        self.assertEqual(self.card(repo, "round-task")["codex"]["review"], "pending")
        new = board_json("decide", "--repo", repo.root, "--task", "round-task",
                         "--event-id", new_event["id"], "--decision", "accept",
                         "--reviewed-head", new_head, env=env)
        self.assertTrue(new["aggregate"])
        conflict = run_board("decide", "--repo", repo.root, "--task", "round-task",
                             "--event-id", new_event["id"], "--decision", "accept",
                             "--reviewed-head", old_head, env=env, expect=2)
        self.assertTrue("different reviewed head" in conflict.stderr
                        or "does not resolve to the event candidate" in conflict.stderr)

    # ------------------------------------------------------------------
    # retention / projection / monitor
    # ------------------------------------------------------------------
    def test_pending_events_beyond_display_budget_are_retained(self):
        repo, _worktree = self.make()
        board = board_only(repo)
        card = board["cards"]["unit-task"]
        for index in range(60):
            pi_board.add_event(card, "resource_breach", 3, f"check-{index}:path:100",
                               f"breach {index}", {"round": 3, "head": None},
                               {"guardPath": "p"}, "resolve or repair", 2 + index)
        self.assertEqual(len(pi_board.pending_events(card)), 60)
        self.assertTrue(card["overflow"]["active"])
        write_board(repo, board)
        env = self.h_env(self.tmp)
        show = board_json("show", "--repo", repo.root, "--task", "unit-task", env=env)
        self.assertEqual(show["cards"][0]["pendingCount"], 60)
        self.assertTrue(show["cards"][0]["overflow"]["active"])

    def test_replay_of_pruned_handled_identity_does_not_resurface(self):
        repo, _worktree = self.make()
        board = board_only(repo)
        card = board["cards"]["unit-task"]
        event = pi_board.add_event(card, "resource_breach", 3, "check:path:100", "s", {}, {}, "q", 1)
        event.update(handled=True, handledAt=2, decision="resolved")
        pi_board._prune_events(card)
        for index in range(pi_board.MAX_HANDLED_EVENTS + 5):
            extra = pi_board.add_event(card, "resource_breach", 3, f"other-{index}:p:1",
                                       "s", {}, {}, "q", 3 + index)
            extra.update(handled=True, handledAt=4 + index, decision="resolved")
            pi_board._prune_events(card)
        self.assertIsNone(pi_board.find_event(card, event["id"]))
        self.assertIn(event["id"], card["handled"])
        self.assertIsNone(pi_board.add_event(card, "resource_breach", 3, "check:path:100",
                                             "s", {}, {}, "q", 99))

    def test_terminal_projection_and_dedup(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-terminal", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-terminal", env=env)["state"], "completed")
        self.register(repo, "board-terminal", env, transport=pi_board.TRANSPORT_OFFLINE)
        card = self.card(repo, "board-terminal")
        events = [event for event in card["events"] if event["kind"] == "review_required"]
        self.assertEqual(len(events), 1)
        revision = self.read_board(repo)["revision"]
        self.refresh(repo, "board-terminal", env)
        self.assertEqual(self.read_board(repo)["revision"], revision)
        self.assertEqual(len([event for event in self.card(repo, "board-terminal")["events"]
                              if event["kind"] == "review_required"]), 1)

    def test_failed_check_alone_is_not_an_event_but_timeout_and_breach_are(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-checks", worktree, env=env)
        repo.wait_round_state("board-checks", "running")
        checks = repo.task_dir("board-checks") / "rounds" / "1" / "round.checks"
        self.register(repo, "board-checks", env, transport=pi_board.TRANSPORT_OFFLINE)
        plain = self.synth_receipt(checks, "plain-failure", 3)
        self.refresh(repo, "board-checks", env)
        card = self.card(repo, "board-checks")
        self.assertEqual([event for event in card["events"] if event["kind"] == "check_failed"], [])
        self.assertTrue(card["evidence"]["receiptRef"].endswith(plain.name))
        self.synth_receipt(checks, "timeout-check", 124, timed_out=True)
        breach = {"path": str(self.tmp / "watch"), "max_bytes": 100, "observed_bytes": 9999,
                  "breached": True, "unknown": False, "complete": True,
                  "breach_basis": "partial lower bound",
                  "reason": "observed 9999 bytes exceed the declared max_bytes 100"}
        self.synth_receipt(checks, "breaching-check", 75, resource_limit=breach)
        self.refresh(repo, "board-checks", env)
        kinds = {event["kind"] for event in self.card(repo, "board-checks")["events"]}
        self.assertIn("check_timeout", kinds)
        self.assertIn("resource_breach", kinds)
        revision = self.read_board(repo)["revision"]
        self.refresh(repo, "board-checks", env)
        self.assertEqual(self.read_board(repo)["revision"], revision)
        repo.cancel("board-checks", env=env)
        repo.wait_terminal("board-checks", env=env, timeout=25)

    def synth_receipt(self, checks: Path, check_id: str, exit_code: int, *, timed_out=False,
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

    def test_resource_fingerprint_is_stable_while_bytes_grow(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-growing-breach", worktree, env=env)
        repo.wait_round_state("board-growing-breach", "running")
        checks = repo.task_dir("board-growing-breach") / "rounds" / "1" / "round.checks"
        checks.mkdir(parents=True, exist_ok=True)
        log = checks / "growing-check-abc.log"
        log.write_text("check output\n", encoding="utf-8")
        marker = checks / "growing-check-abc.running"

        def emit(observed):
            marker.write_text(json.dumps({
                "schema_version": 1, "id": "growing-check", "pid": os.getpid(),
                "started_at": time.time(), "deadline_at": time.time() + 600,
                "timeout_seconds": 600, "deadline_scope": "wrapper timeout only",
                "log": log.name, "receipt": "growing-check-abc.json",
                "resource_limit": {"path": str(self.tmp / "watch"), "max_bytes": 100,
                                   "observed_bytes": observed, "breached": True,
                                   "unknown": False, "complete": True,
                                   "reason": f"observed at least {observed} bytes"},
            }), encoding="utf-8")

        try:
            self.register(repo, "board-growing-breach", env, transport=pi_board.TRANSPORT_OFFLINE)
            emit(100)
            self.refresh(repo, "board-growing-breach", env)
            emit(900)
            self.refresh(repo, "board-growing-breach", env)
            events = [event for event in self.card(repo, "board-growing-breach")["events"]
                      if event["kind"] == "resource_breach"]
            self.assertEqual(len(events), 1)
        finally:
            repo.cancel("board-growing-breach", env=env)
            repo.wait_terminal("board-growing-breach", env=env, timeout=25)

    def test_unregistered_task_avoids_status_work(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="registered-other")
        write_board(repo, board)
        calls = []
        original = pi_board.build_status

        def forbidden(*args, **kwargs):
            calls.append(args)
            raise AssertionError("build_status must not run for an unregistered task")

        pi_board.build_status = forbidden
        try:
            result = pi_board.refresh_registered_task({
                "repo": str(repo.root), "task": "not-registered",
                "commonDir": str(repo.state_dir.parent)})
        finally:
            pi_board.build_status = original
        self.assertFalse(result["refreshed"])
        self.assertEqual(calls, [])

    def test_monitor_freshness_and_transport_are_visible_without_events(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-monitor", worktree, env=env)
        repo.wait_round_state("board-monitor", "running")
        try:
            double, _marker = make_cli_double(self.tmp)
            self.register(repo, "board-monitor", env, thread=THREAD_A, codex_bin=double)
            healthy = board_json("show", "--repo", repo.root, "--task", "board-monitor", env=env)
            self.assertEqual(healthy["monitor"]["board-monitor"]["status"], "healthy")
            self.assertEqual(healthy["queue"]["board-monitor"]["status"], "idle")
            # Stale/error monitor evidence is exposed on recovery, not auto-queued.
            path = repo.state_dir / "board.monitor.json"
            path.write_text(json.dumps({"schemaVersion": 1, "tasks": {
                "board-monitor": {"task": "board-monitor", "healthy": False,
                                  "error": "simulated monitor failure",
                                  "refreshedAt": time.time() - 100000}}}), encoding="utf-8")
            stale = board_json("show", "--repo", repo.root, "--task", "board-monitor", env=env)
            self.assertEqual(stale["monitor"]["board-monitor"]["status"], "error")
            recovered = board_json("recover", "--thread", THREAD_A, env=env)
            self.assertTrue(any("monitor=error" in line for line in recovered["lines"]),
                            recovered["lines"])
            hook = json.loads(run_hook({"hook_event_name": "SessionStart",
                                        "session_id": THREAD_A, "cwd": str(repo.root)},
                                       env).stdout)
            context = hook["hookSpecificOutput"]["additionalContext"]
            self.assertIn("cli-queue transport", context)
            self.assertIn("monitor=error", context)
        finally:
            repo.cancel("board-monitor", env=env)
            repo.wait_terminal("board-monitor", env=env, timeout=25)

    def test_monitor_exception_becomes_a_visible_unhealthy_lease(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="monitor-boom", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        board_file = write_board(repo, board)
        original = pi_board.build_status

        def broken(*args, **kwargs):
            raise ValueError("simulated status failure")

        pi_board.build_status = broken
        try:
            result = pi_board.supervisor_tick({
                "repo": str(repo.root), "task": "monitor-boom",
                "commonDir": str(repo.state_dir.parent)})
        finally:
            pi_board.build_status = original
        self.assertFalse(result["ok"])
        monitors, problem = pi_board.read_monitors(board_file)
        self.assertIsNone(problem)
        record = pi_board.monitor_for(monitors, "monitor-boom")
        self.assertFalse(record.get("healthy"))
        self.assertIn("simulated status failure", record.get("error") or "")

    # ------------------------------------------------------------------
    # legacy Stop overlap
    # ------------------------------------------------------------------
    def test_arm_and_stop_are_disabled_for_cli_queue_tasks(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        double, _marker = make_cli_double(self.tmp)
        repo.start("board-overlap", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-overlap", env=env)["state"], "completed")
        armed = run_handoff("arm", "--repo", repo.root, "--task", "board-overlap", "--round", "1",
                            "--session-id", THREAD_A, env=env)
        self.assertEqual(armed.returncode, 0)
        self.register(repo, "board-overlap", env, thread=THREAD_A, codex_bin=double)
        refused = run_handoff("arm", "--repo", repo.root, "--task", "board-overlap", "--round", "1",
                              "--session-id", THREAD_B, env=env, expect=2)
        self.assertIn("cli-queue", refused.stderr)
        hook = json.loads(run_hook({"hook_event_name": "Stop", "session_id": THREAD_A,
                                    "cwd": str(repo.root)}, env).stdout)
        self.assertNotEqual(hook.get("decision"), "block",
                            "the board queue owns the notification")
        self.assertIn("cli-queue", json.dumps(hook))

    # ------------------------------------------------------------------
    # helper upgrade
    # ------------------------------------------------------------------
    def test_cli_double_allows_only_queue_and_rejects_forbidden_capabilities(self):
        double, _marker = make_cli_double(self.tmp, name="trap")
        for argv in (["exec", "x"], ["resume", "--last"], ["fork"], ["app-server"],
                     ["-m", "gpt"], ["--model", "gpt"],
                     ["--disable", "daemon_auto_start", "queue", "--thread", "x"]):
            proc = subprocess.run([str(double), *argv], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 9, f"forbidden argv must fail: {argv}")
        self.assertEqual(subprocess.run([str(double), "--help"], capture_output=True).returncode, 0)
        allowed = subprocess.run(
            [str(double), "--disable", "daemon_auto_start", "queue", "--thread", THREAD_A,
             "--message", "packet"], capture_output=True, text=True)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_concurrent_dispatch_claims_exactly_once(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        board = board_only(repo, task_id="race-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["race-task"], "review_required", 3, "completed:abc:0",
                           "terminal", {"round": 3, "head": "a" * 40}, {}, "review", 1)
        write_board(repo, board)
        procs = [subprocess.Popen([sys.executable, str(BOARD), "dispatch", "--repo",
                                   str(repo.root), "--task", "race-task"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(3)]
        results = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(len(self.marker_lines(marker)), 1,
                         "a concurrent tick must not duplicate the queue claim")
        self.assertEqual(sum(1 for item in results if item["dispatched"]), 1)
        queue = self.read_queue(repo)["tasks"]["race-task"]
        self.assertEqual(len(queue["claims"]), 1)

    def test_dispatched_queue_is_never_automatically_resent(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        board = board_only(repo, task_id="resent-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["resent-task"], "review_required", 3, "completed:abc:0",
                           "terminal", {"round": 3, "head": "a" * 40}, {}, "review", 1)
        write_board(repo, board)
        self.assertTrue(self.dispatch(repo, "resent-task", env)["dispatched"])
        for _ in range(3):
            self.assertFalse(self.dispatch(repo, "resent-task", env)["dispatched"])
        self.assertEqual(len(self.marker_lines(marker)), 1)
        result = self.dispatch(repo, "resent-task", env)
        self.assertFalse(result["dispatched"])
        self.assertIn("idempotency key", result["queue"]["limitations"][0])

    def test_unregistered_dispatch_never_invokes_cli(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        board = board_only(repo, task_id="other-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        write_board(repo, board)
        result = self.dispatch(repo, "missing-task", env)
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["reason"], "task is not registered")
        self.assertFalse(marker.exists())

    def test_missing_board_is_a_visible_dispatch_error(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        result = self.dispatch(repo, "no-board", env)
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["status"], "unknown")

    def test_supervisor_final_tick_dispatches_terminal_state(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="delay-ok", PI_DOUBLE_DELAY="1.2")
        double, marker = make_cli_double(self.tmp)
        repo.start("board-final", worktree, env=env)
        repo.wait_round_state("board-final", "running")
        self.register(repo, "board-final", env, thread=THREAD_A, codex_bin=double)
        self.assertFalse(marker.exists(), "registration while running dispatches nothing")
        self.assertEqual(repo.wait_terminal("board-final", env=env, timeout=30)["state"],
                         "completed")

        def dispatched_once():
            return self.marker_lines(marker)

        self.wait_for(dispatched_once, timeout=10, what="supervisor final dispatch")

        def finalized():
            queue = self.read_queue(repo).get("tasks", {}).get("board-final", {})
            claims = queue.get("claims", {})
            return claims and all(claim.get("status") == "queued" for claim in claims.values())

        self.wait_for(finalized, timeout=10, what="queue finalize")
        lines = self.marker_lines(marker)
        self.assertEqual(len(lines), 1, "the supervisor publishes terminal state and dispatches once")
        card = self.card(repo, "board-final")
        event = card["events"][0]
        queue = self.read_queue(repo)["tasks"]["board-final"]
        self.assertEqual(queue["claims"][event["id"]]["status"], "queued")
        self.assertFalse(event["handled"])

    def test_corrupt_board_is_a_visible_dispatch_error(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        board_file = repo.state_dir / "board.json"
        board_file.parent.mkdir(parents=True, exist_ok=True)
        board_file.write_text("{broken", encoding="utf-8")
        result = self.dispatch(repo, "corrupt-task", env)
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("board state is", result["error"])
        self.assertFalse(marker.exists())

    def test_packet_byte_limit_keeps_omitted_events_unclaimed(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="size-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["size-task"]
        events = []
        for index in range(4):
            events.append(pi_board.add_event(card, "resource_breach", 3,
                                             f"size-{index}:p:1", "s" * pi_board.MAX_SUMMARY,
                                             {"round": 3, "head": None},
                                             {"guardPath": "p"}, "q" * 300, index))
        original = pi_board.MAX_PACKET_CHARS
        pi_board.MAX_PACKET_CHARS = 2000
        try:
            text, included = pi_board.build_packet(card, events)
        finally:
            pi_board.MAX_PACKET_CHARS = original
        self.assertGreaterEqual(len(included), 1)
        self.assertLess(len(included), 4, "the byte boundary must omit at least one event")
        self.assertLessEqual(len(text), pi_board.MAX_PACKET_CHARS)
        for event in included:
            self.assertIn(event["id"], text)
        omitted = [event for event in events if event not in included]
        self.assertTrue(omitted)
        for event in omitted:
            self.assertNotIn(event["id"], text)

    def test_rearm_refuses_live_inflight_and_late_result_cannot_overwrite(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        board = board_only(repo, task_id="claim-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        event = pi_board.add_event(board["cards"]["claim-task"], "review_required", 3,
                                   "completed:abc:0", "terminal",
                                   {"round": 3, "head": "a" * 40}, {}, "review", 1)
        board_file = write_board(repo, board)
        now = time.time()
        self.assertEqual(pi_board._claim_queue(board_file, "claim-task", [event["id"]], now,
                                               "packet-live"), [event["id"]])
        targeted = run_board("rearm", "--repo", repo.root, "--task", "claim-task",
                             "--event-id", event["id"], env=env, expect=2)
        self.assertIn("live inflight", targeted.stderr)
        bulk = board_json("rearm", "--repo", repo.root, "--task", "claim-task", env=env)
        self.assertEqual(bulk["clearedClaims"], 0)
        self.assertEqual(bulk["refusedInflight"], [event["id"]])
        calls = []
        blocked = pi_board.dispatch_task(board_file, "claim-task",
                                         cli_runner=captured_runner(calls))
        self.assertFalse(blocked["dispatched"])
        self.assertEqual(calls, [], "a live inflight claim must never be claimed twice")
        # Pause/resume must not clear or duplicate the live claim.
        board_json("pause", "--repo", repo.root, "--task", "claim-task", env=env)
        board_json("resume", "--repo", repo.root, "--task", "claim-task", env=env)
        queue, _ = pi_board.read_queue(board_file)
        self.assertEqual(queue["tasks"]["claim-task"]["claims"][event["id"]]["status"], "inflight")
        # Age it: rearm marks uncertain, then explicit recovery clears it.
        claims = queue["tasks"]["claim-task"]["claims"]
        claims[event["id"]]["at"] = now - 10_000
        pi_board._write_queue(board_file, queue)
        marked = board_json("rearm", "--repo", repo.root, "--task", "claim-task", env=env)
        self.assertEqual(marked["markedUncertain"], [event["id"]])
        recovered = board_json("rearm", "--repo", repo.root, "--task", "claim-task",
                               "--event-id", event["id"], env=env)
        self.assertEqual(recovered["clearedClaims"], 1)
        # A late result from the old packet must not overwrite the recovered state.
        pi_board._finish_queue(board_file, "claim-task", [event["id"]],
                               {"status": "queued", "exitCode": 0}, time.time(), "packet-live")
        queue, _ = pi_board.read_queue(board_file)
        entry = queue["tasks"]["claim-task"]
        self.assertNotIn(event["id"], entry["claims"])
        self.assertEqual(entry["lastSkippedFinalize"]["packetId"], "packet-live")

    def test_stdout_capture_is_bounded_while_reading(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp, behavior="spam")
        board = board_only(repo, task_id="spam-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE, codex_bin=str(double))
        pi_board.add_event(board["cards"]["spam-task"], "review_required", 3, "completed:abc:0",
                           "terminal", {"round": 3, "head": "a" * 40}, {}, "review", 1)
        write_board(repo, board)
        result = self.dispatch(repo, "spam-task", env)
        self.assertEqual(result["status"], "queued")
        self.assertLessEqual(len(result["receipt"]["outputExcerpt"]), 1000,
                             "captured output must be bounded while the child runs")
        self.assertEqual(len(self.marker_lines(marker)), 1)

    def test_near_full_packet_keeps_the_mandatory_drain_instruction(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="near-full", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["near-full"]
        events = []
        for index in range(4):
            evidence = {"briefRef": "b" * 1336} if index == 0 else {"guardPath": "p"}
            events.append(pi_board.add_event(card, "resource_breach", 3,
                                             f"near-{index}:p:1", "s" * 100,
                                             {"round": 3, "head": None}, evidence,
                                             "q" * 100, index))
        text, included = pi_board.build_packet(card, events)
        self.assertTrue(included)
        self.assertLess(len(included), 4, "a near-full packet must omit events, not the instruction")
        self.assertIn("pending event(s)", text)
        self.assertIn("same turn", text)
        self.assertIn("pi_board.py show", text)
        self.assertLessEqual(len(text.encode("utf-8")), pi_board.MAX_PACKET_CHARS)
        for event in included:
            self.assertIn(event["id"], text)
        omitted = [event for event in events if event not in included]
        for event in omitted:
            self.assertNotIn(event["id"], text)

    def test_limit_count_overflow_keeps_the_drain_instruction(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="count-full", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["count-full"]
        events = [pi_board.add_event(card, "resource_breach", 3, f"count-{i}:p:1", "s",
                                     {"round": 3, "head": None}, {}, "q", i)
                  for i in range(5)]
        text, included = pi_board.build_packet(card, events)
        self.assertEqual(len(included), pi_board.MAX_PACKET_EVENTS)
        self.assertIn("pending event(s)", text)
        self.assertIn("same turn", text)
        self.assertLessEqual(len(text.encode("utf-8")), pi_board.MAX_PACKET_CHARS)

    def test_utf8_near_full_packet_keeps_drain_and_byte_bound(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="utf8-near", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["utf8-near"]
        events = [pi_board.add_event(card, "resource_breach", 3, f"multi-{i}:p:1", "é" * 200,
                                     {"round": 3, "head": None}, {"briefRef": "é" * 300},
                                     "q" * 120, i) for i in range(4)]
        text, included = pi_board.build_packet(card, events)
        self.assertTrue(included)
        self.assertLessEqual(len(text.encode("utf-8")), pi_board.MAX_PACKET_CHARS)
        self.assertIn("pending event(s)", text, "the drain instruction must survive the byte reserve")

    def test_oversized_fallback_with_remaining_events(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="oversize-many", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["oversize-many"]
        events = [pi_board.add_event(card, "resource_breach", 3, f"big-{i}:p:1", "x" * 300,
                                     {"round": 3, "head": None}, {}, "q" * 300, i)
                  for i in range(3)]
        original = pi_board.MAX_PACKET_CHARS
        pi_board.MAX_PACKET_CHARS = 300
        try:
            text, included = pi_board.build_packet(card, events)
        finally:
            pi_board.MAX_PACKET_CHARS = original
        self.assertEqual(included, [events[0]])
        self.assertIn(events[0]["id"], text)
        self.assertTrue("board_ref=" in text or "show" in text)
        self.assertLessEqual(len(text.encode("utf-8")), 300)

    def test_default_codex_bin_is_resolved_to_an_absolute_path(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-default-bin", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-default-bin", env=env)["state"], "completed")
        result = self.register(repo, "board-default-bin", env, thread=THREAD_A)
        self.assertTrue(Path(result["codexBin"]).is_absolute())
        self.assertEqual(Path(result["codexBin"]), (self.fake_bin / "codex").resolve())
        marker = self.fake_bin / "codex.jsonl"
        self.assertTrue(marker.exists(), "the resolved default binary performed the dispatch")

    def test_packet_utf8_byte_limit_is_bounded(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="utf8-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["utf8-task"]
        events = []
        for index in range(3):
            events.append(pi_board.add_event(card, "resource_breach", 3, f"utf8-{index}:p:1",
                                             "é" * 200, {"round": 3, "head": None},
                                             {"guardPath": "p"}, "q" * 120, index))
        original = pi_board.MAX_PACKET_CHARS
        pi_board.MAX_PACKET_CHARS = 1500
        try:
            text, included = pi_board.build_packet(card, events)
        finally:
            pi_board.MAX_PACKET_CHARS = original
        self.assertTrue(included)
        self.assertLessEqual(len(text.encode("utf-8")), 1500,
                             "the packet limit must be enforced in UTF-8 bytes")

    def test_oversized_event_falls_back_with_id_and_board_ref(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="oversize-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["oversize-task"]
        event = pi_board.add_event(card, "resource_breach", 3, "huge:p:1", "x" * 300,
                                   {"round": 3, "head": None}, {"guardPath": "p"},
                                   "q" * 300, 1)
        original = pi_board.MAX_PACKET_CHARS
        pi_board.MAX_PACKET_CHARS = 300
        try:
            text, included = pi_board.build_packet(card, [event])
        finally:
            pi_board.MAX_PACKET_CHARS = original
        self.assertEqual(included, [event], "an oversized event must not be silently dropped")
        self.assertIn(event["id"], text, "the fallback keeps the exact event id")
        self.assertTrue("show" in text or "board_ref=" in text,
                        "the fallback gives a bounded board evidence reference")
        self.assertLessEqual(len(text.encode("utf-8")), 300)

    def test_overflow_packet_directs_draining_in_the_same_turn(self):
        repo, _worktree = self.make()
        board = board_only(repo, task_id="drain-task", thread=THREAD_A,
                           transport=pi_board.TRANSPORT_CLI_QUEUE)
        card = board["cards"]["drain-task"]
        events = []
        for index in range(55):
            events.append(pi_board.add_event(card, "resource_breach", 3, f"drain-{index}:p:1",
                                             f"breach {index}", {"round": 3, "head": None},
                                             {"guardPath": "p"}, "resolve", index))
        self.assertTrue(card["overflow"]["active"])
        text, included = pi_board.build_packet(card, events)
        self.assertTrue(included)
        self.assertIn("overflow=", text)
        self.assertIn("same turn", text)
        self.assertIn("pi_board.py show", text)
        lower = text.lower()
        self.assertIn("drain", lower)

    def make_frozen_task(self, repo, task: str):
        task_dir = repo.task_dir(task)
        round_dir = task_dir / "rounds" / "1"
        round_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "session").mkdir(parents=True, exist_ok=True)
        (task_dir / "task.json").write_text(json.dumps({
            "schemaVersion": 1, "task": task, "taskDir": str(task_dir), "repo": str(repo.root),
            "commonDir": str(repo.state_dir.parent), "worktree": str(repo.root), "readOnly": False,
            "model": MODEL, "thinking": "max", "constraints": [], "checks": [],
            "maxWorkers": 1, "timeoutSeconds": 60, "createdAt": time.time(),
            "startHead": None, "runtimeVersion": "test", "helperHashes": {},
            "sessionId": task, "sessionDir": str(task_dir / "session"),
        }), encoding="utf-8")
        (round_dir / "round.jsonl").touch()
        (round_dir / "round.err").touch()
        (round_dir / "round.meta").write_text("", encoding="utf-8")
        (round_dir / "round.state.json").write_text(json.dumps({
            "schemaVersion": 1, "round": 1, "state": "starting", "startedAt": time.time(),
            "exitCode": None, "timedOut": False, "cancelled": False,
            "taskDir": str(task_dir), "briefSha256": "x",
        }), encoding="utf-8")
        (task_dir / "cancel.json").write_text(json.dumps({
            "schemaVersion": 1, "task": task, "round": 1, "requestedAt": time.time(),
            "nonce": "pre-spawn",
        }), encoding="utf-8")
        return task_dir

    def test_prespawn_cancel_makes_the_same_final_board_notification(self):
        repo, _worktree = self.make()
        repo.commit_file("seed.txt", "seed\n")
        env = self.h_env(self.tmp)
        double, marker = make_cli_double(self.tmp)
        task_dir = self.make_frozen_task(repo, "prespawn")
        self.register(repo, "prespawn", env, thread=THREAD_A, codex_bin=double)
        lock_fd = os.open(task_dir / ".task.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            proc = subprocess.run(
                [sys.executable, str(CLI), "_worker", "--task-dir", str(task_dir),
                 "--round", "1", "--lock-fd", str(lock_fd), "--timeout-seconds", "60"],
                capture_output=True, text=True, env=env, timeout=60)
        finally:
            os.close(lock_fd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = json.loads((task_dir / "rounds" / "1" / "round.state.json").read_text())
        self.assertEqual(state["state"], "cancelled")
        self.assertTrue(self.marker_lines(marker),
                        "the pre-spawn cancel must still dispatch the terminal notification")
        card = self.card(repo, "prespawn")
        kinds = {event["kind"] for event in card["events"]}
        self.assertIn("review_required", kinds)
        queue = self.read_queue(repo)["tasks"]["prespawn"]
        self.assertTrue(any(claim.get("status") == "queued"
                            for claim in queue["claims"].values()), queue)

    def make_snapshot_task(self, repo, task: str, state: str = "running",
                           meta_text: str = "exit=None\n"):
        task_dir = self.make_frozen_task(repo, task)
        (task_dir / "cancel.json").unlink()
        state_path = task_dir / "rounds" / "1" / "round.state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data["state"] = state
        state_path.write_text(json.dumps(data), encoding="utf-8")
        (task_dir / "rounds" / "1" / "round.meta").write_text(meta_text, encoding="utf-8")
        tools = task_dir / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "pi_board.py").write_text("junk\n", encoding="utf-8")
        return task_dir

    def test_upgrade_requires_a_well_formed_terminal_round(self):
        repo, _worktree = self.make()
        env = self.h_env(self.tmp)
        for state in ("running", "starting", "unknown"):
            task = f"snap-{state}"
            task_dir = self.make_snapshot_task(repo, task, state=state)
            frozen_before = (task_dir / "task.json").read_bytes()
            refused = run_cli("upgrade", "--repo", repo.root, "--task", task,
                              env=env, expect=2)
            self.assertIn("not terminal-known", refused.stderr)
            self.assertEqual((task_dir / "tools" / "pi_board.py").read_text(encoding="utf-8"),
                             "junk\n", "refused upgrades must not touch helper bytes")
            self.assertEqual((task_dir / "task.json").read_bytes(), frozen_before,
                             "refused upgrades must not touch task metadata")
            self.assertFalse((task_dir / "tools.new").exists())

        missing = self.make_snapshot_task(repo, "snap-missing", state="completed")
        (missing / "rounds" / "1" / "round.state.json").unlink()
        refused = run_cli("upgrade", "--repo", repo.root, "--task", "snap-missing",
                          env=env, expect=2)
        self.assertIn("no readable round.state.json", refused.stderr)

        noexit = self.make_snapshot_task(repo, "snap-noexit", state="completed", meta_text="")
        refused = run_cli("upgrade", "--repo", repo.root, "--task", "snap-noexit",
                          env=env, expect=2)
        self.assertIn("no exit evidence", refused.stderr)

        done = self.make_snapshot_task(repo, "snap-done", state="completed", meta_text="exit=0\n")
        ok = json.loads(run_cli("upgrade", "--repo", repo.root, "--task", "snap-done",
                                env=env, expect=0).stdout)
        self.assertTrue(ok["ok"])
        self.assertEqual((done / "tools" / "pi_board.py").read_bytes(),
                         (RUNTIME / "pi_board.py").read_bytes())
        self.assertFalse((done / "tools.new").exists())

    def test_upgrade_refused_when_lifecycle_locks_are_acquired_first(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("upgrade-locks", worktree, env=env)
        self.assertEqual(repo.wait_terminal("upgrade-locks", env=env)["state"], "completed")
        task_dir = repo.task_dir("upgrade-locks")
        tools = task_dir / "tools"
        (tools / "pi_board.py").write_text("junk\n", encoding="utf-8")
        handles = []
        for name in (".task.lock", ".supervisor.lock"):
            handle = open(task_dir / name, "a+", encoding="utf-8")
            fcntl.flock(handle, fcntl.LOCK_EX)
            handles.append(handle)
        try:
            refused = run_cli("upgrade", "--repo", repo.root, "--task", "upgrade-locks",
                              env=env, expect=2)
            self.assertIn("never hot-edited", refused.stderr)
            self.assertEqual((tools / "pi_board.py").read_text(encoding="utf-8"), "junk\n",
                             "a refused upgrade must not half-publish helpers")
            self.assertFalse((task_dir / "tools.new").exists())
        finally:
            for handle in handles:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
        # With the locks released the same command succeeds cleanly.
        result = json.loads(run_cli("upgrade", "--repo", repo.root, "--task", "upgrade-locks",
                                    env=env, expect=0).stdout)
        self.assertTrue(result["ok"])
        self.assertEqual((tools / "pi_board.py").read_bytes(),
                         (RUNTIME / "pi_board.py").read_bytes())

    def test_upgrade_refused_when_only_the_supervisor_lock_is_held(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("upgrade-lease", worktree, env=env)
        self.assertEqual(repo.wait_terminal("upgrade-lease", env=env)["state"], "completed")
        task_dir = repo.task_dir("upgrade-lease")
        handle = open(task_dir / ".supervisor.lock", "a+", encoding="utf-8")
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            refused = run_cli("upgrade", "--repo", repo.root, "--task", "upgrade-lease",
                              env=env, expect=2)
            self.assertIn("supervisor lease", refused.stderr)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def test_upgrade_waits_for_the_lifecycle_admission_lock(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("upgrade-admission", worktree, env=env)
        self.assertEqual(repo.wait_terminal("upgrade-admission", env=env)["state"], "completed")
        admission = repo.state_dir / ".admission.lock"
        handle = open(admission, "a+", encoding="utf-8")
        fcntl.flock(handle, fcntl.LOCK_EX)
        proc = subprocess.Popen([sys.executable, str(CLI), "upgrade", "--repo", str(repo.root),
                                 "--task", "upgrade-admission"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            time.sleep(0.8)
            self.assertIsNone(proc.poll(),
                              "upgrade must wait for the lifecycle admission lock, not race it")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertTrue(json.loads(stdout)["ok"])

    def test_upgrade_restores_snapshot_for_terminal_task(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="ok")
        repo.start("board-upgrade", worktree, env=env)
        self.assertEqual(repo.wait_terminal("board-upgrade", env=env)["state"], "completed")
        tools = repo.task_dir("board-upgrade") / "tools"
        (tools / "pi_board.py").write_text("junk\n", encoding="utf-8")
        result = json.loads(run_cli("upgrade", "--repo", repo.root, "--task", "board-upgrade",
                                    env=env, expect=0).stdout)
        self.assertTrue(result["ok"])
        self.assertEqual((tools / "pi_board.py").read_bytes(),
                         (RUNTIME / "pi_board.py").read_bytes())
        frozen = json.loads((repo.task_dir("board-upgrade") / "task.json").read_text())
        self.assertEqual(frozen["helperHashes"]["pi_board.py"],
                         hashlib.sha256((RUNTIME / "pi_board.py").read_bytes()).hexdigest())
        for name, digest in frozen["helperHashes"].items():
            self.assertEqual(digest, hashlib.sha256((RUNTIME / name).read_bytes()).hexdigest(),
                             f"snapshot metadata must match the runtime helper {name}")
        self.assertTrue(frozen.get("upgradeHistory"))
        self.assertFalse((repo.task_dir("board-upgrade") / "tools.new").exists())
        self.assertTrue(result["backupDir"])

    def test_upgrade_refused_while_active(self):
        repo, worktree = self.make()
        env = self.h_env(self.tmp, PI_DOUBLE_MODE="hang")
        repo.start("board-active-upgrade", worktree, env=env)
        repo.wait_round_state("board-active-upgrade", "running")
        try:
            refused = run_cli("upgrade", "--repo", repo.root, "--task", "board-active-upgrade",
                              env=env, expect=2)
            self.assertIn("never hot-edited", refused.stderr)
        finally:
            repo.cancel("board-active-upgrade", env=env)
            repo.wait_terminal("board-active-upgrade", env=env, timeout=25)


if __name__ == "__main__":
    unittest.main()
