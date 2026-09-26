"""Machine-checkable prevention rules (docs/PLAN.md, Member B specs).

Used by C's scrubber and B's validator — both must apply exactly the same
logic. ``apply_fix`` refuses any change to diagnosis codes or charges
(the no-upcoding guardrail lives here, not in a prompt).
"""
from __future__ import annotations

import copy
from typing import Any

from common.paths import resolve

ALLOWED_FIELDS = {
    "diagnosis_codes",
    "lines.hcpcs",
    "lines.modifiers",
    "lines.units",
    "total_charge_usd",
    "prior_auth_id",
    "referring_provider_id",
    "encounter_type",
}
ALLOWED_OPS = {"eq", "ne", "in", "gt", "lt", "missing", "present", "contains"}
ALLOWED_FIX_ACTIONS = {
    "attach_prior_auth",
    "attach_referring_provider",
    "add_modifier",
    "split_units",
    "drop_line",
    "hold_for_review",
}


def _condition_matches(cond: dict, claim: dict) -> bool:
    field_path, op, value = cond["field"], cond["op"], cond.get("value")
    if field_path not in ALLOWED_FIELDS:
        raise ValueError(f"field not allowed in a rule condition: {field_path}")
    if op not in ALLOWED_OPS:
        raise ValueError(f"op not allowed in a rule condition: {op}")
    present = [v for v in resolve(claim, field_path) if v is not None]

    if op == "missing":
        return len(present) == 0
    if op == "present":
        return len(present) > 0
    if op == "eq":
        return any(v == value for v in present)
    if op == "ne":
        return not any(v == value for v in present)
    if op == "in":
        return any(v in value for v in present)
    if op == "gt":
        return any(isinstance(v, (int, float)) and v > value for v in present)
    if op == "lt":
        return any(isinstance(v, (int, float)) and v < value for v in present)
    if op == "contains":
        return any(value in v for v in present if isinstance(v, (str, list)))
    return False


def matches(rule: dict, claim: dict) -> bool:
    """Does ``claim`` satisfy every condition in ``rule["condition"]["all"]``?"""
    return all(_condition_matches(c, claim) for c in rule["condition"].get("all", []))


def to_match(rule: dict) -> dict:
    """Build a MongoDB ``$match`` stage equivalent to ``rule["condition"]``.

    Nested array fields (``lines.hcpcs``) use dotted paths so Atlas treats
    them the same way ``matches()`` fans out in Python.
    """
    op_map = {"eq": "$eq", "ne": "$ne", "in": "$in", "gt": "$gt", "lt": "$lt"}
    clauses: list[dict[str, Any]] = []
    for cond in rule["condition"].get("all", []):
        field_path, op, value = cond["field"], cond["op"], cond.get("value")
        if field_path not in ALLOWED_FIELDS:
            raise ValueError(f"field not allowed in a rule condition: {field_path}")
        if op == "missing":
            clauses.append(
                {
                    "$or": [
                        {field_path: {"$exists": False}},
                        {field_path: None},
                        {field_path: ""},
                    ]
                }
            )
        elif op == "present":
            clauses.append(
                {
                    field_path: {
                        "$exists": True,
                        "$nin": [None, ""],
                    }
                }
            )
        elif op == "contains":
            clauses.append({field_path: value})
        else:
            clauses.append({field_path: {op_map[op]: value}})

    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def apply_fix(rule: dict, claim: dict) -> dict:
    """Return a fixed copy of ``claim``.

    Never touches diagnosis codes; never raises a billed amount.
    ``hold_for_review`` is a no-op here — the scrubber holds instead of submitting.
    """
    action = rule["fix"]["action"]
    params = rule["fix"].get("params", {})
    if action not in ALLOWED_FIX_ACTIONS:
        raise ValueError(f"fix action not allowed: {action}")

    before_diagnoses = list(claim.get("diagnosis_codes", []))
    before_total = claim.get("total_charge_usd", 0.0)
    fixed = copy.deepcopy(claim)

    if action == "hold_for_review":
        pass
    elif action == "attach_prior_auth":
        fixed["prior_auth_id"] = fixed.get("records", {}).get("prior_auth_id")
    elif action == "attach_referring_provider":
        fixed["referring_provider_id"] = fixed.get("records", {}).get("referring_provider_id")
    elif action == "add_modifier":
        target_hcpcs = _hcpcs_from_condition(rule)
        for line in fixed.get("lines", []):
            if target_hcpcs is None or line.get("hcpcs") == target_hcpcs:
                modifiers = line.setdefault("modifiers", [])
                if params["modifier"] not in modifiers:
                    modifiers.append(params["modifier"])
    elif action == "split_units":
        max_units = params["max_units"]
        new_lines = []
        for line in fixed.get("lines", []):
            units = line.get("units", 1)
            if units <= max_units:
                new_lines.append(line)
                continue
            per_unit_charge = line["charge_usd"] / units
            remaining = units
            while remaining > 0:
                take = min(max_units, remaining)
                split_line = dict(line, units=take, charge_usd=round(per_unit_charge * take, 2))
                new_lines.append(split_line)
                remaining -= take
        fixed["lines"] = new_lines
    elif action == "drop_line":
        fixed["lines"] = [ln for ln in fixed.get("lines", []) if ln.get("hcpcs") != params["hcpcs"]]
        fixed["total_charge_usd"] = round(sum(ln["charge_usd"] for ln in fixed["lines"]), 2)

    if fixed.get("diagnosis_codes", []) != before_diagnoses:
        raise ValueError("apply_fix refused: a fix attempted to change diagnosis codes")
    if fixed.get("total_charge_usd", 0.0) > before_total + 1e-6:
        raise ValueError("apply_fix refused: a fix attempted to raise the billed amount")
    return fixed


def _hcpcs_from_condition(rule: dict) -> str | None:
    for cond in rule["condition"].get("all", []):
        if cond["field"] == "lines.hcpcs" and cond["op"] == "eq":
            return cond["value"]
    return None
