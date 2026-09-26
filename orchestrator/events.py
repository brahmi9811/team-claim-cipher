"""The `harness_events` writer -- Member C's own spec (docs/PLAN.md: "Event
document"), not a stand-in for anyone else's contract. Every agent decision
that changes state (a rule proposed/promoted/rejected/retired, a profile or
permission change, a rollback, a blocked PHI leak) is logged through this
one function, so D's "harness changes" panel and "Why?" drawer have exactly
one place to read from, and nothing writes `harness_events` by hand.
"""
from __future__ import annotations

from common.models import new_event

from orchestrator.db import coll, utcnow


def emit(
    *,
    insurer: str,
    actor: str,
    type_: str,
    reason: str,
    before: dict | None = None,
    after: dict | None = None,
    evidence_ids: list[str] | None = None,
) -> dict:
    event = new_event(insurer=insurer, actor=actor, type_=type_, reason=reason, before=before, after=after, evidence_ids=evidence_ids)
    event["ts"] = utcnow()  # common.models.new_event() stamps an ISO string; store a real date instead
    coll("harness_events").insert_one(event)
    return event


def mark_reverted(event_id: str) -> None:
    coll("harness_events").update_one({"_id": event_id}, {"$set": {"reverted": True}})
