"""Judge agent -- Loop 2 (Judge) and half of Loop 1 (Learn), docs/PLAN.md.

`classify()` decides whether a denial is legitimate, wrongful (policy or
bulk), or needs a human, using only signals a real billing team would have:
the denial code/text, the insurer's own published policy (via
`search.vector_search`), similar claims that WERE paid, and bulk-denial
timing statistics -- never the simulator's hidden rules or ground truth.

When it calls a denial `legitimate`, it also drives Loop 1: once three or
more denials with the same reason code have been called legitimate for an
insurer, it proposes a candidate prevention rule from the pattern, in
Member B's frozen `common/rules.py` condition/fix format, and logs the
proposal to `harness_events` for D's live view.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from common import llm, search
from common.models import EventType, RuleStatus, VerdictLabel, new_rule, new_verdict
from firewall.guard import PHILeak

from orchestrator.db import as_datetime, coll, utcnow
from orchestrator.events import emit

MODEL_ID = llm.MODEL_SONNET
MIN_CLUSTER_SIZE = 3
REPROPOSE_AFTER = 5  # new matching denials needed before a rejected rule is proposed again
BULK_MAX_MEDIAN_LATENCY_MS = 2000
_KNOWN_LEGIT_CARCS = {"CO-4", "CO-16", "CO-151", "CO-18", "CO-29", "CO-97"}
# The denial text names the offending line: "Service line 2 (G0444 Depression screening)."
_SERVICE_LINE_RE = re.compile(r"Service line (\d+) \((\w+)")
# Standard modifier a billing team appends for a CO-4 / N822 "missing modifier" denial.
_MODIFIER_FOR_CODE = {"A0429": "RH"}
_DEFAULT_MODIFIER = "25"
_SPLIT_MAX_UNITS = 4
_PROMPT = (Path(__file__).parent / "prompts" / "judge.md").read_text()


def classify(adjudication: dict, claim: dict, profile: dict) -> dict:
    """Return a Verdict (docs/PLAN.md, Member C specs) and persist it onto
    the adjudication. Only ever called for a denied adjudication."""
    if adjudication["status"] != "denied":
        raise ValueError("classify() only applies to denied adjudications")

    insurer = claim["insurer"]
    ctx = profile["context_policy"]
    query_text = f"{adjudication.get('denial_text', '')} {adjudication.get('carc', '')}"

    policy_hits = search.vector_search(
        "policies", query_text, {"insurer": insurer, "current": True}, top_k=ctx["policy_clauses"]
    )
    comparable = (
        search.vector_search("adjudications", query_text, {"insurer": insurer, "status": "paid"}, top_k=ctx["paid_comparables"])
        if ctx["paid_comparables"]
        else []
    )
    pattern_stats = (
        _bulk_pattern_stats(insurer, adjudication.get("carc"), profile["judge"]["bulk_window_sec"])
        if ctx["include_pattern_stats"]
        else {}
    )

    try:
        verdict = _classify_with_llm(adjudication, claim, policy_hits, comparable, pattern_stats)
    except (llm.LLMUnavailable, KeyError, TypeError, ValueError):
        # KeyError/TypeError/ValueError: the LLM returned valid JSON but not the
        # expected shape (a missing field, or a `confidence` that isn't a number) --
        # same fallback as a missing key, not something to let crash this claim.
        verdict = _classify_heuristic(adjudication, claim, policy_hits, comparable, pattern_stats, profile)
    except PHILeak as exc:
        # A hit here means real PHI (or something that looks like it) reached
        # or came back from the LLM. Don't fall back to the heuristic and
        # quietly carry on -- route to a human instead of risking a rule
        # proposal or appeal built from a leaking claim.
        verdict = new_verdict(
            label=VerdictLabel.NEEDS_REVIEW.value,
            confidence=0.0,
            reason=f"Blocked: PHI leak detected ({exc.pattern}) while classifying this denial; routed to human review.",
            evidence={"clause_ids": [], "comparable_claim_ids": [], "pattern_stats": {}},
            model="blocked-phi-leak",
        )

    if verdict["label"] in (VerdictLabel.WRONGFUL_POLICY.value, VerdictLabel.WRONGFUL_BULK.value):
        # Evidence the Judge computed itself goes into the appeal whatever the LLM echoed back:
        # insurers check for it (e.g. Payer B needs the bulk pattern stats and paid comparables).
        evidence = verdict.setdefault("evidence", {})
        if pattern_stats and not evidence.get("pattern_stats"):
            evidence["pattern_stats"] = pattern_stats
        if comparable and not evidence.get("comparable_claim_ids"):
            evidence["comparable_claim_ids"] = [c.get("claim_id", c["_id"]) for c in comparable]

    if verdict["label"] != VerdictLabel.NEEDS_REVIEW.value and verdict["confidence"] < profile["judge"]["min_confidence"]:
        verdict["label"] = VerdictLabel.NEEDS_REVIEW.value

    verdict["suggested_rule"] = (
        _propose_rule_if_clustered(adjudication, claim) if verdict["label"] == VerdictLabel.LEGITIMATE.value else None
    )
    coll("adjudications").update_one({"_id": adjudication["_id"]}, {"$set": {"verdict": verdict}})
    return verdict


def _bulk_pattern_stats(insurer: str, carc: str | None, window_sec: int) -> dict:
    if not carc:
        return {"identical_denials_60s": 0, "median_latency_ms": 0}
    recent = coll("adjudications").find({"insurer": insurer, "status": "denied", "carc": carc})
    now = utcnow()
    within_window = [a for a in recent if (now - as_datetime(a["adjudicated_at"])).total_seconds() <= window_sec]
    latencies = sorted(a.get("latency_ms", 0) for a in within_window) or [0]
    return {"identical_denials_60s": len(within_window), "median_latency_ms": latencies[len(latencies) // 2]}


def _classify_with_llm(adjudication: dict, claim: dict, policy_hits: list[dict], comparable: list[dict], pattern_stats: dict) -> dict:
    user = json.dumps(
        {
            "denial_code": adjudication.get("carc"),
            "denial_text": adjudication.get("denial_text"),
            "claim_summary": {
                "insurer": claim["insurer"],
                "diagnosis_codes": claim["diagnosis_codes"],
                "lines": claim["lines"],
                "prior_auth_id": claim.get("prior_auth_id"),
                # Billing notes as the firewall redacted them; the leak detector checks this
                # prompt before any call and blocks it if a patient identifier slipped through.
                "notes": claim.get("notes_redacted"),
            },
            "policy_clauses": [{"id": p["_id"], "clause_no": p.get("clause_no"), "text": p.get("clause_text")} for p in policy_hits],
            "comparable_paid_claims": [c.get("claim_id", c["_id"]) for c in comparable],
            "pattern_stats": pattern_stats,
        }
    )
    result = llm.complete(system=_PROMPT, user=user, model=MODEL_ID, insurer=claim["insurer"], claim_id=claim["_id"])
    return new_verdict(
        label=result["label"],
        confidence=float(result["confidence"]),
        reason=result["reason"],
        evidence=result.get("evidence", {"clause_ids": [], "comparable_claim_ids": [], "pattern_stats": {}}),
        model="sonnet-5",
    )


def _classify_heuristic(adjudication: dict, claim: dict, policy_hits: list[dict], comparable: list[dict], pattern_stats: dict, profile: dict) -> dict:
    """Used whenever OPENROUTER_API_KEY isn't set. Deterministic, so the
    same denial always gets the same verdict offline."""
    carc = adjudication.get("carc")
    rarc = adjudication.get("rarc")
    identical = pattern_stats.get("identical_denials_60s", 0)
    bulk_min = profile["judge"]["bulk_min_identical"]
    comparable_ids = [c.get("claim_id", c["_id"]) for c in comparable]
    missing_field = {"M62": "prior_auth_id", "N286": "referring_provider_id"}.get(rarc) if carc == "CO-16" else None

    # Bulk = many identical denials in the window AND decided seconds after submission
    # (PLAN.md, Loop 2: "identical denials in the last 60 seconds, time from submission
    # to denial"). Checked first: it is the more specific signal. Both the window's median
    # and this denial must be fast, which keeps slow, ordinary denials of the same code out.
    fast = BULK_MAX_MEDIAN_LATENCY_MS
    if (identical >= bulk_min and pattern_stats.get("median_latency_ms", fast) < fast
            and adjudication.get("latency_ms", fast) < fast):
        # Both signals agree, so start above the usual 0.75 bar (the old 0.6 start was
        # tuned for a threshold of 20 and fell under it).
        confidence = round(min(0.97, 0.8 + 0.02 * (identical - bulk_min)), 2)
        latency = adjudication.get("latency_ms", 0) / 1000
        clause = _best_clause(claim, policy_hits)
        return new_verdict(
            label=VerdictLabel.WRONGFUL_BULK.value,
            confidence=confidence,
            reason=f"{identical} identical '{carc}' denials within the bulk-detection window, this one {latency:.1f} s after submission.",
            evidence={"clause_ids": [clause["_id"]] if clause else [], "comparable_claim_ids": comparable_ids, "pattern_stats": pattern_stats},
            model="heuristic-fallback",
        )

    # "Missing" information that is actually on the claim: the insurer's own reason is wrong.
    if missing_field and claim.get(missing_field):
        clause = _best_clause(claim, policy_hits, prefer_words=("authorization",) if missing_field == "prior_auth_id" else ("referring",))
        return new_verdict(
            label=VerdictLabel.WRONGFUL_POLICY.value,
            confidence=0.85,
            reason=f"Denied as missing '{missing_field}', but the claim carries one.",
            evidence={"clause_ids": [clause["_id"]] if clause else [], "comparable_claim_ids": comparable_ids, "pattern_stats": pattern_stats},
            model="heuristic-fallback",
        )
    if carc in _KNOWN_LEGIT_CARCS:
        return new_verdict(
            label=VerdictLabel.LEGITIMATE.value,
            confidence=0.9,
            reason=f"'{carc}' matches a known fixable billing problem, not a coverage dispute.",
            evidence={"clause_ids": [], "comparable_claim_ids": [], "pattern_stats": {}},
            model="heuristic-fallback",
        )
    top = _best_clause(claim, policy_hits) if carc == "CO-50" else None
    if top:
        return new_verdict(
            label=VerdictLabel.WRONGFUL_POLICY.value,
            confidence=0.8,
            reason=f"Denied as 'not medically necessary', but clause {top.get('clause_no')} of the current policy covers this service.",
            evidence={"clause_ids": [top["_id"]], "comparable_claim_ids": comparable_ids, "pattern_stats": pattern_stats},
            model="heuristic-fallback",
        )
    return new_verdict(
        label=VerdictLabel.NEEDS_REVIEW.value,
        confidence=0.4,
        reason="No clause, comparable claim, or bulk pattern clearly explains this denial.",
        evidence={"clause_ids": [], "comparable_claim_ids": [], "pattern_stats": {}},
        model="heuristic-fallback",
    )


def _best_clause(claim: dict, policy_hits: list[dict], prefer_words: tuple[str, ...] = ()) -> dict | None:
    """The current clause that names the most of this claim's codes (HCPCS and ICD-10),
    read from the insurer's published policy -- what a billing team checks by hand."""
    codes = {ln["hcpcs"] for ln in claim.get("lines", [])} | set(claim.get("diagnosis_codes", []))
    try:
        clauses = list(coll("policies").find({"insurer": claim["insurer"], "current": True}, {"embedding": 0}))
    except Exception:  # noqa: BLE001 -- offline: rank what vector search returned
        clauses = []
    clauses = clauses or list(policy_hits)

    def score(clause: dict) -> tuple[int, int]:
        text = clause.get("clause_text") or ""
        return (sum(1 for c in codes if c in text), sum(1 for w in prefer_words if w in text.lower()))

    ranked = sorted(clauses, key=score, reverse=True)
    if ranked and score(ranked[0]) > (0, 0):
        return ranked[0]
    return policy_hits[0] if policy_hits else None


