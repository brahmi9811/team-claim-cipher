"""Turn Synthea encounters into about 2,000 claims in the frozen `Claim` shape.

Each encounter is mapped to the 40-code set: a diagnosis from the encounter
reason (or the patient's known conditions), then the services a hospital
would bill for that visit type. The hospital's own records hold the prior
authorization and referring provider it obtained; billing staff copy them onto
the claim most of the time but not always, which is what the scrubber learns to fix.

Synthea dates span decades, so service dates are re-based onto the last few
months (keeping their order); a few claims are deliberately filed late.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone

from .codes import ICD10, PROCEDURES
from .synthea import Encounter, Patient

INSURERS = ("payer_a", "payer_b", "payer_c")

# Encounter reason / condition text -> diagnosis. First match wins.
REASON_MAP: list[tuple[tuple[str, ...], str]] = [
    (("neoplasm of breast",), "C50.911"),
    (("screening for malignant neoplasm of colon",), "Z12.11"),
    (("chronic kidney disease",), "N18.4"),
    (("pregnancy", "miscarriage", "prenatal"), "Z34.90"),
    (("diabetes",), "E11.9"),
    (("hypertension",), "I10"),
    (("hyperlipidemia",), "E78.5"),
    (("asthma",), "J45.909"),
    (("sleep", "apnea"), "G47.33"),
    (("sinusitis", "pharyngitis", "bronchitis", "otitis", "sore throat", "rhinitis", "respiratory"), "J06.9"),
    (("sprain", "fracture", "laceration"), "S93.401A"),
    (("chronic pain", "back pain"), "M54.50"),
    (("heart", "ischemic", "coronary", "aortic", "angina", "chest pain"), "R07.9"),
    (("appendicitis", "abdominal", "cystitis"), "R10.9"),
    (("transition from acute care", "follow-up"), "Z09"),
]
SKIP_CLASSES = {"snf", "hospice", "home"}
SKIP_REASONS = ("dental", "gingivitis", "teeth", "tooth")
AMBULATORY = {"ambulatory", "outpatient", "urgentcare", "virtual"}
EMERGENCY_DX = {"R07.9", "R10.9", "S93.401A", "J45.909", "J06.9"}
FALLBACK_DX = ["J06.9", "M54.50", "R10.9", "I10", "E78.5"]

AUTH_TYPICAL = {"C8901", "C8908", "E0601", "J9355", "Q0084", "G0279"}
REFERRAL_TYPICAL = {"C8901", "C8908", "E0601", "G0283", "G0279"}
MA_AREA_CODES = ["508", "617", "781", "978", "413"]

# Tuning knobs (see sim/report.py). Probabilities per claim.
MIX = {
    "late_filing": 0.025,
    "auth_on_file_typical": 0.97,
    "auth_on_file_wellness": 0.92,
    "auth_on_file_other": 0.30,
    "auth_copied_typical": 0.65,
    "auth_copied_wellness": 0.35,
    "auth_copied_other": 0.55,
    "referral_on_file": 0.96,
    "referral_copied_typical": 0.65,
    "referral_copied_other": 0.72,
    "modifier_25_present": 0.40,
    "ambulance_modifier_present": 0.40,
    "follow_up": 0.20,
    "mra_abdominal_pain": 0.45,
    "mra_renal_workup": 0.18,
}


def _rng_for(*parts: str) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:16], 16))


def map_reason(text: str) -> str | None:
    lowered = (text or "").lower()
    for keywords, dx in REASON_MAP:
        if any(k in lowered for k in keywords):
            return dx
    return None


def patient_dx(patient: Patient) -> list[str]:
    found = []
    for condition in patient.conditions:
        dx = map_reason(condition)
        if dx and dx not in found and dx not in {"Z09", "Z12.11", "Z34.90"}:
            found.append(dx)
    return found


def eligible(enc: Encounter) -> bool:
    return enc.encounter_class not in SKIP_CLASSES and not any(s in enc.reason.lower() for s in SKIP_REASONS)


def assign_insurer(enc: Encounter) -> str:
    """Patients change plans over time: one insurer per patient per half-year of the original record."""
    half = "H1" if enc.start[5:7] <= "06" else "H2"
    return INSURERS[int(hashlib.sha256(f"{enc.patient_id}|{enc.start[:4]}|{half}".encode()).hexdigest(), 16) % 3]


def member_id(patient_id: str, insurer: str) -> str:
    """Formats per insurer; B's leak detector (firewall/guard.py) knows these patterns."""
    r = _rng_for(patient_id, insurer, "member")
    if insurer == "payer_a":
        return f"PAM{r.randint(0, 10**9 - 1):09d}"
    if insurer == "payer_b":
        return f"BXH-{r.randint(0, 10**8 - 1):08d}"
    return f"PC{r.randint(0, 10**10 - 1):010d}"


