#!/usr/bin/env python3
"""Bounded Pi evidence summary. Process completion is never acceptance PASS.

No Codex CLI is invoked. Reasoning tokens are never added on top of provider totals.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

# The frozen helper snapshot is immutable evidence: never write bytecode caches
# into the task tools directory.
sys.dont_write_bytecode = True

from pi_size import sanitize_snapshot

RISKY = [
    (r"\bgit\s+push\b", "git push"), (r"\bgit\s+stash\b", "git stash"),
    (r"\bgit\s+add\s+(-A|--all|\.)(\s|$)", "git add -A / ."),
    (r"--no-verify\b", "--no-verify"), (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+(checkout|switch)\s+(-b\s+)?(main|master|bake-[\w-]+)\b", "switch branch"),
    (r"\brm\s+-rf?\s+/(?!\S*build/)", "absolute rm"),
    (r"\bPLAN\.md\b", "mentions PLAN.md (inspect read versus write)"),
    (r"\bdocs/plan/", "frozen docs/plan"),
]
ABS_PATH = re.compile(r"(?<![\w.])(/(?:private/)?(?:tmp|var|Users|home|etc)/[^\s'\"`;|&)]*)")
USAGE_KEYS = ("input", "cacheRead", "cacheWrite", "output", "totalTokens")


def inside(path: Path, roots: list[Path]) -> bool:
    path = path.resolve()
    return any(path == root or root in path.parents for root in roots)


def read_meta(path: Path) -> dict:
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            # Legacy model/thinking share their first line; paths may contain spaces.
            if line.startswith("task="):
                result.update(part.split("=", 1) for part in line.split() if "=" in part)
            elif "=" in line:
                key, value = line.split("=", 1)
                result[key] = value
    return result


def receipts(directory: Path) -> list[dict]:
    result = []
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            log = path.parent / data["log"]
            valid = (inside(log, [path.parent.resolve()]) and log.is_file()
                     and hashlib.sha256(log.read_bytes()).hexdigest() == data["log_sha256"])
            result.append({key: data.get(key) for key in (
                "id", "argv", "head", "dirty", "exit_code", "timed_out", "test_counts", "log")}
                | {"receipt": str(path), "log_verified": valid,
                   "resource_limit": sanitize_snapshot(data.get("resource_limit")
                                                       or data.get("resourceLimit"))})
        except (ValueError, KeyError, OSError, TypeError):
            result.append({"receipt": str(path), "log_verified": False, "exit_code": None})
    return result


def summarize(log: Path, worktree: Path, run_dir: Path | None = None,
              expected_model: str | None = None, checks_dir: Path | None = None) -> dict:
    run_dir = (run_dir or log.parent).resolve()
    worktree = worktree.resolve()
    allowed = [worktree, run_dir]
    meta = read_meta(log.with_suffix(".meta"))
    expected_model = expected_model or meta.get("model")
    models, stop_reasons, counts, totals = Counter(), Counter(), Counter(), Counter()
    usage_samples = Counter()
    flags, errors, commands = [], [], []
    started = {}
    malformed = turns = assistant_messages = 0
    final, cost, cost_samples = "", 0.0, 0
    for line_no, raw in enumerate(log.open() if log.exists() else (), 1):
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("not an event")
        except (ValueError, TypeError):
            malformed += 1
            continue
        kind = event.get("type")
        if kind == "turn_end":
            turns += 1
        elif kind == "tool_execution_start":
            tool, args = event.get("toolName", "?"), event.get("args") or {}
            counts[tool] += 1
            text = str(args.get("command") or args.get("path") or args)
            for pattern, label in RISKY:
                if re.search(pattern, text):
                    flags.append(f"line {line_no}: {label}: {text[:180]}")
            if tool in ("read", "write", "edit") and args.get("path"):
                path = Path(args["path"])
                if not inside(path if path.is_absolute() else worktree / path, allowed):
                    flags.append(f"line {line_no}: {tool} outside allowed directories: {str(path)[:180]}")
            elif tool == "bash":
                for path in ABS_PATH.findall(text):
                    if not inside(Path(path), allowed):
                        flags.append(f"line {line_no}: bash outside path: {path[:180]}")
                command = {"command": text, "line": line_no, "exit_code": None, "tool_error": None}
                commands.append(command)
                started[event.get("toolCallId")] = command
        elif kind == "tool_execution_end":
            command = started.get(event.get("toolCallId"))
            if command is not None:
                command["tool_error"] = event.get("isError")
                details = (event.get("result") or {}).get("details") or {}
                code = details.get("exitCode")
                if isinstance(code, int) and not isinstance(code, bool):
                    command["exit_code"] = code
                # isError=False and a green-looking text are not a numeric exit code.
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            assistant_messages += 1
            model = f'{message.get("provider", "?")}/{message.get("model", "?")}'
            models[model] += 1
            stop_reasons[str(message.get("stopReason", "unknown"))] += 1
            if message.get("errorMessage"):
                errors.append(str(message["errorMessage"])[:300])
            usage = message.get("usage") or {}
            for key in USAGE_KEYS:
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[key] += value
                    usage_samples[key] += 1
            value = (usage.get("cost") or {}).get("total")
            if isinstance(value, (int, float)):
                cost += value
                cost_samples += 1
            texts = [part["text"] for part in message.get("content", [])
                     if part.get("type") == "text" and part.get("text")]
            if texts:
                final = "\n".join(texts)
    model_check = "unknown"
    if models and expected_model:
        model_check = "matched" if set(models) == {expected_model} else "mismatch"
    usage = {key: totals[key] if usage_samples[key] else None for key in USAGE_KEYS}
    full_usage = bool(assistant_messages) and all(usage_samples[k] == assistant_messages
                                                for k in USAGE_KEYS[:-1])
    check_dir = checks_dir or (run_dir / (log.stem.replace("round-", "checks-") if log.stem.startswith("round-") else "checks"))
    return {"schema_version": 1, "source": str(log.resolve()), "source_bytes": log.stat().st_size if log.exists() else 0,
            "turns": turns, "assistant_messages": assistant_messages, "models": dict(models),
            "expected_model": expected_model, "model_check": model_check,
            "usage": usage, "usage_complete": full_usage,
            "reported_cost_usd": cost if cost_samples else None, "cost_samples": cost_samples,
            "stop_reasons": dict(stop_reasons), "tool_counts": dict(counts),
            "commands": commands, "guardrail_flags": list(dict.fromkeys(flags)),
            "errors": errors, "malformed_lines": malformed, "final_text": final,
            "check_receipts": receipts(check_dir), "checks_dir": str(check_dir.resolve()),
            "process_exit": meta.get("exit"),
            "acceptance": "not_verified"}


CHECK_FLAGS = ("failed", "timed_out", "unverified_log", "unknown_exit", "skipped", "zero_run", "unknown_counts", "resource_breach")


def check_flag_map(check: dict) -> dict:
    code = check.get("exit_code")
    counts = check.get("test_counts") or {}
    resource = check.get("resource_limit") or {}
    return {"failed": (code is not None and code != 0) or bool(counts.get("fail")),
            "timed_out": bool(check.get("timed_out")),
            "unverified_log": not check["log_verified"],
            "unknown_exit": code is None,
            "skipped": bool(counts.get("skip")),
            "zero_run": bool(counts) and counts.get("run") == 0,
            "unknown_counts": not counts,
            "resource_breach": bool(resource.get("breached"))}


def check_overview(data: dict, limit: int = 8) -> dict:
    """Aggregate every receipt (never dropping failures) and show at most `limit`."""
    checks = data.get("check_receipts", [])
    totals = Counter()
    for check in checks:
        totals.update({key: int(value) for key, value in check_flag_map(check).items()})
    selected = sorted(checks, key=lambda check: tuple(-int(check_flag_map(check)[key])
                                                      for key in CHECK_FLAGS))[:max(0, limit)]
    receipts = [{"id": check.get("id"), "receipt": check.get("receipt"),
                 "exit_code": check.get("exit_code"), "timed_out": check.get("timed_out"),
                 "test_counts": check.get("test_counts"), "log_verified": check.get("log_verified"),
                 "resource_limit": check.get("resource_limit"),
                 "flags": [key for key in CHECK_FLAGS if check_flag_map(check)[key]]} for check in selected]
    return {"total": len(checks), "counts": {key: totals[key] for key in CHECK_FLAGS},
            "receipts": receipts, "listed": len(receipts),
            "absence": "missing evidence: no check receipts were recorded" if not checks else None,
            "note": "all attempts are aggregated; retries do not erase failures"}


def bounded(data: dict, receipt_limit: int = 8) -> dict:
    """Small, safe-to-return structured evidence. Not acceptance."""
    return {"schema_version": 1, "source": data["source"], "source_bytes": data["source_bytes"],
            "checks_dir": data.get("checks_dir"), "turns": data["turns"],
            "assistant_messages": data["assistant_messages"], "models": data["models"],
            "expected_model": data["expected_model"], "model_check": data["model_check"],
            "usage": data["usage"], "usage_complete": data["usage_complete"],
            "reported_cost_usd": data["reported_cost_usd"], "cost_samples": data["cost_samples"],
            "stop_reasons": data["stop_reasons"], "tool_counts": data["tool_counts"],
            "guardrail_flags": data["guardrail_flags"][:20],
            "guardrail_flag_count": len(data["guardrail_flags"]),
            "errors": data["errors"][:20], "error_count": len(data["errors"]),
            "malformed_lines": data["malformed_lines"],
            "final_excerpt": data["final_text"][:1200],
            "process_exit": data["process_exit"], "checks": check_overview(data, receipt_limit),
            "acceptance": "not_verified"}


def compact(data: dict) -> str:
    lines = [f'Pi evidence: turns={data["turns"]} messages={data["assistant_messages"]} process_exit={data["process_exit"]}',
             f'model_check={data["model_check"]} models={data["models"]}',
             f'usage={data["usage"]} complete={data["usage_complete"]} reported_cost_usd={data["reported_cost_usd"]}',
             f'tools={data["tool_counts"]} malformed_lines={data["malformed_lines"]}',
             'acceptance=not_verified (requires PLAN checks and independent review)']
    for label, items in (("errors", data["errors"]), ("guardrail_flags", data["guardrail_flags"])):
        lines.append(f"{label}: count={len(items)}")
        lines.extend("  " + str(item)[:240] for item in items[:5])
    commands = data["commands"]
    lines.append(f'commands: total={len(commands)} unknown_exit={sum(c["exit_code"] is None for c in commands)} '
                 f'tool_errors={sum(c["tool_error"] is True for c in commands)}')
    checks = data["check_receipts"]
    totals = Counter()
    for check in checks:
        totals.update({key: int(value) for key, value in check_flag_map(check).items()})
    lines.append(f"receipt_attempts: total={len(checks)} " + " ".join(f"{key}={totals[key]}" for key in (
        "failed", "timed_out", "unverified_log", "unknown_exit", "skipped", "zero_run", "unknown_counts",
        "resource_breach")))
    lines.append("All attempts counted; retries do not erase failures. Absence is missing evidence.")
    priority = CHECK_FLAGS
    selected = sorted(checks, key=lambda check: tuple(-int(check_flag_map(check)[key]) for key in priority))[:8]
    for check in selected:
        lines.append(f'  {check.get("id", "?")}: exit={check.get("exit_code")} log_verified={check["log_verified"]} counts={check.get("test_counts")}')
    lines.extend(["final excerpt (untrusted report):", data["final_text"][:1200],
                  f'full evidence: {data["source"]}; use --json or --commands only for a specific investigation'])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--checks-dir", type=Path)
    parser.add_argument("--expected-model")
    parser.add_argument("--json", action="store_true", help="Full structured evidence, preferably redirect to disk")
    parser.add_argument("--commands", action="store_true", help="Explicitly expand command evidence")
    args = parser.parse_args()
    data = summarize(args.jsonl, args.worktree, args.run_dir, args.expected_model, args.checks_dir)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.commands:
        print(json.dumps(data["commands"], ensure_ascii=False, indent=2))
    else:
        print(compact(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
