"""Tokenize claims before any LLM call.

Real patient fields become ``PATIENT_NNNN``, age bands, state-only address,
and relative service days. Mapping is stored in ``phi_tokens`` (encrypted).
"""
from __future__ import annotations

import hashlib
import logging
import random
import re
from datetime import date, datetime
from typing import Any

from common import db as dbmod
from firewall.guardrails import learned_from_profile

log = logging.getLogger(__name__)

_STATE_RE = re.compile(r",\s*([A-Z]{2})\s*(?:\d{5}(?:-\d{4})?)?$")
_STATE_FALLBACK = re.compile(r"\b([A-Z]{2})\b")


def _age_band(dob: str) -> str:
    born = datetime.strptime(dob[:10], "%Y-%m-%d").date()
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    lower = max(0, (age // 10) * 10)
    return f"{lower}-{lower + 9}"


def _extract_state(address: str) -> str:
    match = _STATE_RE.search(address or "")
    if match:
        return match.group(1)
    # last 2-letter token that looks like a state
    parts = _STATE_FALLBACK.findall(address or "")
    return parts[-1] if parts else "XX"


def _stable_token(member_id: str) -> str:
    digest = hashlib.sha256(member_id.encode()).hexdigest()
    return f"PATIENT_{int(digest[:8], 16) % 10_000:04d}"


def _service_day(claim: dict) -> int:
    created_raw = claim.get("created_at", "")
    service_raw = claim.get("service_date", "")
    try:
        if "T" in created_raw:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).date()
        else:
            created = datetime.strptime(created_raw[:10], "%Y-%m-%d").date()
        service = datetime.strptime(service_raw[:10], "%Y-%m-%d").date()
        return (service - created).days
    except (ValueError, TypeError):
        return 0


def _store_token_mapping(token: str, patient: dict, claim_id: str) -> None:
    """Persist token → patient reference in ``phi_tokens``.

    ``patient_ref`` is encrypted as one value before it is written; if encryption
    isn't configured the mapping is not stored at all (never in plaintext), and
    render_letter simply leaves the token in place.
    """
    try:
        db = dbmod.get_db("firewall")
        existing = db[dbmod.PHI_TOKENS].find_one({"_id": token}, {"_id": 1})
        if existing:
            db[dbmod.PHI_TOKENS].update_one(
                {"_id": token},
                {"$addToSet": {"claim_ids": claim_id}},
            )
            return
        from firewall.encryption import detect_mode, encrypt_object

        if detect_mode() == "none":
            log.debug("phi_tokens write skipped (encryption not configured)")
            return
        doc = {
            "_id": token,
            "claim_ids": [claim_id],
            "patient_ref": encrypt_object({
                "name": patient.get("name"),
                "dob": patient.get("dob"),
                "member_id": patient.get("member_id"),
                "ssn": patient.get("ssn"),
                "address": patient.get("address"),
                "phone": patient.get("phone"),
            }),
            # hashed member_id for equality lookup without storing plaintext index
            "member_id_hash": hashlib.sha256(
                patient.get("member_id", "").encode()
            ).hexdigest(),
        }
        db[dbmod.PHI_TOKENS].insert_one(doc)
    except Exception as exc:  # noqa: BLE001 — tokenization must still succeed
        log.debug("phi_tokens write skipped (%s)", exc)


def _redact_notes(notes: str | None, learned: set[str]) -> str | None:
    if not notes:
        return None
    if "drop_notes_field" in learned:
        return None
    if "redact_notes_strict" in learned:
        return re.sub(r"\S", "*", notes)
    # Default per PLAN.md: notes removed unless the profile allows a redacted copy
    return None


def tokenize(claim: dict, *, profile: dict | None = None) -> dict:
    """Claim → TokenizedClaim. Never lets patient identifiers past this call."""
    patient = claim.get("patient") or {}
    if not patient.get("member_id"):
        # Deterministic fallback so a malformed claim still tokenizes
        member_id = f"unknown-{claim.get('_id', random.randint(0, 9999))}"
        patient = {**patient, "member_id": member_id, "dob": patient.get("dob", "1970-01-01")}

    token = _stable_token(patient["member_id"])
    _store_token_mapping(token, patient, claim.get("_id", ""))

    learned = learned_from_profile(profile)
    notes_redacted = _redact_notes(claim.get("notes"), learned)

    return {
        "_id": claim["_id"],
        "insurer": claim["insurer"],
        "patient_token": token,
        "age_band": _age_band(patient.get("dob", "1970-01-01")),
        "state": _extract_state(patient.get("address", "")),
        "service_day": _service_day(claim),
        "encounter_type": claim.get("encounter_type"),
        "diagnosis_codes": list(claim.get("diagnosis_codes", [])),
        "lines": list(claim.get("lines", [])),
        "total_charge_usd": claim.get("total_charge_usd"),
        "prior_auth_id": claim.get("prior_auth_id"),
        "referring_provider_id": claim.get("referring_provider_id"),
        "records": claim.get("records", {}),
        "notes_redacted": notes_redacted,
        "holdout": claim.get("holdout", False),
    }


def lookup_patient(token: str) -> dict[str, Any] | None:
    """Resolve a ``PATIENT_NNNN`` token back to the stored patient_ref."""
    try:
        db = dbmod.get_db("firewall")
        doc = db[dbmod.PHI_TOKENS].find_one({"_id": token})
        if doc:
            ref = doc.get("patient_ref")
            if type(ref).__name__ == "Binary":  # encrypted by _store_token_mapping
                from firewall.encryption import decrypt_value

                ref = decrypt_value(ref)
            return ref if isinstance(ref, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.debug("token lookup failed (%s)", exc)
    return None
