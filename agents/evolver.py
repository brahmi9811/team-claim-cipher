"""Evolver agent -- Loop 4 (Evolve), docs/PLAN.md.

`run_cycle(insurer)` is the meta-agent: it reads recent outcomes, proposes
at most one bounded change to that insurer's harness profile with a written
reason, and -- on the *next* cycle -- checks whether the change it made last
time actually helped, automatically reverting it if not. It never touches
`permissions` (only agents/trust_ladder.py may) or `guardrails.fixed`
(nobody may).
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from common import llm, profiles
from common.models import EventType
from firewall.guard import PHILeak
from validator.threshold_replay import replay_thresholds

from orchestrator.db import coll
from orchestrator.events import emit, mark_reverted
from orchestrator.metrics import recent_metrics

log = logging.getLogger(__name__)

MODEL_ID = llm.MODEL_SONNET
_PROMPT = (Path(__file__).parent / "prompts" / "evolver.md").read_text()

ROLLBACK_MARGIN = 0.05
ALLOWED_LEARNED_GUARDRAILS = {"drop_notes_field", "redact_notes_strict"}

_BOUNDED_NUMERIC: dict[str, tuple[float, float, type]] = {
    "context_policy.policy_clauses": (1, 5, int),
    "context_policy.paid_comparables": (0, 8, int),
    "judge.min_confidence": (0.50, 0.95, float),
    "judge.bulk_window_sec": (30, 300, int),
    "judge.bulk_min_identical": (5, 100, int),
}
_BOOLEAN_FIELDS = {"context_policy.include_pattern_stats", "appeal_strategy.quote_clause_verbatim"}
_ENUM_FIELDS = {"appeal_strategy.lead_with": {"clause", "paid_comparables", "pattern_stats"}}
_TARGET_METRIC = {
    "context_policy.policy_clauses": "appeal_win_rate",
    "context_policy.paid_comparables": "appeal_win_rate",
    "context_policy.include_pattern_stats": "appeal_win_rate",
    # NOT acceptance_rate: min_confidence has no causal effect on whether a
    # claim is paid on first submission (that's A's hidden rules + the
    # Scrubber's active rules, upstream of anything the Judge does). Gating
    # it on acceptance_rate meant the rollback check could never fire --
    # acceptance stayed flat either way -- and the heuristic below kept
    # ratcheting the same field down every cycle. appeal_win_rate is what a
    # confidence-bar change can actually move.
    "judge.min_confidence": "appeal_win_rate",
    "judge.bulk_window_sec": "appeal_win_rate",
    "judge.bulk_min_identical": "appeal_win_rate",
    "appeal_strategy.lead_with": "appeal_win_rate",
    "appeal_strategy.quote_clause_verbatim": "appeal_win_rate",
}


def run_cycle(insurer: str) -> dict | None:
    """One Evolver cycle for `insurer`. Returns the Event it produced
    (a rollback, a profile change, or a guardrail addition), or None."""
    metrics = recent_metrics(insurer)

    reverted = _check_rollback(insurer, metrics)
    if reverted is not None:
        return reverted

    try:
        proposal = _propose_with_llm(insurer, metrics)
    except (llm.LLMUnavailable, KeyError, PHILeak):
        # KeyError: the LLM returned valid JSON but without "value"/"reason" once
        # "field" was non-null -- same fallback as a missing key entirely.
        proposal = _propose_heuristic(insurer, metrics)
    if proposal is None:
        return None

    field_path, new_value, reason = proposal
    try:
        return _apply_change(insurer, field_path, new_value, reason, metrics)
    except ValueError:
        return None  # invalid proposal (out of bounds, or a forbidden field) -- skip this cycle


def _safe_save_profile(profile: dict, event: dict) -> None:
    """`common.profiles.save_profile` (unlike `get_profile`) doesn't catch a
    Mongo outage itself; a hiccup here shouldn't crash the Evolver's cycle."""
    try:
        profiles.save_profile(profile, event)
    except Exception:  # noqa: BLE001
        log.exception("failed to save harness_profiles for %s", profile.get("_id"))


def _check_rollback(insurer: str, metrics: dict) -> dict | None:
    state = coll("evolver_state").find_one({"_id": insurer})
    pending = state.get("pending") if state else None
    if not pending:
        return None

    target = pending["target_metric"]
    baseline, current = pending["baseline_value"], metrics.get(target)
    if target is None or baseline is None or current is None:
        _set_pending(insurer, None)
        return None

    if current < baseline - ROLLBACK_MARGIN:
        profile = profiles.get_profile(insurer)
        before = copy.deepcopy(profile)
        _set_field(profile, pending["field"], pending["before_value"])
        profile["version"] += 1
        profile["updated_by"] = "evolver"
        event = emit(
            insurer=insurer,
            actor="evolver",
            type_=EventType.ROLLBACK.value,
            reason=f"Reverted {pending['field']}: {target} fell from {baseline:.2f} to {current:.2f} after the change.",
            before=before,
            after=profile,
        )
        _safe_save_profile(profile, event)
        mark_reverted(pending["event_id"])
        _set_pending(insurer, None)
        return event

    _set_pending(insurer, None)  # confirmed: metric held or improved, keep the change
    return None


def _apply_change(insurer: str, field_path: str, new_value, reason: str, metrics: dict) -> dict | None:
    _validate(field_path, new_value)

    if field_path == "judge.min_confidence":
        # docs/PLAN.md: "The Judge's thresholds follow the same idea: the
        # Evolver may only change them if a replay over past verdicts with
        # known appeal outcomes shows an improvement." This is that gate --
        # skipping it is exactly how the confidence bar got ratcheted to the
        # floor with no rollback ever catching it.
        check = replay_thresholds(insurer, min_confidence=new_value)
        if not check.get("recommend_change"):
            raise ValueError(f"threshold replay does not support this change ({check.get('reason')})")
        reason = f"{reason} Threshold replay: {check['reason']}."

    profile = profiles.get_profile(insurer)
    before = copy.deepcopy(profile)
    is_guardrail = field_path == "guardrails.learned"

    if is_guardrail and new_value in profile["guardrails"].get("learned", []):
        return None  # already learned; not a real change -- don't bump version or log a false event

    _set_field(profile, field_path, new_value)
    profile["version"] += 1
    profile["updated_by"] = "evolver"
    event_type = EventType.GUARDRAIL_ADDED.value if is_guardrail else EventType.PROFILE_CHANGED.value
    event = emit(insurer=insurer, actor="evolver", type_=event_type, reason=reason, before=before, after=profile)
    _safe_save_profile(profile, event)

    if not is_guardrail:  # guardrail additions are monotonic safety improvements; never rolled back
        target = _TARGET_METRIC.get(field_path)
        _set_pending(insurer, {
            "field": field_path,
            "before_value": _get_field(before, field_path),
            "event_id": event["_id"],
            "target_metric": target,
            "baseline_value": metrics.get(target),
        })
    return event


def _propose_with_llm(insurer: str, metrics: dict) -> tuple[str, object, str] | None:
    profile = profiles.get_profile(insurer)
    # Only the fields the Evolver may change. `permissions` (Trust Ladder only) carries an
    # `earned_at` date: not JSON-serializable, and a date the PHI guard would flag as a DOB.
    editable = {k: profile[k] for k in ("context_policy", "judge", "appeal_strategy", "guardrails") if k in profile}
    user = json.dumps({
        "insurer": insurer,
        "metrics": metrics,
        "current_profile": editable,
        "allowed_learned_guardrails": sorted(ALLOWED_LEARNED_GUARDRAILS - set(profile["guardrails"]["learned"])),
    }, default=str)
    result = llm.complete(system=_PROMPT, user=user, model=MODEL_ID, insurer=insurer, claim_id="evolver-cycle")
    if result.get("field") is None:
        return None
    return result["field"], result["value"], result["reason"]


def _propose_heuristic(insurer: str, metrics: dict) -> tuple[str, object, str] | None:
    """Deterministic fallback, mirroring the examples in docs/PLAN.md.

    Every branch here is gated on `appeal_win_rate` -- not `acceptance_rate`,
    which nothing the Evolver is allowed to touch can actually move (that's
    fixed by A's hidden insurer rules and the Scrubber's active rules,
    upstream of the Judge entirely). A low win rate means the Judge's
    wrongful calls are often getting upheld on appeal, i.e. wrong: first try
    strengthening the appeal itself (lead with comparables, add more of
    them); only once those are maxed out does it become a Judge-threshold
    problem, and the fix there is to RAISE the confidence bar (make fewer,
    more confident wrongful calls) -- never lower it.
    """
    profile = profiles.get_profile(insurer)

    # A blocked leak comes first: adopt the guardrail the firewall proposed for it
    # (PLAN.md: "On a hit ... the Evolver proposes a new guardrail").
    learned = set(profile["guardrails"].get("learned", []))
    open_leaks = {g: n for g, n in (metrics.get("phi_blocked_by_guardrail") or {}).items()
                  if g in ALLOWED_LEARNED_GUARDRAILS and g not in learned}
    if open_leaks:
        guardrail = max(open_leaks, key=open_leaks.get)
        n = open_leaks[guardrail]
        return (
            "guardrails.learned",
            guardrail,
            f"The leak detector blocked {n} LLM call{'s' if n != 1 else ''} with patient identifiers in the notes; adopting '{guardrail}'.",
        )

    win_rate, appeal_n = metrics["appeal_win_rate"], metrics["appeal_sample_size"]
    if win_rate is None or appeal_n < 3 or win_rate >= 0.5:
        return None

    if not profile["appeal_strategy"].get("quote_clause_verbatim"):
        return (
            "appeal_strategy.quote_clause_verbatim",
            True,
            f"Appeal win rate is {win_rate:.0%} over the last {appeal_n} appeals; quoting the policy clause word for word.",
        )
    if profile["appeal_strategy"]["lead_with"] != "paid_comparables" and profile["context_policy"]["paid_comparables"] > 0:
        return (
            "appeal_strategy.lead_with",
            "paid_comparables",
            f"Appeal win rate is {win_rate:.0%} over the last {appeal_n} appeals; leading with paid comparable claims.",
        )
    if profile["context_policy"]["paid_comparables"] < 8:
        return (
            "context_policy.paid_comparables",
            profile["context_policy"]["paid_comparables"] + 1,
            f"Appeal win rate is {win_rate:.0%} over the last {appeal_n} appeals; including one more paid comparable.",
        )
    if profile["judge"]["min_confidence"] < 0.95:
        new_conf = round(min(0.95, profile["judge"]["min_confidence"] + 0.05), 2)
        return (
            "judge.min_confidence",
            new_conf,
            f"Appeal win rate is still {win_rate:.0%} over the last {appeal_n} appeals even with strong evidence "
            "already included; raising the Judge's confidence bar so fewer marginal wrongful calls are made.",
        )
    return None


def _validate(field_path: str, value) -> None:
    if field_path in _BOUNDED_NUMERIC:
        lo, hi, cast = _BOUNDED_NUMERIC[field_path]
        value = cast(value)
        if not (lo <= value <= hi):
            raise ValueError(f"{field_path}={value} outside allowed range [{lo}, {hi}]")
    elif field_path in _BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(f"{field_path} must be boolean")
    elif field_path in _ENUM_FIELDS:
        if value not in _ENUM_FIELDS[field_path]:
            raise ValueError(f"{field_path}={value} not one of {_ENUM_FIELDS[field_path]}")
    elif field_path == "guardrails.learned":
        if value not in ALLOWED_LEARNED_GUARDRAILS:
            raise ValueError(f"guardrail '{value}' is not in the allowed list")
    else:
        raise ValueError(f"'{field_path}' is not a field the Evolver may change")


def _set_field(profile: dict, field_path: str, value) -> None:
    if field_path == "guardrails.learned":
        learned = profile["guardrails"].setdefault("learned", [])
        if value not in learned:
            learned.append(value)
        return
    section, key = field_path.split(".")
    profile[section][key] = value


def _get_field(profile: dict, field_path: str):
    section, key = field_path.split(".")
    return profile[section][key]


def _set_pending(insurer: str, pending: dict | None) -> None:
    coll("evolver_state").update_one({"_id": insurer}, {"$set": {"pending": pending}}, upsert=True)
