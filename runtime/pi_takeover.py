"""Review-based implementation handoff; never a model launcher or acceptance owner.

The board's exact main-session decisions are the authority. Count distinct
reported rounds within a phase, across contract revisions; ordinary checks,
progress and explicitly external blockers do not consume repair attempts.
"""
from __future__ import annotations

FAILED_DELIVERY_LIMIT = 3
DELIVERY_KINDS = frozenset(("review_required", "phase_blocked"))
FAILURE_KINDS = ("quality", "external")
TAKEOVER_MESSAGE = (
    "Codex takeover required: three reviewed deliveries failed acceptance. "
    "Do not continue Pi or reset the count by changing contract/task identity. "
    "Wait for verified writer release, reassess the complete outcome, design and "
    "evidence, then the existing Codex main session implements and validates. "
    "Resume does not return this work to Pi."
)


def review_policy(card: dict) -> dict:
    """Bounded derived view, including eligible pre-policy phase decisions.

    An accepted delivery resets its phase's streak. A contract edit does not.
    A persisted takeover latch cannot be cleared by refresh/resume or history
    compaction. Missing historical event kinds count only when a phase identity
    proves this was a phase decision, not an arbitrary operational event.
    """
    records = {key: dict(value, eventId=key)
               for key, value in (card.get("handled") or {}).items()
               if isinstance(value, dict)}
    for event in card.get("events") or []:
        if not isinstance(event, dict) or not event.get("handled"):
            continue
        key = event.get("id")
        records[key] = {**records.get(key, {}), **event, "eventId": key,
                        "at": event.get("handledAt") or 0,
                        "eventKind": event.get("kind")}
    phase = (card.get("phase") or {}).get("phaseId")
    key = phase or "task:" + str(card.get("taskId"))
    failed, rounds = [], set()
    for record in sorted(records.values(), key=lambda row: (row.get("at") or 0,
                                                           row.get("eventId") or "")):
        record_key = record.get("phaseId") or "task:" + str(card.get("taskId"))
        if record_key != key:
            continue
        kind = record.get("eventKind")
        if kind not in DELIVERY_KINDS and not (kind is None and record.get("phaseId")):
            continue
        decision = record.get("decision")
        if decision == "accepted":
            failed, rounds = [], set()
            continue
        if decision not in ("rejected", "changes_requested") \
                or record.get("failureKind", "quality") != "quality":
            continue
        number = record.get("round")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1 \
                or number in rounds:
            continue
        rounds.add(number)
        failed.append({"round": number, "eventId": record.get("eventId"),
                       "phaseId": record.get("phaseId"), "at": record.get("at")})
    latch = (card.get("codex") or {}).get("takeover")
    latched = isinstance(latch, dict) and latch.get("required")
    required = bool(latched) or len(failed) >= FAILED_DELIVERY_LIMIT
    if latched:
        key = latch.get("scope") or key
        failed = latch.get("failedReports") or failed
    return {"limit": FAILED_DELIVERY_LIMIT, "scope": key,
            "failedDeliveries": max(len(failed), FAILED_DELIVERY_LIMIT) if latched else len(failed),
            "failedReports": failed[-FAILED_DELIVERY_LIMIT:],
            "takeoverRequired": required, "implementationOwner": "codex" if required else "pi",
            "instruction": TAKEOVER_MESSAGE if required else None}
