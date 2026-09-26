"""Rule lifecycle: candidate → shadow → active/rejected, and active → retired.

Logs every transition to ``harness_events``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from common import db as dbmod
from common.models import EventActor, EventType, RuleStatus, new_event, now_iso
from validator.replay import replay

log = logging.getLogger(__name__)

RULE_MIN_PRECISION = 0.80
RULE_OUTCOME_WINDOW = 20
RULE_IDLE_RETIRE_MINUTES = 30


def _as_datetime(value: Any) -> datetime | None:
    """Timestamps arrive as BSON dates (the orchestrator) or ISO strings (common.models)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _event(**kwargs: Any) -> dict[str, Any]:
    """new_event() with a real date in `ts`, like every other harness_events writer,
    so the live view's time ordering isn't split between strings and dates."""
    event = new_event(**kwargs)
    event["ts"] = datetime.now(timezone.utc)
    return event


def _rule_summary(rule: dict) -> dict[str, Any]:
    return {"condition": rule.get("condition"), "fix": rule.get("fix")}


def advance(rule: dict) -> dict[str, Any]:
    """Move a candidate/shadow rule through replay → active or rejected.

    Returns the ``harness_events`` document that was written.
    """
    stats = replay(rule)
    before = {"status": rule.get("status"), "replay": rule.get("replay")}

    if stats["decision"] == "promote":
        new_status = RuleStatus.ACTIVE.value
        event_type = EventType.RULE_PROMOTED.value
        reason = (
            f"Replay promoted rule: prevented={stats['prevented']}, "
            f"false_block_rate={stats['false_block_rate']}"
        )
    else:
        new_status = RuleStatus.REJECTED.value
        event_type = EventType.RULE_REJECTED.value
        reason = (
            f"Replay rejected rule: prevented={stats['prevented']}, "
            f"false_block_rate={stats['false_block_rate']}"
        )

    after = {
        "status": new_status,
        "rule": _rule_summary(rule),  # so the live view can show which rule went live
        "replay": {
            "prevented": stats["prevented"],
            "false_blocks": stats["false_blocks"],
            "precision": stats["precision"],
        },
    }
    event = _event(
        insurer=rule["insurer"],
        actor=EventActor.VALIDATOR.value,
        type_=event_type,
        reason=reason,
        before=before,
        after=after,
        evidence_ids=rule.get("evidence_ids", []),
    )

    try:
        db = dbmod.get_db("agent_worker")
        db[dbmod.RULES].update_one(
            {"_id": rule["_id"]},
            {
                "$set": {
                    "status": new_status,
                    "replay": after["replay"],
                }
            },
            upsert=False,
        )
        # If the rule was only in-memory (candidate just proposed), upsert
        if db[dbmod.RULES].find_one({"_id": rule["_id"]}) is None:
            saved = dict(rule)
            saved["status"] = new_status
            saved["replay"] = after["replay"]
            db[dbmod.RULES].insert_one(saved)
        db[dbmod.HARNESS_EVENTS].insert_one(event)
    except Exception as exc:  # noqa: BLE001
        log.warning("lifecycle persist failed: %s", exc)

    # Mutate caller's copy so orchestrator sees the new status
    rule["status"] = new_status
    rule["replay"] = after["replay"]
    return event


def retire_stale(insurer: str | None = None) -> list[dict[str, Any]]:
    """Retire active rules with precision < 80% over last 20 uses, or idle 30 min.

    Returns the list of retirement events written.
    """
    query: dict[str, Any] = {"status": RuleStatus.ACTIVE.value}
    if insurer:
        query["insurer"] = insurer

    events: list[dict[str, Any]] = []
    try:
        db = dbmod.get_db("agent_worker")
        rules = list(db[dbmod.RULES].find(query))
    except Exception as exc:  # noqa: BLE001
        log.debug("retire_stale skipped (%s)", exc)
        return events

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RULE_IDLE_RETIRE_MINUTES)

    for rule in rules:
        reason = _retirement_reason(rule, cutoff)
        if not reason:
            continue
        before = {"status": rule["status"]}
        event = _event(
            insurer=rule["insurer"],
            actor=EventActor.VALIDATOR.value,
            type_=EventType.RULE_RETIRED.value,
            reason=reason,
            before=before,
            after={"status": RuleStatus.RETIRED.value, "rule": _rule_summary(rule)},
            evidence_ids=rule.get("evidence_ids", []),
        )
        try:
            db[dbmod.RULES].update_one(
                {"_id": rule["_id"]},
                {"$set": {"status": RuleStatus.RETIRED.value}},
            )
            db[dbmod.HARNESS_EVENTS].insert_one(event)
            events.append(event)
        except Exception as exc:  # noqa: BLE001
            log.warning("retire failed for %s: %s", rule.get("_id"), exc)

    return events


def _retirement_reason(rule: dict, cutoff: datetime) -> str | None:
    outcomes = rule.get("recent_outcomes") or []
    if len(outcomes) >= RULE_OUTCOME_WINDOW:
        window = outcomes[-RULE_OUTCOME_WINDOW:]
        precision = sum(1 for x in window if x) / len(window)
        if precision < RULE_MIN_PRECISION:
            return (
                f"Precision {precision:.2f} below {RULE_MIN_PRECISION} "
                f"over last {RULE_OUTCOME_WINDOW} uses"
            )

    used_at = _as_datetime(rule.get("last_used_at"))
    if used_at is not None:
        if used_at < cutoff:
            return f"Unused since {used_at:%H:%M} UTC (>{RULE_IDLE_RETIRE_MINUTES} min)"
    elif rule.get("hit_count", 0) == 0:
        created = _as_datetime(rule.get("created_at"))
        if created is not None and created < cutoff:
            return f"Never used and older than {RULE_IDLE_RETIRE_MINUTES} min"
    return None


def propose_candidate(rule: dict) -> dict[str, Any]:
    """Insert a candidate rule and emit ``rule_proposed``. Then run ``advance``."""
    rule = dict(rule)
    rule.setdefault("status", RuleStatus.CANDIDATE.value)
    rule.setdefault("created_at", now_iso())
    event = _event(
        insurer=rule["insurer"],
        actor=EventActor.JUDGE.value,
        type_=EventType.RULE_PROPOSED.value,
        reason="Candidate rule proposed from denial cluster",
        after={"rule_id": rule["_id"], "condition": rule.get("condition")},
        evidence_ids=rule.get("evidence_ids", []),
    )
    try:
        db = dbmod.get_db("agent_worker")
        db[dbmod.RULES].replace_one({"_id": rule["_id"]}, rule, upsert=True)
        db[dbmod.HARNESS_EVENTS].insert_one(event)
    except Exception as exc:  # noqa: BLE001
        log.warning("propose_candidate persist failed: %s", exc)

    # Shadow is implicit: replay runs immediately
    rule["status"] = RuleStatus.SHADOW.value
    return advance(rule)