def _propose_rule_if_clustered(adjudication: dict, claim: dict) -> dict | None:
    insurer = claim["insurer"]
    carc = adjudication.get("carc")
    if not carc:
        return None

    rarc = adjudication.get("rarc")
    cluster = list(coll("adjudications").find({"insurer": insurer, "carc": carc, "rarc": rarc, "verdict.label": VerdictLabel.LEGITIMATE.value}))
    cluster_size = len(cluster) + 1  # +1: this adjudication's verdict isn't saved yet
    if cluster_size < MIN_CLUSTER_SIZE:
        return None

    suggestion = _suggest_condition_fix(adjudication, claim)
    if suggestion is None:
        return None
    condition, fix = suggestion

    same = [r for r in coll("rules").find({"insurer": insurer}) if r["condition"] == condition and r["fix"] == fix]
    if any(r["status"] != RuleStatus.REJECTED.value for r in same):
        return None  # already proposed (or promoted); don't spam duplicates
    rejected_at = max((len(r.get("evidence_ids") or []) for r in same), default=None)
    if rejected_at is not None and cluster_size < rejected_at + REPROPOSE_AFTER:
        return None  # replay rejected this rule; wait for new evidence instead of re-proposing every denial

    evidence_ids = [a["_id"] for a in cluster] + [adjudication["_id"]]
    rule = new_rule(insurer=insurer, condition=condition, fix=fix, evidence_ids=evidence_ids, created_by="judge")
    rule["target_carc"], rule["target_rarc"] = carc, rarc  # validator.replay counts only these denials as prevented
    coll("rules").insert_one(rule)
    emit(
        insurer=insurer,
        actor="judge",
        type_=EventType.RULE_PROPOSED.value,
        reason=f"{cluster_size} denials with reason '{carc}' called legitimate; proposing '{fix['action']}'.",
        after=rule,
        evidence_ids=evidence_ids,
    )
    return rule


