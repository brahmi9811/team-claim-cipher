"""Trust Ladder -- docs/PLAN.md, Member C specs: "permissions: Never
directly. Only the Trust Ladder changes it."

This is deliberately its own module, separate from evolver.py, so the one
piece of the harness that can escalate an insurer from draft-only to
auto-filing appeals is governed by a fixed, simple rule the Evolver's
LLM-driven proposals can never touch or bypass:

    draft_only -> auto_file   after 10 consecutive correct wrongful calls
                              (a wrongful_* verdict whose appeal was overturned)
    auto_file  -> draft_only  after 2 consecutive upheld appeals
                              (the agent auto-filed and was wrong, twice in a row)
"""
from __future__ import annotations

import copy
import logging

from common import profiles
from common.models import AppealMode, EventType

from orchestrator.db import coll, utcnow
from orchestrator.events import emit

log = logging.getLogger(__name__)

PROMOTE_AFTER_STREAK = 10
DEMOTE_AFTER_STREAK = 2


def update(insurer: str) -> dict | None:
    """Check this insurer's most recent filed appeals and promote/demote
    appeal-filing permission if the streak rule fires. Returns the
    `permission_changed` Event, or None if nothing changed."""
    filed = [
        # By filing time, not drafting time: a draft a human approves later is filed out of order.
        a for a in coll("appeals").find({"insurer": insurer, "outcome": {"$ne": None}}).sort("filed_at", -1)
        if a.get("outcome")
    ]
    profile = profiles.get_profile(insurer)
    current = profile["permissions"]["appeals"]

    if current == AppealMode.DRAFT_ONLY.value:
        streak = _leading_streak(filed, lambda a: a["verdict"] != "legitimate" and a["outcome"] == "overturned")
        if streak >= PROMOTE_AFTER_STREAK:
            return _change_permission(insurer, profile, AppealMode.AUTO_FILE.value,
                                       f"{streak} consecutive correct wrongful calls (appeal overturned); earning auto-file.")
    elif current == AppealMode.AUTO_FILE.value:
        streak = _leading_streak(filed, lambda a: a["outcome"] == "upheld")
        if streak >= DEMOTE_AFTER_STREAK:
            return _change_permission(insurer, profile, AppealMode.DRAFT_ONLY.value,
                                       f"{streak} consecutive upheld appeals while auto-filing; returning to draft-only.")
    return None


def _leading_streak(filed_desc: list[dict], predicate) -> int:
    """Count how many of the most recent filed appeals satisfy `predicate`,
    stopping at the first one that doesn't (a true "current streak")."""
    streak = 0
    for appeal in filed_desc:
        if not predicate(appeal):
            break
        streak += 1
    return streak


def _change_permission(insurer: str, profile: dict, new_mode: str, reason: str) -> dict:
    before = copy.deepcopy(profile)
    profile["permissions"] = {"appeals": new_mode, "earned_at": utcnow()}
    profile["version"] += 1
    profile["updated_by"] = "trust_ladder"
    event = emit(insurer=insurer, actor="evolver", type_=EventType.PERMISSION_CHANGED.value, reason=reason, before=before, after=profile)
    try:
        profiles.save_profile(profile, event)
    except Exception:  # noqa: BLE001 -- don't crash the loop over a Mongo hiccup
        log.exception("failed to save harness_profiles for %s", insurer)
    return event
