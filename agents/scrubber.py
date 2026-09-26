"""Scrubber agent -- Loop 1 (Learn), docs/PLAN.md.

Applies every *active* prevention rule for an insurer to a tokenized claim
before it's submitted. The fix itself is deterministic and LLM-free by
design (`common.rules.apply_fix` is a pure function over a small, whitelisted
set of actions) -- there is nothing for a model to decide, and an LLM
"maybe" fixing a claim is a liability, not a feature, when the fix can be
computed exactly. Haiku 4.5 is used only for the cheap, non-critical part:
writing the one-line audit summary a human reviewer sees, with a
deterministic fallback so a missing LLM key never blocks a claim.
"""
from __future__ import annotations

from pathlib import Path

from common import llm
from common.models import RuleStatus
from common.rules import apply_fix, matches
from firewall.guard import PHILeak

from orchestrator.db import coll, utcnow

MODEL = llm.MODEL_HAIKU
_PROMPT = (Path(__file__).parent / "prompts" / "scrubber.md").read_text()


def active_rules(insurer: str) -> list[dict]:
    return list(coll("rules").find({"insurer": insurer, "status": RuleStatus.ACTIVE.value}))


def scrub(claim: dict) -> dict:
    """Apply every active rule for `claim["insurer"]`.

    Returns {"claim": <possibly fixed claim>, "applied_rule_ids": [...],
    "hold_for_review": bool, "summary": str}. Never changes diagnosis codes
    or raises a charge -- `apply_fix` enforces that, not this function.
    """
    fixed = claim
    applied: list[str] = []
    hold = False
    for rule in active_rules(claim["insurer"]):
        if not matches(rule, fixed):
            continue
        applied.append(rule["_id"])
        coll("rules").update_one({"_id": rule["_id"]}, {"$inc": {"hit_count": 1}, "$set": {"last_used_at": utcnow()}})
        if rule["fix"]["action"] == "hold_for_review":
            hold = True
            continue
        fixed = apply_fix(rule, fixed)

    summary = _summarize(claim["_id"], claim["insurer"], applied)
    return {"claim": fixed, "applied_rule_ids": applied, "hold_for_review": hold, "summary": summary}


def _summarize(claim_id: str, insurer: str, applied_rule_ids: list[str]) -> str:
    if not applied_rule_ids:
        return "No active rule matched; submitted as received."
    try:
        result = llm.complete(
            system=_PROMPT,
            user=f"claim_id={claim_id}\nmatched_rule_ids={applied_rule_ids}",
            model=MODEL,
            insurer=insurer,
            claim_id=claim_id,
        )
        return result["summary"]
    except (llm.LLMUnavailable, KeyError, PHILeak):
        return f"Applied {len(applied_rule_ids)} active rule(s): {', '.join(applied_rule_ids)}."
