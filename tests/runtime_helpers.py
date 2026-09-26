"""Shared helpers for the Codex-Pi offline behavioral tests."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "runtime" / "pi_task.py"
RUNTIME = ROOT / "runtime"
DOUBLES = Path(__file__).resolve().parent / "doubles"
PI_DOUBLE = DOUBLES / "pi_double.py"
GRANDCHILD = DOUBLES / "grandchild.py"


def default_config(**overrides) -> dict:
    config = {"schemaVersion": 1, "model": "deepseek/deepseek-flash", "thinking": "max",
              "constraints": ["AGENTS.md"], "checks": ["make check"], "maxWorkers": 1,
              "timeoutSeconds": 14400}
    config.update(overrides)
    return config


def base_env(**extra) -> dict:
    try:
        os.chmod(PI_DOUBLE, 0o755)
    except OSError:
        pass
    env = os.environ.copy()
    env.update({"PI_BIN": str(PI_DOUBLE), "PI_DOUBLE_MODE": "ok"})
    env.pop("PI_DOUBLE_TRACE", None)
    env.pop("PI_DOUBLE_GRANDCHILD_PIDFILE", None)
    env.pop("PI_DOUBLE_REPORTED_PROVIDER", None)
    env.pop("PI_DOUBLE_REPORTED_MODEL", None)
    env.update({key: str(value) for key, value in extra.items()})
    return env


def write_config(checkout: Path, config: dict | None = None) -> Path:
    """Write a project config into one checkout (untracked, checkout-local)."""
    directory = Path(checkout) / ".agents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "codex-pi.json"
    path.write_text(json.dumps(config if config is not None else default_config(), indent=2),
                    encoding="utf-8")
    return path


def make_pi_trap(directory: Path, name: str = "pi-trap") -> tuple:
    """Executable that records if it is ever invoked. Returns (path, marker_path)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{name}.marker"
    trap = directory / name
    trap.write_text(f"#!/bin/sh\ntouch {marker}\nexit 9\n", encoding="utf-8")
    trap.chmod(0o755)
    return trap, marker


