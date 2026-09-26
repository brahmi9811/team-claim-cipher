"""The honest scoreboard. The only code that reads `sim_truth`.

Every 30 seconds it writes one `metrics` document per insurer plus an
`insurer: "all"` row (docs/PLAN.md, "Metrics and scoreboard"):

    {ts, insurer, acceptance_rate, judge_precision, judge_recall, appeal_win_rate,
     recovered_usd, phi_leaks_to_llm, leaks_blocked, cost_usd_per_claim, ...sample sizes}

- acceptance_rate: share of held-out claims paid on first submission, over the
  most recent `window` held-out first submissions (so the curve moves).
- judge_precision / judge_recall: the Judge's wrongful calls against the
  ground truth, over every denial it has judged so far.
- appeal_win_rate, recovered_usd: from the simulator's own record of appeal decisions.
- phi_leaks_to_llm: planted PHI values found in any stored LLM output (verdicts,
  harness events, rules, appeal letters). Must stay 0.
- leaks_blocked: `phi_incidents` logged by the firewall.
- cost_usd_per_claim: LLM spend (`llm_calls`, written by common/llm.py) divided by the
  claims processed; None while nothing has been logged.

`compute_metrics` is pure (lists in, documents out) so it is easy to test and
to check by hand; `run_once` does the MongoDB reads and the write.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from forge.phi import value_hash

INSURERS = ("payer_a", "payer_b", "payer_c")
WRONGFUL_LABELS = {"wrongful_policy", "wrongful_bulk"}
DEFAULT_WINDOW = 60

_DIGIT_PATTERNS = [
    re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"),  # phone
    re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b"),  # SSN
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),  # US date
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO date
    re.compile(r"\b\d{8}\b"),  # packed date
]
_MEMBER_ID = re.compile(r"\b(?:PAM\d{9}|BXH-\d{8}|PC\d{10})\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def candidate_hashes(text: str, *, include_names: bool = True) -> set[str]:
    """Hash every string in `text` that could be a planted value, the same way forge hashed them."""
    found: set[str] = set()
    for pattern in _DIGIT_PATTERNS:
        for match in pattern.findall(text):
            digits = re.sub(r"\D", "", match)
            for kind in ("dob", "phone", "ssn"):
                found.add(value_hash(kind, digits))
    for match in _MEMBER_ID.findall(text):
        found.add(value_hash("member_id", match))
    if include_names:
        words = _WORD.findall(text)
        for a, b in zip(words, words[1:]):
            found.add(value_hash("name", f"{a} {b}"))
    return found


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _truth_for(adj: dict, by_id: dict[str, dict], latest_denial: dict[str, dict]) -> dict | None:
    return by_id.get(adj["_id"]) or latest_denial.get(adj.get("claim_id", ""))


def compute_metrics(
    truth: Iterable[dict],
    adjudications: Iterable[dict],
    phi_incidents: Iterable[dict] = (),
    llm_outputs: Iterable[dict] = (),
    llm_costs: Iterable[dict] = (),
    *,
    window: int = DEFAULT_WINDOW,
    now: datetime | None = None,
) -> list[dict]:
    """truth: `sim_truth` documents (adjudications and phi canaries).
    adjudications: the orchestrator's `adjudications` (only `_id`, `claim_id`, `insurer`, `verdict.label` are used).
    phi_incidents: firewall incidents. llm_outputs: {insurer, text, names_allowed} for every stored LLM output."""
    now = now or datetime.now(timezone.utc)
    truth = list(truth)
    decisions = [t for t in truth if t.get("type", "adjudication") == "adjudication"]
    canaries = {h for t in truth if t.get("type") == "phi_canary" for h in t.get("value_hashes", [])}

    by_id = {t["_id"]: t for t in decisions}
    latest_denial: dict[str, dict] = {}
    for t in sorted(decisions, key=lambda t: t.get("attempt", 1)):
        if t["status"] == "denied":
            latest_denial[t["claim_id"]] = t

    groups: dict[str, dict] = {k: defaultdict(int) for k in (*INSURERS, "all")}
    firsts: dict[str, list[dict]] = defaultdict(list)
    for t in decisions:
        if t.get("attempt", 1) == 1 and t.get("holdout"):
            firsts[t["insurer"]].append(t)
            firsts["all"].append(t)
        for appeal in t.get("appeals") or []:
            for key in (t["insurer"], "all"):
                g = groups[key]
                g["appeals"] += 1
                if appeal["outcome"] == "overturned":
                    g["overturned"] += 1
                    g["recovered"] += appeal.get("paid_amount", 0.0)

    for adj in adjudications:
        label = ((adj.get("verdict") or {}).get("label"))
        if not label:
            continue
        t = _truth_for(adj, by_id, latest_denial)
        if not t or t.get("status") != "denied":
            continue
        called = label in WRONGFUL_LABELS
        actual = t.get("kind") == "wrongful"
        for key in (t["insurer"], "all"):
            g = groups[key]
            g["judged"] += 1
            g["called_wrongful"] += called
            g["true_positive"] += called and actual
            g["actual_wrongful"] += actual

    spend: dict[str, float] = defaultdict(float)
    for call in llm_costs:
        for key in (call.get("insurer"), "all"):
            if key in groups:
                spend[key] += float(call.get("cost_usd") or 0)
    processed: dict[str, set] = defaultdict(set)
    for t in decisions:
        for key in (t["insurer"], "all"):
            processed[key].add(t["claim_id"])

    for incident in phi_incidents:
        for key in (incident.get("insurer"), "all"):
            if key in groups:
                groups[key]["blocked"] += 1

    if canaries:
        for out in llm_outputs:
            hits = candidate_hashes(out.get("text", ""), include_names=not out.get("names_allowed", False)) & canaries
            if hits:
                for key in (out.get("insurer"), "all"):
                    if key in groups:
                        groups[key]["leaks"] += 1

    docs = []
    for key, g in groups.items():
        size = window * (3 if key == "all" else 1)
        recent = sorted(firsts[key], key=lambda t: t["adjudicated_at"])[-size:]
        paid = sum(1 for t in recent if t["status"] == "paid")
        docs.append({
            "ts": now,
            "insurer": key,
            "acceptance_rate": _rate(paid, len(recent)),
            "judge_precision": _rate(g["true_positive"], g["called_wrongful"]),
            "judge_recall": _rate(g["true_positive"], g["actual_wrongful"]),
            "appeal_win_rate": _rate(g["overturned"], g["appeals"]),
            "recovered_usd": round(g["recovered"], 2),
            "phi_leaks_to_llm": g["leaks"],
            "leaks_blocked": g["blocked"],
            "cost_usd_per_claim": round(spend[key] / len(processed[key]), 5) if spend.get(key) and processed[key] else None,
            "holdout_sample": len(recent),
            "judged_denials": g["judged"],
            "appeals_decided": g["appeals"],
        })
    return docs


# --- MongoDB -----------------------------------------------------------------


def _llm_outputs(db) -> list[dict]:
    outputs = []
    for adj in db["adjudications"].find({"verdict": {"$ne": None}}, {"insurer": 1, "verdict": 1}):
        outputs.append({"insurer": adj.get("insurer"), "text": json.dumps(adj.get("verdict"), default=str)})
    for ev in db["harness_events"].find({}, {"insurer": 1, "reason": 1, "before": 1, "after": 1}):
        outputs.append({"insurer": ev.get("insurer"), "text": json.dumps([ev.get("reason"), ev.get("before"), ev.get("after")], default=str)})
    for rule in db["rules"].find({}, {"insurer": 1, "condition": 1, "fix": 1}):
        outputs.append({"insurer": rule.get("insurer"), "text": json.dumps([rule.get("condition"), rule.get("fix")], default=str)})
    for appeal in db["appeals"].find({}, {"insurer": 1, "letter_tokenized": 1}):
        # render_letter puts the patient's name back after the LLM is done, so names don't count here
        outputs.append({"insurer": appeal.get("insurer"), "text": appeal.get("letter_tokenized") or "", "names_allowed": True})
    return outputs


def run_once(db=None, *, window: int = DEFAULT_WINDOW, write: bool = True) -> list[dict]:
    if db is None:
        from common.db import get_db

        db = get_db("scorer")
    docs = compute_metrics(
        db["sim_truth"].find({}),
        db["adjudications"].find({"verdict": {"$ne": None}}, {"claim_id": 1, "insurer": 1, "verdict.label": 1}),
        db["phi_incidents"].find({}, {"insurer": 1}),
        _llm_outputs(db),
        db["llm_calls"].find({}, {"insurer": 1, "cost_usd": 1}),
        window=window,
    )
    if write:
        db["metrics"].insert_many([dict(d) for d in docs])
    return docs
