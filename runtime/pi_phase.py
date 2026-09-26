#!/usr/bin/env python3
"""Phase contract validation shared by the Codex-Pi runtime helpers.

A phase contract is a small machine-readable snapshot of the main session's
authorized design for one complete, independently reviewable phase. It is
stored under the task evidence directory as ``phase.json`` (contract plus its
frozen digest) and referenced by the round brief. This module only validates
shape, required fields, reference hashes and version consistency. It never
judges semantic design quality, never executes the declared check commands and
never claims acceptance.

No Codex CLI or model is invoked here.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

PHASE_SCHEMA_VERSION = 1
PHASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_CONTRACT_BYTES = 262_144
MAX_TEXT = 4_000
MAX_LIST_TEXT = 1_000
MAX_ITEMS = 100
MAX_SCOPE = 100
MAX_LIMITS = 50
MAX_BUDGET_SECONDS = 2_592_000.0
MAX_COMMAND_TIMEOUT_SECONDS = 604_800.0
MAX_DESIGN_BYTES = 10_485_760

CONTRACT_KEYS = (
    "schemaVersion", "phaseId", "goal", "result", "baseline", "scope", "designRef",
    "designSha256", "acceptanceItems", "budgetSeconds", "commandTimeoutSeconds",
    "resourceLimits", "autonomousRepair", "escalateWhen", "preconditions", "nextPhaseRef",
)
ITEM_KEYS = ("id", "description", "checkId", "command", "passCondition", "evidence",
             "minRun", "forbidSkip")


def _text(value, limit: int, label: str, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"phase contract {label} must be a non-empty string")
    text = value.replace("\x00", " ").strip()
    if len(text) > limit:
        raise ValueError(f"phase contract {label} exceeds {limit} characters")
    return text


def _text_list(value, label: str, required: bool = True, limit: int = MAX_LIST_TEXT):
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value):
        raise ValueError(f"phase contract {label} must be a "
                         f"{'non-empty ' if required else ''}list of non-empty strings")
    result = []
    for entry in value:
        result.append(_text(entry, limit, f"{label} entry"))
    return result


def _safe_relative(value, label: str) -> str:
    text = _text(value, 500, label)
    if text.startswith("/") or "\\" in text:
        raise ValueError(f"phase contract {label} must be a relative POSIX path: {text!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts or not parts:
        raise ValueError(f"phase contract {label} uses traversal or is empty: {text!r}")
    return "/".join(parts)


def _safe_scope(value, label: str) -> str:
    text = _text(value, 500, label)
    if text == ".":
        return "."
    return _safe_relative(text, label)


def _inside(path: Path, root: Path) -> bool:
    path, root = path.resolve(), root.resolve()
    return path == root or root in path.parents


def design_digest(root: Path, ref: str) -> str:
    """Digest the referenced design file, refusing traversal and oversized files."""
    relative = _safe_relative(ref, "designRef")
    candidate = Path(root).expanduser() / relative
    if not _inside(candidate, Path(root)):
        raise ValueError(f"phase contract designRef escapes the repository: {ref!r}")
    if not candidate.is_file():
        raise ValueError(f"phase contract designRef does not exist: {candidate}")
    try:
        size = candidate.stat().st_size
    except OSError as exc:
        raise ValueError(f"phase contract designRef is unreadable: {candidate}: {exc}") from None
    if size > MAX_DESIGN_BYTES:
        raise ValueError(f"phase contract designRef exceeds {MAX_DESIGN_BYTES} bytes: {candidate}")
    return hashlib.sha256(candidate.read_bytes()).hexdigest()


def validate_contract(data, root, check_design: bool = True) -> dict:
    """Validate one contract object and return a normalized copy.

    Only machine-checkable shape/identity/version facts are enforced. Declared
    check commands are references; this module never runs them and a valid
    contract is never evidence of quality or acceptance.
    """
    if not isinstance(data, dict):
        raise ValueError("phase contract must be a JSON object")
    unknown = sorted(set(data) - set(CONTRACT_KEYS))
    if unknown:
        raise ValueError(f"phase contract has unsupported keys {unknown}; allowed: "
                         f"{list(CONTRACT_KEYS)}")
    if data.get("schemaVersion") != PHASE_SCHEMA_VERSION:
        raise ValueError(f"phase contract schemaVersion must be {PHASE_SCHEMA_VERSION}, "
                         f"got {data.get('schemaVersion')!r}")
    phase_id = data.get("phaseId")
    if not isinstance(phase_id, str) or not PHASE_ID_RE.fullmatch(phase_id):
        raise ValueError("phase contract phaseId must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    goal = _text(data.get("goal"), MAX_TEXT, "goal")
    result = _text(data.get("result"), MAX_TEXT, "result")
    baseline = _text(data.get("baseline"), 500, "baseline")
    scope = [_safe_scope(entry, "scope entry") for entry in _text_list(data.get("scope"), "scope")]
    if len(scope) > MAX_SCOPE:
        raise ValueError(f"phase contract scope exceeds {MAX_SCOPE} entries")
    design_ref = _safe_relative(data.get("designRef"), "designRef")
    design_sha = data.get("designSha256")
    if not isinstance(design_sha, str) or not SHA256_RE.fullmatch(design_sha):
        raise ValueError("phase contract designSha256 must be a 64-character lowercase hex digest")
    if check_design:
        actual = design_digest(Path(root), design_ref)
        if actual != design_sha:
            raise ValueError(f"phase contract designSha256 {design_sha} does not match the "
                             f"current design file digest {actual} for {design_ref}")
    items = data.get("acceptanceItems")
    if not isinstance(items, list) or not items:
        raise ValueError("phase contract acceptanceItems must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"phase contract acceptanceItems exceeds {MAX_ITEMS} entries")
    normalized_items = []
    seen_ids = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"phase contract acceptanceItems[{index}] must be an object")
        unknown_item = sorted(set(item) - set(ITEM_KEYS))
        if unknown_item:
            raise ValueError(f"phase contract acceptanceItems[{index}] has unsupported keys "
                             f"{unknown_item}; allowed: {list(ITEM_KEYS)}")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not PHASE_ID_RE.fullmatch(item_id):
            raise ValueError(f"phase contract acceptanceItems[{index}].id must match "
                             "[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        if item_id in seen_ids:
            raise ValueError(f"phase contract acceptance item id {item_id!r} is duplicated")
        seen_ids.add(item_id)
        check_id = item.get("checkId") or item_id
        if not isinstance(check_id, str) or not PHASE_ID_RE.fullmatch(check_id):
            raise ValueError(f"phase contract acceptanceItems[{index}].checkId must match "
                             "[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        min_run = item.get("minRun")
        if min_run is not None and (isinstance(min_run, bool) or not isinstance(min_run, int)
                                    or min_run < 0 or min_run > 10 ** 9):
            raise ValueError(f"phase contract acceptanceItems[{index}].minRun must be a "
                             "non-negative integer")
        forbid_skip = item.get("forbidSkip", False)
        if not isinstance(forbid_skip, bool):
            raise ValueError(f"phase contract acceptanceItems[{index}].forbidSkip must be a boolean")
        normalized_items.append({
            "id": item_id,
            "checkId": check_id,
            "description": _text(item.get("description"), MAX_TEXT,
                                 f"acceptanceItems[{index}].description"),
            "command": _text(item.get("command"), MAX_LIST_TEXT,
                             f"acceptanceItems[{index}].command"),
            "passCondition": _text(item.get("passCondition"), MAX_LIST_TEXT,
                                   f"acceptanceItems[{index}].passCondition"),
            "evidence": _text(item.get("evidence"), MAX_LIST_TEXT,
                              f"acceptanceItems[{index}].evidence"),
            "minRun": min_run,
            "forbidSkip": forbid_skip,
        })
    budget = data.get("budgetSeconds")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) \
            or not math.isfinite(float(budget)) or not 0 < float(budget) <= MAX_BUDGET_SECONDS:
        raise ValueError(f"phase contract budgetSeconds must be positive and at most "
                         f"{int(MAX_BUDGET_SECONDS)}")
    command_timeout = data.get("commandTimeoutSeconds")
    if isinstance(command_timeout, bool) or not isinstance(command_timeout, (int, float)) \
            or not math.isfinite(float(command_timeout)) \
            or not 0 < float(command_timeout) <= MAX_COMMAND_TIMEOUT_SECONDS:
        raise ValueError("phase contract commandTimeoutSeconds must be present, positive and at "
                         f"most {int(MAX_COMMAND_TIMEOUT_SECONDS)}")
    limits = data.get("resourceLimits")
    if not isinstance(limits, list) or len(limits) > MAX_LIMITS:
        raise ValueError(f"phase contract resourceLimits must be declared as a list of at most "
                         f"{MAX_LIMITS} entries (an empty list explicitly declares none)")
    normalized_limits = []
    for index, entry in enumerate(limits):
        if not isinstance(entry, dict) or set(entry) != {"path", "maxBytes"}:
            raise ValueError(f"phase contract resourceLimits[{index}] must contain exactly "
                             "path and maxBytes")
        path = _text(entry.get("path"), 1_000, f"resourceLimits[{index}].path")
        max_bytes = entry.get("maxBytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError(f"phase contract resourceLimits[{index}].maxBytes must be a "
                             "positive integer")
        normalized_limits.append({"path": path, "maxBytes": max_bytes})
    next_phase = data.get("nextPhaseRef")
    if next_phase is not None:
        if not isinstance(next_phase, str) or not PHASE_ID_RE.fullmatch(next_phase):
            raise ValueError("phase contract nextPhaseRef must be a safe phase id")
    return {
        "schemaVersion": PHASE_SCHEMA_VERSION,
        "phaseId": phase_id,
        "goal": goal,
        "result": result,
        "baseline": baseline,
        "scope": scope,
        "designRef": design_ref,
        "designSha256": design_sha,
        "acceptanceItems": normalized_items,
        "budgetSeconds": float(budget),
        "commandTimeoutSeconds": float(command_timeout),
        "resourceLimits": normalized_limits,
        "autonomousRepair": _text_list(data.get("autonomousRepair"), "autonomousRepair"),
        "escalateWhen": _text_list(data.get("escalateWhen"), "escalateWhen"),
        "preconditions": _text_list(data.get("preconditions"), "preconditions", required=False),
        "nextPhaseRef": next_phase,
    }


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def contract_hash(contract: dict) -> str:
    return hashlib.sha256(canonical_json(contract)).hexdigest()


def load_contract(path) -> dict:
    """Bounded JSON read; returns the raw object, not a validated contract."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"phase contract file is unreadable: {path}: {exc}") from None
    if len(raw) > MAX_CONTRACT_BYTES:
        raise ValueError(f"phase contract file exceeds {MAX_CONTRACT_BYTES} bytes: {path}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"phase contract file is not valid JSON: {path}: {exc}") from None
    return data


def contract_view(contract: dict, limit: int = 20) -> dict:
    """Compact bounded projection for status/board output."""
    items = contract.get("acceptanceItems") or []
    return {
        "schemaVersion": contract.get("schemaVersion"),
        "phaseId": contract.get("phaseId"),
        "goal": contract.get("goal"),
        "result": contract.get("result"),
        "baseline": contract.get("baseline"),
        "scope": (contract.get("scope") or [])[:50],
        "designRef": contract.get("designRef"),
        "designSha256": contract.get("designSha256"),
        "budgetSeconds": contract.get("budgetSeconds"),
        "commandTimeoutSeconds": contract.get("commandTimeoutSeconds"),
        "resourceLimits": (contract.get("resourceLimits") or [])[:20],
        "autonomousRepair": (contract.get("autonomousRepair") or [])[:20],
        "escalateWhen": (contract.get("escalateWhen") or [])[:20],
        "nextPhaseRef": contract.get("nextPhaseRef"),
        "acceptanceItems": [{
            "id": item.get("id"), "checkId": item.get("checkId"),
            "description": item.get("description"), "command": item.get("command"),
            "passCondition": item.get("passCondition"), "evidence": item.get("evidence"),
            "minRun": item.get("minRun"), "forbidSkip": item.get("forbidSkip"),
        } for item in items[:limit]],
        "acceptanceItemCount": len(items),
    }
