"""Appeal Writer agent -- Loop 3 (Fight), docs/PLAN.md.

`draft()` turns a wrongful Verdict into a filed-or-pending Appeal, following
the insurer's current appeal strategy (`harness_profiles.appeal_strategy`:
which evidence to lead with, whether to quote a clause verbatim). `file()`
submits it to the simulator and records overturned/upheld -- the signal the
Evolver and the Trust Ladder both learn from.

The heuristic fallback (no LLM key) still includes every piece of evidence
it was given, in the profile's preferred order, because the simulator's own
appeal logic (docs/PLAN.md, "Insurer simulator spec") is a hard check for
specific evidence per insurer -- an appeal that drops evidence to sound more
natural would simply lose more often, offline or not.
"""
from __future__ import annotations

import json
from pathlib import Path

from common import llm
from common.models import VerdictLabel, new_appeal
from firewall.guard import PHILeak
from firewall.render import render_letter

from orchestrator.contracts.sim_client import appeal as sim_appeal
from orchestrator.db import coll, utcnow

MODEL_ID = llm.MODEL_SONNET
_PROMPT = (Path(__file__).parent / "prompts" / "appeal_writer.md").read_text()
_APPEALABLE = {VerdictLabel.WRONGFUL_POLICY.value, VerdictLabel.WRONGFUL_BULK.value}


def draft(verdict: dict, adjudication: dict, tokenized_claim: dict, profile: dict) -> dict:
    if verdict["label"] not in _APPEALABLE:
        raise ValueError(f"verdict label '{verdict['label']}' is not appealable")

    try:
        letter = _draft_with_llm(verdict, adjudication, tokenized_claim, profile)
    except (llm.LLMUnavailable, KeyError, PHILeak):
        # KeyError: the LLM returned valid JSON but without a "letter" key --
        # same fallback as a missing key entirely, not a reason to crash this claim.
        # The heuristic template only ever uses verdict evidence (clause text,
        # comparable ids, pattern stats) and the patient token -- never raw
        # claim/notes fields -- so it's PHI-safe regardless of why the LLM
        # path didn't complete.
        letter = _draft_heuristic(verdict, adjudication, tokenized_claim, profile)

    # Stored tokenized: the real patient name goes back in only in the copy sent to
    # the insurer (file() below), so MongoDB and the live view never hold it.
    appeal = new_appeal(
        claim_id=adjudication["claim_id"],
        insurer=adjudication["insurer"],
        verdict=verdict,
        letter_tokenized=letter,
        mode=profile["permissions"]["appeals"],
    )
    appeal["created_at"] = utcnow()  # not in common.models.Appeal; needed to sort recent appeals
    coll("appeals").insert_one(appeal)
    return appeal


def file(appeal: dict) -> dict:
    """Submit `appeal` to the simulator and record overturned/upheld. Call
    only once the appeal is auto_file, or a human approved a draft_only one."""
    rendered = render_letter(appeal["letter_tokenized"], appeal["claim_id"])  # outside any LLM call
    result = sim_appeal({**appeal, "letter_rendered": rendered})
    recovered = result["paid_amount"] if result["outcome"] == "overturned" else 0.0
    coll("appeals").update_one(
        {"_id": appeal["_id"]},
        {"$set": {"status": "filed", "outcome": result["outcome"], "recovered_usd": recovered, "filed_at": utcnow()}},
    )
    return {**appeal, "status": "filed", "outcome": result["outcome"], "recovered_usd": recovered}


def _clause_lookup(clause_ids: list[str]) -> dict[str, dict]:
    if not clause_ids:
        return {}
    return {c["_id"]: c for c in coll("policies").find({"_id": {"$in": clause_ids}})}


def _draft_with_llm(verdict: dict, adjudication: dict, tokenized_claim: dict, profile: dict) -> str:
    clauses = _clause_lookup(verdict["evidence"].get("clause_ids", []))
    user = json.dumps(
        {
            "insurer": adjudication["insurer"],
            "denial_code": adjudication.get("carc"),
            "denial_text": adjudication.get("denial_text"),
            "verdict_label": verdict["label"],
            "reason": verdict["reason"],
            "patient_token": tokenized_claim.get("patient_token"),
            "evidence": {
                "clause_ids": verdict["evidence"].get("clause_ids", []),
                "clause_texts": {cid: c["clause_text"] for cid, c in clauses.items()},
                "comparable_claim_ids": verdict["evidence"].get("comparable_claim_ids", []),
                "pattern_stats": verdict["evidence"].get("pattern_stats", {}),
            },
            "appeal_strategy": profile["appeal_strategy"],
        }
    )
    result = llm.complete(system=_PROMPT, user=user, model=MODEL_ID, insurer=adjudication["insurer"], claim_id=adjudication["claim_id"])
    return result["letter"]


def _draft_heuristic(verdict: dict, adjudication: dict, tokenized_claim: dict, profile: dict) -> str:
    strategy = profile["appeal_strategy"]
    clauses = _clause_lookup(verdict["evidence"].get("clause_ids", []))
    blocks = {
        "clause": _clause_block(verdict, clauses, strategy["quote_clause_verbatim"]),
        "paid_comparables": _comparables_block(verdict),
        "pattern_stats": _pattern_block(verdict),
    }
    order = [strategy["lead_with"]] + [k for k in blocks if k != strategy["lead_with"]]
    body = "\n\n".join(blocks[k] for k in order if blocks[k])

    patient_token = tokenized_claim.get("patient_token", "UNKNOWN")
    header = f"Re: Appeal for {patient_token}, claim {adjudication['claim_id']}, denial code {adjudication.get('carc')}.\n\n"
    closing = "\n\nWe request this claim be reprocessed and paid at its original billed amount."
    return header + body + closing


def _clause_block(verdict: dict, clauses: dict[str, dict], verbatim: bool) -> str:
    ids = verdict["evidence"].get("clause_ids", [])
    if not ids:
        return ""
    lines = []
    for cid in ids:
        clause = clauses.get(cid)
        if clause is None:
            lines.append(f"Policy clause {cid} covers the denied service.")
        elif verbatim:
            lines.append(f"Clause {clause.get('clause_no')}: \"{clause['clause_text']}\"")
        else:
            lines.append(f"Clause {clause.get('clause_no')} of the current policy covers this service.")
    return "\n".join(lines)


def _comparables_block(verdict: dict) -> str:
    ids = verdict["evidence"].get("comparable_claim_ids", [])
    if not ids:
        return ""
    return f"{len(ids)} comparable claims for the same service were paid without incident: {', '.join(ids)}."


def _pattern_block(verdict: dict) -> str:
    stats = verdict["evidence"].get("pattern_stats", {})
    if not stats:
        return ""
    return (
        f"{stats.get('identical_denials_60s', 0)} identical denials were returned within seconds of "
        f"submission (median latency {stats.get('median_latency_ms', 0)} ms), consistent with an "
        "automated bulk denial rather than an individual clinical review."
    )