def phone(patient_id: str) -> str:
    r = _rng_for(patient_id, "phone")
    return f"({r.choice(MA_AREA_CODES)}) 555-01{r.randint(0, 99):02d}"


def _auth_id(r: random.Random) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "PA-" + "".join(r.choice(alphabet) for _ in range(6))


def _provider_id(r: random.Random) -> str:
    return f"PRV-{r.randint(100000, 999999)}"


class _Lines:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, hcpcs: str, units: int = 1, modifiers: list[str] | None = None) -> None:
        if hcpcs in {ln["hcpcs"] for ln in self.items}:
            return
        price = PROCEDURES[hcpcs].price_usd
        self.items.append({
            "line_no": len(self.items) + 1,
            "hcpcs": hcpcs,
            "modifiers": list(modifiers or []),
            "units": units,
            "charge_usd": round(price * units, 2),
        })

    def codes(self) -> set[str]:
        return {ln["hcpcs"] for ln in self.items}


def services_for(enc: Encounter, patient: Patient, r: random.Random, age: int, first_wellness: bool) -> tuple[list[str], list[dict]]:
    """Diagnosis codes and service lines for one encounter."""
    dx: list[str] = []
    lines = _Lines()
    known = patient_dx(patient)
    reason_dx = map_reason(enc.reason)
    cls = enc.encounter_class

    if cls == "wellness":
        dx.append("Z00.01" if r.random() < 0.2 else "Z00.00")
        lines.add("G0438" if first_wellness else "G0439")
        if r.random() < 0.30:
            lines.add("G0444")
        if age >= 45:
            roll = r.random()
            if roll < 0.14:
                lines.add("G0328")
                dx.append("Z12.11")
            elif roll < 0.22:
                lines.add("G0121")
                dx.append("Z12.11")
        if patient.gender == "M" and age >= 50 and r.random() < 0.25:
            lines.add("G0103")
            dx.append("Z12.5")
        if patient.gender == "F" and age >= 40 and r.random() < 0.12:
            lines.add("G0279")
            dx.append("Z12.31")
        if patient.gender == "F" and age >= 30 and dx[0] == "Z00.01" and r.random() < 0.35:
            lines.add("C8908")
            dx.append("N63.0")
        if r.random() < 0.18:
            modifiers = ["25"] if r.random() < MIX["modifier_25_present"] else []
            lines.add("G0463", modifiers=modifiers)
            dx.append(known[0] if known else r.choice(FALLBACK_DX))

    elif cls in AMBULATORY:
        primary = reason_dx or (r.choice(known) if known else r.choice(FALLBACK_DX))
        if primary == "Z12.11":
            lines.add("G0121")
            dx.append("Z12.11")
        else:
            lines.add("G0463")
            if primary == "Z09" or r.random() < MIX["follow_up"]:
                dx.append("Z09")
                if primary == "Z09":
                    primary = known[0] if known else r.choice(FALLBACK_DX)
            dx.append(primary)
            _ambulatory_extras(primary, lines, r)
        for extra in known:
            if extra not in dx and len(dx) < 3 and r.random() < 0.4:
                dx.append(extra)

    elif cls == "emergency":
        primary = reason_dx if reason_dx in EMERGENCY_DX else r.choice(["R07.9", "R10.9", "S93.401A", "J45.909"])
        dx.append(primary)
        lines.add("G0383")
        if r.random() < 0.6:
            lines.add("J7030")
        if r.random() < 0.55:
            lines.add("J1885", units=r.randint(1, 6))
        if r.random() < 0.35:
            lines.add("A0429", modifiers=["RH"] if r.random() < MIX["ambulance_modifier_present"] else [])
        if r.random() < (0.35 if primary == "R10.9" else 0.15):
            lines.add("C8901")

    else:  # inpatient: billed here as observation
        primary = reason_dx if reason_dx in EMERGENCY_DX else r.choice(["R07.9", "R10.9"])
        dx.append(primary)
        lines.add("G0378", units=r.randint(8, 40))
        lines.add("J7030")
        if r.random() < 0.4:
            lines.add("J1885", units=r.randint(1, 6))

    return dx, lines.items


