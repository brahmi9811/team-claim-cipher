"""Client for Member A's insurer simulator (`POST /submit`, `POST /appeal`).

Always tries the real HTTP endpoint at `SIM_URL` first (default
http://localhost:8001). If the simulator isn't reachable, falls back to a
*local* generator -- deterministic given the claim's own fields (missing
prior auth, excess units, a high-cost imaging line for Payer B, ...) rather
than truly random, so scripts/smoke.py can construct an exact test case and
get a predictable result, and so the loop still has something learnable to
work with when A's simulator isn't running.

The fallback never writes `sim_truth`: only A's simulator records ground truth,
so a fallback run can't pollute the scorer. Every fallback is logged as a
warning, because the numbers it produces are not the real insurers'.
"""
from __future__ import annotations

import json
import logging
import os
import random
import urllib.error
import urllib.request

from common.models import new_id

from orchestrator.db import coll, utcnow

log = logging.getLogger(__name__)

SIM_URL = os.environ.get("SIM_URL", "http://localhost:8001")
_TIMEOUT_SEC = 1.5

_REQUIRES_PRIOR_AUTH = {"G0439"}
_MAX_UNITS_PER_DAY = 4


def _post(path: str, payload: dict) -> dict | None:
    try:
        req = urllib.request.Request(
            f"{SIM_URL}{path}",
            data=json.dumps(payload, default=str).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def submit(claim: dict) -> dict:
    """POST /submit. Falls back to a local, claim-shaped denial generator.

    Response always carries `adjudication_id` (A's real key) and `attempt` --
    both the real simulator and the fallback below set them the same way.
    """
    response = _post("/submit", claim)
    if response is not None:
        return response
    log.warning("simulator at %s unreachable; claim %s adjudicated by the local fallback", SIM_URL, claim.get("_id"))
    return _fallback_submit(claim)


def appeal(appeal_doc: dict) -> dict:
    """POST /appeal. Falls back to a local outcome generator that follows the
    same per-insurer evidence rules documented in docs/PLAN.md."""
    response = _post(
        "/appeal",
        {
            "claim_id": appeal_doc["claim_id"],
            # The rendered letter (real patient name) if the caller made one; never stored.
            "letter": appeal_doc.get("letter_rendered") or appeal_doc["letter_tokenized"],
            "cited_clause_ids": appeal_doc["evidence"].get("clause_ids", []),
            "comparable_claim_ids": appeal_doc["evidence"].get("comparable_claim_ids", []),
            "pattern_stats": appeal_doc["evidence"].get("pattern_stats", {}),
        },
    )
    if response is not None:
        return response
    log.warning("simulator at %s unreachable; appeal for %s decided by the local fallback", SIM_URL, appeal_doc.get("claim_id"))
    return _fallback_appeal(appeal_doc)


def _fallback_submit(claim: dict) -> dict:
    denial = _fallback_denial_reason(claim)
    now = utcnow()
    base = {"claim_id": claim["_id"], "attempt": 1, "adjudicated_at": now, "submitted_at": now}

    if denial is None:
        adjudication_id = new_id("adj")
        return {
            **base,
            "adjudication_id": adjudication_id,
            "status": "paid",
            "carc": None,
            "rarc": None,
            "denial_text": None,
            "paid_amount": claim["total_charge_usd"],
            "latency_ms": random.randint(200, 600),
        }

    carc, rarc, text, _kind, _rule_id, latency = denial
    adjudication_id = new_id("adj")
    return {
        **base,
        "adjudication_id": adjudication_id,
        "status": "denied",
        "carc": carc,
        "rarc": rarc,
        "denial_text": text,
        "paid_amount": 0.0,
        "latency_ms": latency,
    }


def _fallback_denial_reason(claim: dict) -> tuple[str, str, str, str, str, int] | None:
    for line in claim.get("lines", []):
        if line["hcpcs"] in _REQUIRES_PRIOR_AUTH and not claim.get("prior_auth_id"):
            return ("CO-16", "M62", f"Missing prior authorization. Service line {line['line_no']} ({line['hcpcs']}).",
                    "legit", "fallback_legit_prior_auth", random.randint(300, 900))
    for line in claim.get("lines", []):
        if line.get("units", 1) > _MAX_UNITS_PER_DAY:
            return ("CO-151", "N362", f"Units exceed the daily maximum. Service line {line['line_no']} ({line['hcpcs']}).",
                    "legit", "fallback_legit_units", random.randint(300, 900))
    imaging_lines = [ln for ln in claim.get("lines", []) if ln["hcpcs"].startswith("7")]
    if claim["insurer"] == "payer_b" and imaging_lines and claim["total_charge_usd"] > 2000:
        return ("CO-50", "N115", "Not medically necessary",
                "wrongful", "fallback_wrongful_bulk_imaging", random.randint(800, 1500))
    if claim["insurer"] == "payer_a" and any(ln["hcpcs"] == "J9999" for ln in claim.get("lines", [])):
        return ("CO-50", "N115", "Not medically necessary",
                "wrongful", "fallback_wrongful_policy_j9999", random.randint(400, 1000))
    return None


def _fallback_appeal(appeal_doc: dict) -> dict:
    verdict = appeal_doc["verdict"]
    evidence = appeal_doc["evidence"]
    if verdict == "legitimate":
        overturned = False  # a legitimate denial is never overturned (docs/PLAN.md)
    elif verdict == "wrongful_policy":
        overturned = bool(evidence.get("clause_ids"))
    elif verdict == "wrongful_bulk":
        overturned = len(evidence.get("comparable_claim_ids", [])) >= 2 and bool(evidence.get("pattern_stats"))
    else:
        overturned = False

    try:
        claim = coll("claims").find_one({"_id": appeal_doc["claim_id"]})
    except Exception:  # noqa: BLE001 -- offline / no Mongo
        claim = None
    paid_amount = claim["total_charge_usd"] if (overturned and claim) else 0.0
    return {
        "appeal_id": new_id("apl_out"),
        "outcome": "overturned" if overturned else "upheld",
        "paid_amount": paid_amount,
    }
