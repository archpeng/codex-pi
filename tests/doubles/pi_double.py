#!/usr/bin/env python3
"""Offline test double for the Pi CLI.

Behaves enough like ``pi -p --mode json`` for the runtime, without any model,
network, or Codex access. Scenario is selected with PI_DOUBLE_MODE:

  ok                     emit a successful JSON event stream and exit 0
  fail                   emit events and exit 3
  hang                   emit a start event, then sleep (kill to stop)
  delay-ok               wait PI_DOUBLE_DELAY seconds, then behave like ok
  session-trace          record argv once, then behave like ok
  descendant-ignore-term spawn an ignore-TERM grandchild, then exit 0
  descendant-hang        spawn an ignore-TERM grandchild, then hang

The recorded session id / session dir / tools / model are appended as JSON
lines to $PI_DOUBLE_TRACE when that variable is set.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRANDCHILD = HERE / "grandchild.py"


def arg_value(flag: str):
    argv = sys.argv[1:]
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return None


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def reported_model() -> tuple:
    """Offline metadata only: lets tests simulate a provider reporting another model."""
    return (os.environ.get("PI_DOUBLE_REPORTED_PROVIDER", "deepseek"),
            os.environ.get("PI_DOUBLE_REPORTED_MODEL", "deepseek-flash"))


def emit_success(final_text: str = "test double finished") -> None:
    provider, model = reported_model()
    emit({"type": "turn_start"})
    emit({"type": "tool_execution_start", "toolName": "read",
          "args": {"path": "README.md"}, "toolCallId": "call-1"})
    emit({"type": "tool_execution_end", "toolCallId": "call-1", "isError": False,
          "result": {"details": {"exitCode": 0}}})
    emit({"type": "message_end", "message": {
        "role": "assistant", "provider": provider, "model": model,
        "stopReason": "stop",
        "usage": {"input": 10, "cacheRead": 0, "cacheWrite": 0, "output": 5, "totalTokens": 15},
        "content": [{"type": "text", "text": final_text}],
    }})
    emit({"type": "turn_end"})


def record_trace() -> None:
    trace = os.environ.get("PI_DOUBLE_TRACE")
    if not trace:
        return
    entry = {
        "argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
        "sessionId": arg_value("--session-id"), "sessionDir": arg_value("--session-dir"),
        "model": arg_value("--model"), "thinking": arg_value("--thinking"),
        "tools": arg_value("--tools"),
        "noExtensions": "--no-extensions" in sys.argv,
        "noSkills": "--no-skills" in sys.argv,
        "noPromptTemplates": "--no-prompt-templates" in sys.argv,
        "noContextFiles": "--no-context-files" in sys.argv,
        "atFile": next((item[1:] for item in sys.argv[1:] if item.startswith("@")), None),
    }
    with Path(trace).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry) + "\n")


def spawn_grandchild() -> None:
    pidfile = os.environ.get("PI_DOUBLE_GRANDCHILD_PIDFILE")
    if not pidfile:
        raise SystemExit("PI_DOUBLE_GRANDCHILD_PIDFILE is required for descendant scenarios")
    subprocess.Popen([sys.executable, str(GRANDCHILD), pidfile],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def main() -> int:
    mode = os.environ.get("PI_DOUBLE_MODE", "ok")
    record_trace()
    if mode == "fail":
        provider, model = reported_model()
        emit({"type": "turn_start"})
        emit({"type": "message_end", "message": {
            "role": "assistant", "provider": provider, "model": model,
            "stopReason": "stop", "usage": {"input": 3, "output": 2, "totalTokens": 5},
            "content": [{"type": "text", "text": "deliberate failure"}],
        }})
        return 3
    if mode == "hang":
        emit({"type": "turn_start"})
        time.sleep(600)
        return 0
    if mode == "delay-ok":
        emit({"type": "turn_start"})
        time.sleep(float(os.environ.get("PI_DOUBLE_DELAY", "1.0")))
        emit_success()
        return 0
    if mode == "no-usage":
        provider, model = reported_model()
        emit({"type": "turn_end"})
        emit({"type": "message_end", "message": {
            "role": "assistant", "provider": provider, "model": model,
            "stopReason": "stop",
            "content": [{"type": "text", "text": "finished without reporting usage"}],
        }})
        return 0
    if mode == "descendant-ignore-term":
        spawn_grandchild()
        emit_success("parent finished while a descendant lingers")
        return 0
    if mode == "descendant-hang":
        spawn_grandchild()
        emit({"type": "turn_start"})
        time.sleep(600)
        return 0
    # default: ok / session-trace
    emit_success()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
