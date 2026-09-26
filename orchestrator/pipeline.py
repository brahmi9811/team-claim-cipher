"""One claim's journey through the loop (docs/PLAN.md, "The solution"):

    tokenize -> scrub (Learn) -> submit -> [denied] -> judge (Judge)
        -> legitimate: advance the suggested rule through the lifecycle
        -> wrongful_*: draft an appeal, file it if auto_file (Fight)

Rule lifecycle transitions (candidate -> active/rejected, active -> retired)
are B's `validator.lifecycle` module -- this file just calls it at the right
points and keeps the per-rule outcome bookkeeping (`recent_outcomes`,
`last_used_at`) that `retire_stale` reads.
"""
from __future__ import annotations

from common import profiles
from common.models import AppealMode, VerdictLabel
from firewall.tokenize import tokenize
from validator import lifecycle

from agents import appeal_writer, judge, scrubber, trust_ladder
from orchestrator.contracts import sim_client
from orchestrator.db import as_datetime, claims_coll, coll, utcnow

RULE_OUTCOME_WINDOW = 20
_WRONGFUL_LABELS = {VerdictLabel.WRONGFUL_POLICY.value, VerdictLabel.WRONGFUL_BULK.value}


def process_claim(claim: dict) -> dict:
    """Run one claim through Learn -> submit -> Judge -> Fight. Returns a
    summary dict; never raises for an ordinary business outcome (a denial,
    an upheld appeal) -- only for a programming error."""
    # Forge-loaded claims are already stored (encrypted); only a freshly generated
    # demo claim needs writing, and it goes through the firewall so its patient
    # fields are encrypted too.
    if claim.get("source") == "demo":
        claims_coll().replace_one({"_id": claim["_id"]}, claim, upsert=True)
    insurer = claim["insurer"]
    profile = profiles.get_profile(insurer)

    tokenized = tokenize(claim, profile=profile)
    scrub_result = scrubber.scrub(tokenized)
    summary = {
        "claim_id": claim["_id"],
        "insurer": insurer,
        "applied_rule_ids": scrub_result["applied_rule_ids"],
        "scrub_summary": scrub_result["summary"],
    }

    if scrub_result["hold_for_review"]:
        summary["status"] = "held_for_review"
        return summary

    submitted_at = utcnow()
    response = sim_client.submit(scrub_result["claim"])
    adjudication = _record_adjudication(claim, response, submitted_at)
    summary["status"] = adjudication["status"]

    for rule_id in scrub_result["applied_rule_ids"]:
        _record_rule_outcome(rule_id, success=(adjudication["status"] == "paid"))

    if adjudication["status"] != "denied":
        return summary

    verdict = judge.classify(adjudication, scrub_result["claim"], profile)
    summary["verdict"] = verdict["label"]

    if verdict["label"] == VerdictLabel.LEGITIMATE.value and verdict["suggested_rule"] is not None:
        lifecycle_event = lifecycle.advance(verdict["suggested_rule"])
        summary["rule_lifecycle_event"] = lifecycle_event["type"]
    elif verdict["label"] in _WRONGFUL_LABELS:
        appeal = appeal_writer.draft(verdict, adjudication, scrub_result["claim"], profile)
        summary["appeal_id"] = appeal["_id"]
        summary["appeal_mode"] = appeal["mode"]
        if appeal["mode"] == AppealMode.AUTO_FILE.value:
            filed = appeal_writer.file(appeal)
            summary["appeal_outcome"] = filed["outcome"]
            summary["recovered_usd"] = filed["recovered_usd"]
            trust_ladder.update(insurer)

    return summary


def _record_adjudication(claim: dict, response: dict, submitted_at) -> dict:
    adjudication = {
        "_id": response["adjudication_id"],  # A's real key; the fallback sim sets it the same way
        "claim_id": response["claim_id"],
        "insurer": claim["insurer"],
        "attempt": response.get("attempt", 1),
        "status": response["status"],
        "carc": response.get("carc"),
        "rarc": response.get("rarc"),
        "denial_text": response.get("denial_text"),
        "paid_amount_usd": response.get("paid_amount", 0.0),
        "submitted_at": as_datetime(response.get("submitted_at") or submitted_at),
        "adjudicated_at": as_datetime(response["adjudicated_at"]),
        "latency_ms": response.get("latency_ms", 0),
        "verdict": None,
        "holdout": claim.get("holdout", False),
    }
    coll("adjudications").insert_one(adjudication)
    return adjudication


def _record_rule_outcome(rule_id: str, *, success: bool) -> None:
    rule = coll("rules").find_one({"_id": rule_id})
    if rule is None:
        return
    outcomes = (rule.get("recent_outcomes") or []) + [success]
    outcomes = outcomes[-RULE_OUTCOME_WINDOW:]
    coll("rules").update_one({"_id": rule_id}, {"$set": {"recent_outcomes": outcomes, "last_used_at": utcnow()}})
