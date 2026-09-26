from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from common.models import Claim
from forge import synthea
from forge.claims import build_claims
from forge.codes import ICD10, PROCEDURES
from forge.phi import plant_phi, value_hash
from sim.rules.base import service_day

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
MEMBER_ID = {"payer_a": r"PAM\d{9}", "payer_b": r"BXH-\d{8}", "payer_c": r"PC\d{10}"}
REASONS = ["", "Hypertension", "Acute bronchitis (disorder)", "Chest pain (finding)", "Diabetes mellitus type 2",
           "Sprain of ankle", "Abdominal pain", "Chronic kidney disease stage 4", "Screening for malignant neoplasm of colon",
           "Neoplasm of breast (disorder)", "Sleep apnea (disorder)", "Acute viral pharyngitis (disorder)"]
CLASSES = ["wellness", "ambulatory", "emergency", "outpatient", "inpatient", "urgentcare"]


def fake_synthea(n_patients: int = 30, per_patient: int = 12):
    patients, encounters = {}, []
    for p in range(n_patients):
        pid = f"p{p:03d}"
        patients[pid] = synthea.Patient(
            id=pid, first=f"Given{chr(65 + p % 26)}", last=f"Family{chr(65 + p // 26)}", birthdate=f"19{40 + p % 50}-0{1 + p % 9}-1{p % 9}",
            gender="F" if p % 2 else "M", ssn=f"999-{10 + p:02d}-{1000 + p:04d}", address=f"{p} Main St", city="Boston",
            state="MA", zip="02118", conditions=["Hypertension (disorder)"] if p % 3 == 0 else [])
        for e in range(per_patient):
            encounters.append(synthea.Encounter(
                id=f"e{p:03d}_{e:02d}", patient_id=pid, start=f"20{10 + e % 12:02d}-{1 + (p + e) % 12:02d}-15T09:00:00Z",
                encounter_class=CLASSES[(p + e) % len(CLASSES)], description="Encounter for check up",
                reason=REASONS[(p * 7 + e) % len(REASONS)]))
    return patients, encounters


@pytest.fixture(scope="module")
def claims():
    patients, encounters = fake_synthea()
    return build_claims(patients, encounters, n=300, now=NOW)


def test_code_set_is_40():
    assert len(ICD10) == 20 and len(PROCEDURES) == 20
    assert all(re.fullmatch(r"[A-Z]\d{4}", code) for code in PROCEDURES)  # HCPCS Level II, no CPT


def test_claims_match_the_frozen_model(claims):
    assert len(claims) == 300
    for c in claims:
        Claim.model_validate(c)
        assert set(c["diagnosis_codes"]) <= set(ICD10)
        assert {ln["hcpcs"] for ln in c["lines"]} <= set(PROCEDURES)
        assert c["total_charge_usd"] == pytest.approx(sum(ln["charge_usd"] for ln in c["lines"]))


def test_identifiers_are_guard_safe(claims):
    for c in claims:
        assert re.fullmatch(MEMBER_ID[c["insurer"]], c["patient"]["member_id"])
        for value in (c["prior_auth_id"], c["records"]["prior_auth_id"]):
            assert value is None or re.fullmatch(r"PA-[A-Z0-9]{6}", value)
        for value in (c["referring_provider_id"], c["records"]["referring_provider_id"]):
            assert value is None or re.fullmatch(r"PRV-\d{6}", value)
        assert not re.search(r"\d", c["patient"]["name"])


def test_holdout_share(claims):
    assert sum(c["holdout"] for c in claims) == 60


def test_service_dates_are_recent_with_some_late(claims):
    days = [service_day(c) for c in claims]
    assert all(-150 <= d <= -2 for d in days)
    assert any(d < -90 for d in days)


def test_all_insurers_present(claims):
    assert {c["insurer"] for c in claims} == {"payer_a", "payer_b", "payer_c"}


def test_phi_planted_in_5_percent_and_hashed(claims):
    planted = [dict(c) for c in claims]
    before = {c["_id"]: c["notes"] for c in planted}
    canaries = plant_phi(planted, share=0.05)
    assert len(canaries) == 15
    changed = {c["_id"] for c in planted if c["notes"] != before[c["_id"]]}
    assert changed == {c["claim_id"] for c in canaries}
    for canary in canaries:
        c = next(x for x in planted if x["_id"] == canary["claim_id"])
        assert canary["type"] == "phi_canary" and canary["value_hashes"]
        joined = str(canary)
        for field in ("name", "ssn", "phone", "member_id"):
            assert c["patient"][field] not in joined  # only hashes are stored
        if "ssn" in canary["kinds"]:
            assert value_hash("ssn", c["patient"]["ssn"]) in canary["value_hashes"]


def test_unplanted_notes_have_no_phi(claims):
    for c in claims:
        p = c["patient"]
        for value in (p["name"], p["ssn"], p["phone"], p["member_id"]):
            assert value not in c["notes"]


@pytest.mark.skipif(not synthea.available(), reason="Synthea sample not downloaded (python -m forge download)")
def test_real_synthea_build():
    patients = synthea.read_patients()
    encounters = synthea.read_encounters()
    built = build_claims(patients, encounters, n=2000, now=NOW)
    assert len(built) == 2000
    assert sum(c["holdout"] for c in built) == 400
    assert len(plant_phi(built)) == 100
    for c in built[:200]:
        Claim.model_validate(c)