def _ambulatory_extras(primary: str, lines: _Lines, r: random.Random) -> None:
    if primary == "E11.9" and r.random() < 0.40:
        lines.add("G0108", units=r.randint(1, 6))
    elif primary == "G47.33" and r.random() < 0.45:
        lines.add("E0601")
    elif primary == "M54.50" and r.random() < 0.50:
        lines.add("G0283", units=r.randint(1, 6))
    elif primary == "R10.9" and r.random() < MIX["mra_abdominal_pain"]:
        lines.add("C8901")
    elif primary in {"I10", "N18.4"} and r.random() < MIX["mra_renal_workup"]:
        lines.add("C8901")  # renal artery angiography work-up
    elif primary == "C50.911":
        roll = r.random()
        if roll < 0.55:
            lines.add("Q0084")
            lines.add("J9355", units=r.randint(30, 50))
            if r.random() < 0.6:
                lines.add("J7030")
        elif roll < 0.85:
            lines.add("C8908")
    elif primary == "S93.401A" and r.random() < 0.30:
        lines.add("J1885", units=r.randint(1, 6))


def _paperwork(codes: set[str], cls: str, r: random.Random) -> tuple[dict, str | None, str | None]:
    """What the hospital has on file, and what billing staff copied onto the claim."""
    if codes & AUTH_TYPICAL:
        kind = "typical"
    elif cls == "wellness":
        kind = "wellness"
    else:
        kind = "other"
    auth = _auth_id(r) if r.random() < MIX[f"auth_on_file_{kind}"] else None
    auth_on_claim = auth if auth and r.random() < MIX[f"auth_copied_{kind}"] else None

    referral = _provider_id(r) if r.random() < MIX["referral_on_file"] else None
    ref_kind = "typical" if codes & REFERRAL_TYPICAL else "other"
    ref_on_claim = referral if referral and r.random() < MIX[f"referral_copied_{ref_kind}"] else None
    return {"prior_auth_id": auth, "referring_provider_id": referral}, auth_on_claim, ref_on_claim


def build_claims(
    patients: dict[str, Patient],
    encounters: list[Encounter],
    *,
    n: int = 2000,
    seed: int = 42,
    now: datetime | None = None,
    holdout_share: float = 0.20,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rng = random.Random(seed)
    pool = sorted((e for e in encounters if eligible(e) and e.patient_id in patients), key=lambda e: e.start)
    chosen = sorted(rng.sample(pool, min(n, len(pool))), key=lambda e: e.start)

    late = set(rng.sample(range(len(chosen)), int(round(len(chosen) * MIX["late_filing"]))))
    seen_wellness: set[tuple[str, str]] = set()
    claims = []
    for i, enc in enumerate(chosen):
        patient = patients[enc.patient_id]
        insurer = assign_insurer(enc)
        r = _rng_for(str(seed), enc.id)

        days_ago = r.randint(95, 150) if i in late else 85 - int(i / max(1, len(chosen) - 1) * 83)
        service = (now - timedelta(days=days_ago)).date()
        age = patient.age_on(service)
        first_wellness = enc.encounter_class == "wellness" and (enc.patient_id, insurer) not in seen_wellness
        if enc.encounter_class == "wellness":
            seen_wellness.add((enc.patient_id, insurer))

        dx, lines = services_for(enc, patient, r, age, first_wellness)
        records, auth, ref = _paperwork({ln["hcpcs"] for ln in lines}, enc.encounter_class, r)
        claims.append({
            "_id": f"clm_{i + 1:05d}",
            "insurer": insurer,
            "patient": {
                "name": patient.name,
                "dob": patient.birthdate[:10],
                "member_id": member_id(patient.id, insurer),
                "ssn": patient.ssn,
                "address": f"{patient.address}, {patient.city}, {patient.state} {patient.zip}".strip(),
                "phone": phone(patient.id),
            },
            "service_date": service.isoformat(),
            "encounter_type": enc.encounter_class,
            "diagnosis_codes": dx,
            "lines": lines,
            "total_charge_usd": round(sum(ln["charge_usd"] for ln in lines), 2),
            "prior_auth_id": auth,
            "referring_provider_id": ref,
            "records": records,
            "notes": f"Encounter: {enc.description}. Reason: {enc.reason or 'routine visit'}.",
            "holdout": False,
            "created_at": now.isoformat(),
            "synthea_encounter_id": enc.id,
        })

    for idx in rng.sample(range(len(claims)), int(round(len(claims) * holdout_share))):
        claims[idx]["holdout"] = True

    unknown = {dx for c in claims for dx in c["diagnosis_codes"]} - set(ICD10)
    assert not unknown, f"diagnosis codes outside the code set: {unknown}"
    return claims