def run_cli(*args, env: dict | None = None, expect: int | None = 0,
            timeout: float = 90, cwd=None) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, str(CLI), *[str(arg) for arg in args]],
                          capture_output=True, text=True, env=env or base_env(),
                          timeout=timeout, cwd=cwd)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(f"pi_task {' '.join(str(arg) for arg in args)} exited "
                             f"{proc.returncode}, expected {expect}\n"
                             f"stdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def cli_json(*args, env: dict | None = None, timeout: float = 90) -> dict:
    proc = run_cli(*args, env=env, expect=0, timeout=timeout)
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise AssertionError(f"pi_task did not return JSON: {exc}\nstdout={proc.stdout}") from exc


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_gone(pid: int, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def kill_pid(pid, sig=signal.SIGKILL) -> None:
    try:
        os.kill(int(pid), sig)
    except (ProcessLookupError, ValueError, TypeError):
        pass


def snapshot_files(root: Path) -> dict:
    """Byte snapshot of every regular file under root, keyed by relative path."""
    result = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


class Repo:
    """A real temporary git repository plus real linked worktrees."""

    def __init__(self, tmp: Path, name: str = "repo", config: dict | None = None):
        self.tmp = Path(tmp)
        self.root = self.tmp / name
        self.root.mkdir(parents=True)
        _ACTIVE_REPOS.append(self)
        (self.root / "README.md").write_text("# test repo\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("add", "-A")
        self._git("-c", "user.email=test@example.invalid", "-c", "user.name=Test",
                  "commit", "-qm", "init")
        if config is not None:
            self.write_config(config)

    def write_config(self, config: dict) -> None:
        directory = self.root / ".agents"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "codex-pi.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    def _git(self, *args) -> str:
        return subprocess.check_output(["git", "-C", str(self.root), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def commit_file(self, name: str, text: str = "x\n") -> None:
        (self.root / name).write_text(text, encoding="utf-8")
        self._git("add", "-A")
        self._git("-c", "user.email=test@example.invalid", "-c", "user.name=Test",
                  "commit", "-qm", f"add {name}")

    def worktree(self, name: str = "wt", branch: str | None = None) -> Path:
        path = self.tmp / f"{self.root.name}-{name}"
        args = ["-C", str(self.root), "worktree", "add", "-q"]
        if branch is None:
            args += ["--detach", str(path), "HEAD"]
        else:
            args += ["-b", branch, str(path), "HEAD"]
        subprocess.run(["git", *args], check=True, capture_output=True)
        return path

    @property
    def state_dir(self) -> Path:
        return self.root / ".git" / "codex-pi"

    def task_dir(self, task: str) -> Path:
        return self.state_dir / "tasks" / task

    def start(self, task: str, worktree, prompt: str = "Implement the change.",
              read_only: bool = False, env: dict | None = None, expect: int | None = 0):
        args = ["start", "--repo", str(self.root), "--task", task,
                "--worktree", str(worktree), "--prompt", prompt]
        if read_only:
            args.append("--read-only")
        return run_cli(*args, env=env, expect=expect)

    def start_json(self, task: str, worktree, prompt: str = "Implement the change.",
                   read_only: bool = False, env: dict | None = None) -> dict:
        proc = self.start(task, worktree, prompt, read_only, env, expect=0)
        return json.loads(proc.stdout)

    def continue_task(self, task: str, prompt: str = "Continue the task.",
                      env: dict | None = None, expect: int | None = 0):
        return run_cli("continue", "--repo", str(self.root), "--task", task,
                       "--prompt", prompt, env=env, expect=expect)

    def cancel(self, task: str, env: dict | None = None):
        return cli_json("cancel", "--repo", str(self.root), "--task", task, env=env)

    def result(self, task: str, round: int | None = None, env: dict | None = None) -> dict:
        args = ["result", "--repo", str(self.root), "--task", task]
        if round is not None:
            args += ["--round", str(round)]
        return cli_json(*args, env=env)

    def wait_terminal(self, task: str, timeout: float = 30, env: dict | None = None,
                      round: int | None = None) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            result = self.result(task, round=round, env=env)
            if result["state"] not in ("starting", "running"):
                return result
            if time.monotonic() >= deadline:
                raise AssertionError(f"task {task!r} did not reach a terminal state; "
                                     f"state={result['state']} round={result.get('round')}")
            time.sleep(0.2)

    def kill_leftovers(self) -> None:
        """Test-only safety net: kill worker/pi process groups left by failed assertions."""
        tasks_dir = self.state_dir / "tasks"
        if not tasks_dir.is_dir():
            return
        for state_file in tasks_dir.glob("*/rounds/*/round.state.json"):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for key in ("piPid", "supervisorPid"):
                pid = state.get(key)
                if isinstance(pid, int) and not isinstance(pid, bool):
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass

    def wait_round_state(self, task: str, state_name: str, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            task_dir = self.task_dir(task)
            rounds = sorted((task_dir / "rounds").glob("*")) if (task_dir / "rounds").is_dir() else []
            if rounds:
                data = json.loads((rounds[-1] / "round.state.json").read_text(encoding="utf-8"))
                # The worker records state=running before spawning Pi and
                # records piPid immediately after; wait for the identity too so
                # callers never observe a half-written running record.
                ready = data.get("state") == state_name
                if ready and state_name == "running" and not isinstance(data.get("piPid"), int):
                    ready = False
                if ready:
                    return data
            if time.monotonic() >= deadline:
                raise AssertionError(f"round never entered state {state_name!r}")
            time.sleep(0.1)


_ACTIVE_REPOS: list[Repo] = []


def cleanup_repos() -> None:
    for repo in list(_ACTIVE_REPOS):
        repo.kill_leftovers()
    _ACTIVE_REPOS.clear()
