"""Local demo-data generator, standing in for Member A's `forge/` (Synthea
loader) until real claims exist in `claims`. This is intentionally NOT
`scripts/seed_fake.py` -- that file is Member D's, for the live view, and
covers all the collections D's panels need. This one is scoped to exactly
what Member C's own orchestrator and `scripts/smoke.py` need: a handful of
synthetic (never real) patients and claim shapes that reliably exercise
every branch of the loop, matched to the fallback rules in
`orchestrator/contracts/sim_client.py` so the whole thing works with no
simulator and no LLM key running.
"""
from __future__ import annotations

import random
from datetime import datetime, timezone

from common.models import new_id

from orchestrator.db import coll

_FIRST_NAMES = ["Alex", "Jordan", "Sam", "Taylor", "Morgan", "Casey"]
_LAST_NAMES = ["Rivera", "Chen", "Okafor", "Nguyen", "Patel", "Kowalski"]
_STATES = ["CA", "TX", "NY", "OH", "WA"]


def _fake_patient(rng: random.Random) -> dict:
    return {
        "name": f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}",
        "dob": datetime(rng.randint(1945, 2005), rng.randint(1, 12), rng.randint(1, 28)).strftime("%Y-%m-%d"),
        "member_id": f"M{rng.randint(10_000_000, 99_999_999)}",
        "ssn": f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
        "address": f"{rng.randint(1, 9999)} Fake St, Springfield, {rng.choice(_STATES)} {rng.randint(10000, 99999)}",
        "phone": f"555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
    }


def build_claim(
    insurer: str,
    lines: list[dict],
    *,
    rng: random.Random,
    prior_auth_id: str | None = None,
    diagnosis_codes: list[str] | None = None,
    notes: str = "",
    holdout: bool = False,
) -> dict:
    total = round(sum(ln["charge_usd"] for ln in lines), 2)
    return {
        "_id": new_id("clm"),
        "insurer": insurer,
        "patient": _fake_patient(rng),
        "service_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "encounter_type": "outpatient",
        "diagnosis_codes": diagnosis_codes or ["R51.9"],
        "lines": lines,
        "total_charge_usd": total,
        "prior_auth_id": prior_auth_id,
        "referring_provider_id": f"NPI{rng.randint(100000, 999999)}",
        "records": {"prior_auth_id": f"PA{rng.randint(1000, 9999)}", "referring_provider_id": f"NPI{rng.randint(100000, 999999)}"},
        "notes": notes,
        "holdout": holdout,
        "source": "demo",  # claims_source.py's real-claim cursor skips these
        # A string, matching forge/claims.py's `now.isoformat()` -- NOT
        # utcnow() -- for two reasons: firewall.tokenize()'s _service_day()
        # parses claims.created_at expecting a string, and orchestrator/
        # claims_source.py sorts/cursors on this exact field across BOTH
        # forge-loaded and generated claims in the same `claims` collection;
        # mixing BSON types there would scramble ordering (the same class of
        # bug fixed for adjudications/harness_events, but for this field the
        # fix is to match forge's convention, not to override it).
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def missing_prior_auth_claim(insurer: str, rng: random.Random) -> dict:
    """Triggers the CO-16 legitimate-denial fallback (see sim_client.py)."""
    lines = [{"line_no": 1, "hcpcs": "G0439", "modifiers": [], "units": 1, "charge_usd": 150.0}]
    return build_claim(insurer, lines, rng=rng, prior_auth_id=None)


def clean_paid_claim(insurer: str, rng: random.Random) -> dict:
    lines = [{"line_no": 1, "hcpcs": "99213", "modifiers": [], "units": 1, "charge_usd": 120.0}]
    claim = build_claim(insurer, lines, rng=rng)
    claim["prior_auth_id"] = claim["records"]["prior_auth_id"]
    return claim


def wrongful_bulk_imaging_claim(rng: random.Random) -> dict:
    """Payer B, high-cost imaging: triggers the wrongful_bulk fallback."""
    lines = [{"line_no": 1, "hcpcs": "70551", "modifiers": [], "units": 1, "charge_usd": 2400.0}]
    claim = build_claim("payer_b", lines, rng=rng)
    claim["prior_auth_id"] = claim["records"]["prior_auth_id"]
    return claim


def wrongful_policy_claim(rng: random.Random) -> dict:
    """Payer A, chemo administration: triggers the wrongful_policy fallback."""
    lines = [{"line_no": 1, "hcpcs": "J9999", "modifiers": [], "units": 1, "charge_usd": 800.0}]
    claim = build_claim("payer_a", lines, rng=rng, diagnosis_codes=["C50.911"])
    claim["prior_auth_id"] = claim["records"]["prior_auth_id"]
    return claim


def demo_claims(insurer: str, n: int, *, seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    claims = []
    for _ in range(n):
        roll = rng.random()
        if roll < 0.35:
            claims.append(missing_prior_auth_claim(insurer, rng))
        elif insurer == "payer_b" and roll < 0.45:
            claims.append(wrongful_bulk_imaging_claim(rng))
        elif insurer == "payer_a" and roll < 0.45:
            claims.append(wrongful_policy_claim(rng))
        else:
            claims.append(clean_paid_claim(insurer, rng))
    return claims


def seed_policies() -> None:
    """One clause per insurer covering each of the wrongful-denial test
    codes above, so the Judge's evidence retrieval has something to find."""
    for insurer in ("payer_a", "payer_b", "payer_c"):
        if coll("policies").find_one({"insurer": insurer, "current": True}):
            continue
        coll("policies").insert_one({
            "_id": f"{insurer}_v1_c7", "insurer": insurer, "version": 1, "clause_no": 7,
            "title": "Chemotherapy administration",
            "clause_text": "Clause 7: chemotherapy administration billed as J9999 is covered when accompanied by a supporting oncology diagnosis.",
            "current": True,
        })
        coll("policies").insert_one({
            "_id": f"{insurer}_v1_c12", "insurer": insurer, "version": 1, "clause_no": 12,
            "title": "Diagnostic imaging",
            "clause_text": "Clause 12: diagnostic imaging such as 70551 is covered once per encounter with an appropriate diagnosis.",
            "current": True,
        })
