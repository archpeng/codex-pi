"""Review-based implementation handoff; never a model launcher or acceptance owner.

The board's exact main-session decisions are the authority. A new task pins its
quality-failure limit when it is created; legacy tasks without a pin keep the
former limit of three. Counting is task-scoped since the last accepted outcome:
one exact negative quality decision per reported round counts, while duplicate
events, contract revisions, phase renames, ordinary checks, progress and
explicitly external blockers do not. A real acceptance starts a fresh count and
resets failures before takeover. A reached takeover remains latched for that
task; a later outcome needs a separate dispatch.
"""
from __future__ import annotations

NEW_TASK_DEFAULT_LIMIT = 1
EXPLICIT_REVIEW_LIMITS = (1, 2)
LEGACY_FAILED_DELIVERY_LIMIT = 3
DELIVERY_KINDS = frozenset(("review_required", "phase_blocked"))
FAILURE_KINDS = ("quality", "external")
TAKEOVER_PREFIX = "Codex takeover required"


def normalize_review_limit(value):
    """A valid explicit dispatch-time quality-failure limit, else ``None``.

    Only the new-task default and the single explicit local-repair choice are
    accepted. The legacy limit is never selectable for a newly created task; it
    is the fallback for tasks that predate the pinned policy.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value not in EXPLICIT_REVIEW_LIMITS:
        return None
    return value


def pinned_review_limit(card) -> dict | None:
    """The immutable task pin copied onto the card at registration, if valid."""
    if not isinstance(card, dict):
        return None
    pin = card.get("reviewPolicyPin")
    if not isinstance(pin, dict):
        return None
    limit = normalize_review_limit(pin.get("qualityFailureLimit"))
    if limit is None:
        return None
    return {"qualityFailureLimit": limit,
            "pinnedAt": pin.get("pinnedAt"),
            "pinnedBy": pin.get("pinnedBy")}


def takeover_message(limit: int, failed: int) -> str:
    """The refusal/ownership instruction for the exact reached limit."""
    noun = "failure" if limit == 1 else "failures"
    recorded = "delivery" if failed == 1 else "deliveries"
    return (
        f"{TAKEOVER_PREFIX}: the pinned review policy allows {limit} reviewed quality {noun} "
        f"for this outcome and {failed} distinct failed {recorded} have been recorded. "
        "Do not continue Pi or reset the count by changing contract/task identity. "
        "Wait for verified writer release, reassess the complete outcome, design and evidence, "
        "then the existing Codex main session implements and validates. "
        "Resume does not return this work to Pi."
    )


def _records(card: dict) -> dict:
    """Merge durable handled decisions and still-present handled events."""
    records = {}
    handled = card.get("handled") if isinstance(card, dict) else None
    for key, value in (handled or {}).items():
        if isinstance(value, dict):
            records[key] = dict(value, eventId=key)
    events = card.get("events") if isinstance(card, dict) else None
    for event in events or []:
        if not isinstance(event, dict) or not event.get("handled"):
            continue
        key = event.get("id")
        records[key] = {**records.get(key, {}), **event, "eventId": key,
                        "at": event.get("handledAt") or 0,
                        "eventKind": event.get("kind")}
    return records


def review_policy(card: dict) -> dict:
    """Bounded derived view of the pinned limit and exact quality failures.

    Counting is chronological and task-scoped. A negative decision counts only
    when it is an exact main decision on a delivery event (``review_required``
    or ``phase_blocked``), is ``rejected``/``changes_requested`` with
    ``failure-kind quality``, and represents a round not already counted. An
    accepted delivery decision resets the count before the limit is reached. A
    persisted takeover latch cannot be cleared by refresh/resume, acceptance,
    contract revisions or phase renames. Missing historical event kinds count
    only when a stored phase identity proves this was a delivery decision, not
    an arbitrary operational event.
    """
    card = card if isinstance(card, dict) else {}
    task_key = "task:" + str(card.get("taskId"))
    codex = card.get("codex") if isinstance(card.get("codex"), dict) else {}
    latch = codex.get("takeover")
    latched = isinstance(latch, dict) and bool(latch.get("required"))
    pin = pinned_review_limit(card)
    limit = pin["qualityFailureLimit"] if pin else None
    limit_source = "task-pin" if pin else None
    if limit is None and "reviewPolicyPin" in card:
        # A malformed pin must never silently turn a new one-delivery task into
        # a three-delivery legacy task. The creation-time pin is authoritative;
        # its loss is conservative until the board can be repaired from it.
        limit = NEW_TASK_DEFAULT_LIMIT
        limit_source = "invalid-task-pin-fail-closed"
    if limit is None and latched:
        limit = normalize_review_limit(latch.get("limit"))
        if limit is not None:
            limit_source = "takeover-latch"
    if limit is None:
        limit = LEGACY_FAILED_DELIVERY_LIMIT
        limit_source = "legacy-default"
    failed, rounds = [], set()
    for record in sorted(_records(card).values(),
                         key=lambda row: (row.get("at") or 0, row.get("eventId") or "")):
        kind = record.get("eventKind")
        if kind not in DELIVERY_KINDS and not (kind is None and record.get("phaseId")):
            continue
        decision = record.get("decision")
        if decision == "accepted":
            # An accepted delivery resets failures before the threshold. A
            # takeover is terminal for this task's Pi allocation even if a
            # later event is injected or a contract/phase is changed.
            failed, rounds = [], set()
            continue
        if decision not in ("rejected", "changes_requested"):
            continue
        if record.get("failureKind", "quality") != "quality":
            continue
        number = record.get("round")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1 \
                or number in rounds:
            continue
        rounds.add(number)
        failed.append({"round": number, "eventId": record.get("eventId"),
                       "phaseId": record.get("phaseId"),
                       "contractHash": record.get("contractHash"),
                       "at": record.get("at")})
    required = bool(latched) or len(failed) >= limit
    if latched and not failed and isinstance(latch.get("failedReports"), list):
        reports = list(latch.get("failedReports"))
    else:
        reports = failed[-limit:]
    outcome = None
    if failed:
        last = failed[-1]
        outcome = {key: last.get(key) for key in ("phaseId", "contractHash", "round",
                                                  "eventId", "at")}
    elif latched and isinstance(latch.get("outcome"), dict):
        outcome = dict(latch["outcome"])
    failed_count = max(len(failed), limit) if latched else len(failed)
    if required:
        reason = (f"pinned quality-failure limit {limit} reached with {failed_count} distinct "
                  f"failed deliver{'y' if failed_count == 1 else 'ies'}")
    else:
        reason = (f"{failed_count} of {limit} pinned reviewed quality failure allowance used; "
                  "implementation stays with Pi")
    return {
        "schemaVersion": 1,
        "limit": limit,
        "limitSource": limit_source,
        "scope": task_key,
        "outcome": outcome,
        "failedDeliveries": failed_count,
        "failedReports": reports,
        "takeoverRequired": required,
        "implementationOwner": "codex" if required else "pi",
        "reason": reason,
        "instruction": takeover_message(limit, failed_count) if required else None,
    }
