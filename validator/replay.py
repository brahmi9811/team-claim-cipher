"""Replay a candidate rule over past claims via aggregation pipelines.

Promotion rule (docs/PLAN.md lifecycle table):
  promote when prevented >= 3 AND false_block_rate <= 5% of past paid claims
  otherwise reject
  retire (for already-active rules) when precision < 80% over last 20 uses
"""
from __future__ import annotations

import logging
from typing import Any

from common import db as dbmod
from common.rules import matches, to_match

log = logging.getLogger(__name__)

MIN_PREVENTED = 3
MAX_FALSE_BLOCK_RATE = 0.05
# Fixes that withhold or remove something from a claim. Additive fixes (attach an ID
# from the hospital's records, add a modifier, split units) can't block a claim
# that would have been paid, so paid matches don't count against them.
BLOCKING_ACTIONS = {"hold_for_review", "drop_line"}


def _denied_filter(rule: dict) -> dict[str, Any]:
    """Denials this rule claims to prevent: its target reason codes, when the Judge recorded them."""
    query: dict[str, Any] = {"insurer": rule["insurer"], "status": "denied"}
    if rule.get("target_carc"):
        query["carc"] = rule["target_carc"]
    if rule.get("target_rarc"):
        query["rarc"] = rule["target_rarc"]
    return query


def _blocks(rule: dict) -> bool:
    return (rule.get("fix") or {}).get("action") in BLOCKING_ACTIONS


def replay(rule: dict) -> dict[str, Any]:
    """Run ``rule`` against past claims for ``rule["insurer"]``.

    Prefers a MongoDB aggregation that joins adjudications → claims and
    applies ``to_match(rule)``. Falls back to a client-side loop when Atlas
    isn't reachable (same decision logic).
    """
    try:
        return _replay_aggregation(rule)
    except Exception as exc:  # noqa: BLE001
        log.debug("Aggregation replay failed (%s); using client-side", exc)
        return _replay_client_side(rule)


def _decide(prevented: int, false_blocks: int, n_paid: int) -> dict[str, Any]:
    sample_size = prevented + false_blocks
    false_block_rate = false_blocks / n_paid if n_paid else 0.0
    precision = prevented / sample_size if sample_size else 0.0
    decision = (
        "promote"
        if prevented >= MIN_PREVENTED and false_block_rate <= MAX_FALSE_BLOCK_RATE
        else "reject"
    )
    return {
        "prevented": prevented,
        "false_blocks": false_blocks,
        "precision": round(precision, 4),
        "sample_size": sample_size,
        "false_block_rate": round(false_block_rate, 4),
        "decision": decision,
    }


def _replay_aggregation(rule: dict) -> dict[str, Any]:
    insurer = rule["insurer"]
    db = dbmod.get_db("agent_worker")
    condition_match = to_match(rule)

    # Denied claims whose underlying claim matches the rule condition
    # → would have been prevented.
    denied_pipeline = [
        {"$match": _denied_filter(rule)},
        {
            "$lookup": {
                "from": dbmod.CLAIMS,
                "localField": "claim_id",
                "foreignField": "_id",
                "as": "claim",
            }
        },
        {"$unwind": "$claim"},
        {"$replaceRoot": {"newRoot": "$claim"}},
        {"$match": condition_match} if condition_match else {"$match": {}},
        {"$count": "n"},
    ]
    paid_pipeline = [
        {"$match": {"insurer": insurer, "status": "paid"}},
        {
            "$lookup": {
                "from": dbmod.CLAIMS,
                "localField": "claim_id",
                "foreignField": "_id",
                "as": "claim",
            }
        },
        {"$unwind": "$claim"},
        {"$replaceRoot": {"newRoot": "$claim"}},
        {"$match": condition_match} if condition_match else {"$match": {}},
        {"$count": "n"},
    ]

    prevented_docs = list(db[dbmod.ADJUDICATIONS].aggregate(denied_pipeline))
    false_docs = list(db[dbmod.ADJUDICATIONS].aggregate(paid_pipeline)) if _blocks(rule) else []
    prevented = prevented_docs[0]["n"] if prevented_docs else 0
    false_blocks = false_docs[0]["n"] if false_docs else 0
    n_paid = db[dbmod.ADJUDICATIONS].count_documents(
        {"insurer": insurer, "status": "paid"}
    )
    return _decide(prevented, false_blocks, n_paid)


def _replay_client_side(rule: dict) -> dict[str, Any]:
    insurer = rule["insurer"]
    try:
        db = dbmod.get_db("agent_worker")
        past_denied = list(db[dbmod.ADJUDICATIONS].find(_denied_filter(rule)))
        past_paid = list(
            db[dbmod.ADJUDICATIONS].find({"insurer": insurer, "status": "paid"})
        )
        claim_ids = {a["claim_id"] for a in past_denied} | {
            a["claim_id"] for a in past_paid
        }
        claims_by_id = {
            c["_id"]: c
            for c in db[dbmod.CLAIMS].find({"_id": {"$in": list(claim_ids)}})
        }
    except Exception:
        # Fully offline: no Mongo — nothing to replay against
        return {
            "prevented": 0,
            "false_blocks": 0,
            "precision": 0.0,
            "sample_size": 0,
            "false_block_rate": 0.0,
            "decision": "reject",
        }

    def claim_for(adjudication: dict) -> dict | None:
        return claims_by_id.get(adjudication["claim_id"])

    prevented = sum(
        1
        for a in past_denied
        if (claim := claim_for(a)) is not None and matches(rule, claim)
    )
    false_blocks = sum(
        1
        for a in past_paid
        if _blocks(rule) and (claim := claim_for(a)) is not None and matches(rule, claim)
    )
    return _decide(prevented, false_blocks, len(past_paid))
