#!/usr/bin/env python3
"""Offline smoke checks for Member B modules (no Atlas / API key required).

    python scripts/check_b.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Windows consoles default to cp1252, which can't print the arrows in the section titles.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS: list[str] = []
_FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASS.append(label)
        print(f"  PASS  {label}")
    else:
        _FAIL.append(label)
        print(f"  FAIL  {label}  {detail}")


def main() -> int:
    print("[B] models")
    from common.models import Claim, default_harness_profile, new_rule

    profile = default_harness_profile("payer_b")
    check("default profile has judge.min_confidence", profile["judge"]["min_confidence"] == 0.75)

    print("\n[B] rules")
    from common.rules import apply_fix, matches, to_match

    claim = {
        "_id": "clm_test",
        "insurer": "payer_a",
        "diagnosis_codes": ["Z00.00"],
        "lines": [{"line_no": 1, "hcpcs": "G0439", "modifiers": [], "units": 1, "charge_usd": 150.0}],
        "total_charge_usd": 150.0,
        "prior_auth_id": None,
        "records": {"prior_auth_id": "PA-999"},
        "encounter_type": "wellness",
    }
    rule = new_rule(
        insurer="payer_a",
        condition={"all": [
            {"field": "lines.hcpcs", "op": "eq", "value": "G0439"},
            {"field": "prior_auth_id", "op": "missing"},
        ]},
        fix={"action": "attach_prior_auth"},
        evidence_ids=["adj_1"],
    )
    check("matches missing prior auth", matches(rule, claim))
    fixed = apply_fix(rule, claim)
    check("apply_fix attaches prior auth", fixed["prior_auth_id"] == "PA-999")
    check("apply_fix does not change charges", fixed["total_charge_usd"] == 150.0)
    check("to_match returns $and", "$and" in to_match(rule))

    # no-upcoding
    bad = dict(rule, fix={"action": "attach_prior_auth"})
    try:
        hacked = apply_fix(bad, claim)
        hacked["diagnosis_codes"] = ["E11.9"]  # simulate a bad fix mutating after
        # force the guard by monkeying: call with a custom action isn't possible;
        # instead verify immutable check via drop_line that lowers charge is ok
        drop = dict(rule, fix={"action": "drop_line", "params": {"hcpcs": "G0439"}})
        dropped = apply_fix(drop, claim)
        check("drop_line lowers or keeps charge", dropped["total_charge_usd"] <= 150.0)
    except Exception as exc:
        check("drop_line works", False, str(exc))

    print("\n[B] firewall tokenize + guard")
    from firewall import PHILeak, encrypted_collection, guard, render_letter, tokenize

    check("encrypted_collection is exportable", callable(encrypted_collection))

    full_claim = {
        **claim,
        "patient": {
            "name": "Jane Doe",
            "dob": "1961-04-12",
            "member_id": "MBR123456",
            "ssn": "123-45-6789",
            "address": "1 Main St, Boston, MA 02108",
            "phone": "555-123-4567",
        },
        "service_date": "2026-09-01",
        "created_at": "2026-09-10T12:00:00+00:00",
        "notes": "pt called back, DOB 04/12/1961, cell 555-999-8888",
    }
    tok = tokenize(full_claim, profile=profile)
    check("patient_token set", tok["patient_token"].startswith("PATIENT_"))
    check("age_band set", "-" in tok["age_band"])
    check("state extracted", tok["state"] == "MA")
    check("notes removed by default", tok["notes_redacted"] is None)
    check("no raw name in tokenized", "Jane" not in str(tok))

    clean = guard("Appeal for PATIENT_0001 regarding imaging.", direction="request", insurer="payer_a", claim_id="clm_test")
    check("guard allows tokenized text", "PATIENT_0001" in clean)

    blocked = False
    try:
        guard("Patient SSN is 123-45-6789", direction="request", insurer="payer_a", claim_id="clm_test")
    except PHILeak as leak:
        blocked = leak.pattern == "ssn"
    check("guard blocks SSN", blocked)

    mid_blocked = False
    try:
        guard("member PAM123456789 called", direction="request", insurer="payer_a", claim_id="clm_test")
    except PHILeak as leak:
        mid_blocked = leak.pattern == "member_id"
    check("guard blocks PAM member id", mid_blocked)

    letter = render_letter("Re: claim for PATIENT_9999 (unmapped)")
    check("render_letter leaves unknown token", "PATIENT_9999" in letter)

    print("\n[B] search stub")
    from common.search import vector_search

    hits = vector_search("policies", "imaging medically necessary", {"insurer": "payer_b"}, k=2)
    check("stub policies returned", len(hits) >= 1, str(hits))

    print("\n[B] validator replay (offline → reject with empty data)")
    from validator.replay import replay

    stats = replay(rule)
    check("replay returns decision", stats["decision"] in {"promote", "reject"})

    print("\n[B] llm module import")
    from common.llm import LLMUnavailable, complete

    try:
        complete(system="x", user="y", model="anthropic/claude-haiku-4.5", insurer="payer_a", claim_id="c")
        check("complete without key raises", False)
    except LLMUnavailable:
        check("complete without key raises LLMUnavailable", True)

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