def _suggest_condition_fix(adjudication: dict, claim: dict) -> tuple[dict, dict] | None:
    """Turn a denial (CARC + RARC + the service line its text names) into a Rule
    condition/fix pair in B's frozen format (docs/PLAN.md example: G0439 +
    missing prior auth -> attach_prior_auth). Returns None for denials no
    claim edit can prevent (late filing, duplicates), so nothing gets held."""
    carc, rarc = adjudication.get("carc"), adjudication.get("rarc")
    lines = claim.get("lines", [])
    if not lines:
        return None
    match = _SERVICE_LINE_RE.search(adjudication.get("denial_text") or "")
    hcpcs = match.group(2) if match else lines[0]["hcpcs"]
    code_cond = {"field": "lines.hcpcs", "op": "eq", "value": hcpcs}
    others = sorted({ln["hcpcs"] for ln in lines} - {hcpcs})

    if carc == "CO-16" and rarc == "M62" and not claim.get("prior_auth_id"):
        return {"all": [code_cond, {"field": "prior_auth_id", "op": "missing"}]}, {"action": "attach_prior_auth"}
    if carc == "CO-16" and rarc == "N286" and not claim.get("referring_provider_id"):
        return {"all": [code_cond, {"field": "referring_provider_id", "op": "missing"}]}, {"action": "attach_referring_provider"}
    if carc == "CO-151":
        return (
            {"all": [code_cond, {"field": "lines.units", "op": "gt", "value": _SPLIT_MAX_UNITS}]},
            {"action": "split_units", "params": {"max_units": _SPLIT_MAX_UNITS}},
        )
    if carc == "CO-4":
        modifier = _MODIFIER_FOR_CODE.get(hcpcs, _DEFAULT_MODIFIER)
        conds = [code_cond] + ([{"field": "lines.hcpcs", "op": "in", "value": others}] if others else [])
        return {"all": conds}, {"action": "add_modifier", "params": {"modifier": modifier}}
    if carc == "CO-97" and others:
        return (
            {"all": [code_cond, {"field": "lines.hcpcs", "op": "in", "value": others}]},
            {"action": "drop_line", "params": {"hcpcs": hcpcs}},
        )
    return None
