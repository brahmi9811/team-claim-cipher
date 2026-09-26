"""Leak detector: every LLM request/response passes through ``guard``.

On a hit: block the call (raise ``PHILeak``), log ``phi_incidents``, and
propose a new guardrail for the Evolver.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from common import db as dbmod
from common.models import EventActor, EventType, new_event
from firewall.guardrails import propose_for_pattern
from firewall.tokenize import lookup_patient

log = logging.getLogger(__name__)

# Pattern checks: SSN, phone, email, DOB-like dates, member-ID-ish tokens.
# Member ID formats from Role A (sim/README): A PAM#########, B BXH-########, C PC##########
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("ssn", re.compile(r"\b\d{9}\b")),
    ("phone", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("dob", re.compile(r"\b(?:0[1-9]|1[0-2])[/.-](?:0[1-9]|[12]\d|3[01])[/.-](?:19|20)\d{2}\b")),
    ("dob", re.compile(r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")),
    ("member_id", re.compile(r"\b(?:MBR|MEMBER|MID)[-_ ]?[A-Z0-9]{6,}\b", re.I)),
    ("member_id", re.compile(r"\bPAM\d{9}\b")),
    ("member_id", re.compile(r"\bBXH-\d{8}\b")),
    ("member_id", re.compile(r"\bPC\d{10}\b")),
]


class PHILeak(Exception):
    """Raised when a request/response contains PHI. Callers must not proceed."""

    def __init__(self, pattern: str, *, snippet: str = ""):
        self.pattern = pattern
        self.snippet = snippet
        super().__init__(
            f"PHI pattern '{pattern}' found in text sent to or from the LLM"
        )


def _patient_values_for_claim(claim_id: str | None) -> list[str]:
    """Decrypt-in-memory exact values for this patient (firewall only)."""
    if not claim_id:
        return []
    values: list[str] = []
    try:
        db = dbmod.get_db("firewall")
        # Prefer phi_tokens via claim_ids
        token_doc = db[dbmod.PHI_TOKENS].find_one({"claim_ids": claim_id})
        if token_doc and token_doc.get("patient_ref"):
            ref = token_doc["patient_ref"]
            for key in ("name", "dob", "member_id", "ssn", "phone", "address"):
                val = ref.get(key)
                if val:
                    values.append(str(val))
            return values

        claim = db[dbmod.CLAIMS].find_one({"_id": claim_id})
        if claim and claim.get("patient"):
            p = claim["patient"]
            for key in ("name", "dob", "member_id", "ssn", "phone", "address"):
                val = p.get(key)
                if val:
                    values.append(str(val))
    except Exception as exc:  # noqa: BLE001
        log.debug("exact-match lookup skipped (%s)", exc)
    return values


def _values_from_token_mentions(text: str) -> list[str]:
    """If the text already contains PATIENT_NNNN, pull those refs for exact match."""
    values: list[str] = []
    for token in re.findall(r"PATIENT_\d{4}", text):
        ref = lookup_patient(token)
        if not ref:
            continue
        for key in ("name", "dob", "member_id", "ssn", "phone", "address"):
            val = ref.get(key)
            if val:
                values.append(str(val))
    return values


def _find_leak(text: str, claim_id: str | None) -> tuple[str, str] | None:
    """Return ``(pattern, snippet)`` on the first hit, else None."""
    for name, regex in _PATTERNS:
        match = regex.search(text)
        if match:
            # Avoid flagging our own PATIENT_NNNN tokens as member_ids
            if name == "member_id" and match.group(0).upper().startswith("PATIENT"):
                continue
            # Avoid flagging bare 9-digit numbers that are clearly amounts etc. —
            # still flag; planted SSNs are the point of the red-team test.
            return name, match.group(0)

    exact_values = _patient_values_for_claim(claim_id) + _values_from_token_mentions(text)
    for val in exact_values:
        if len(val) >= 3 and val in text:
            # Don't flag age bands or state codes accidentally
            if re.fullmatch(r"\d{2}-\d{2}", val) or re.fullmatch(r"[A-Z]{2}", val):
                continue
            return "exact_phi", val[:32]

    return None


def _log_incident(
    *,
    insurer: str,
    claim_id: str | None,
    direction: str,
    pattern: str,
    guardrail: str,
) -> None:
    incident = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "insurer": insurer,
        "claim_id": claim_id,
        "direction": direction,
        "pattern": pattern,
        "action": "blocked",
        "guardrail_proposed": guardrail,
    }
    try:
        import os

        if not os.environ.get("MONGODB_URI"):
            log.info(
                "PHI blocked (%s/%s) — no MONGODB_URI, incident not persisted",
                pattern,
                direction,
            )
            return
        db = dbmod.get_db("firewall")
        db[dbmod.PHI_INCIDENTS].insert_one(incident)
        event = new_event(
            insurer=insurer,
            actor=EventActor.FIREWALL.value,
            type_=EventType.PHI_BLOCKED.value,
            reason=f"Blocked {direction} leak matching '{pattern}'",
            after={"pattern": pattern, "guardrail_proposed": guardrail},
            evidence_ids=[claim_id] if claim_id else [],
        )
        db[dbmod.HARNESS_EVENTS].insert_one(event)
    except Exception as exc:  # noqa: BLE001
        log.debug("Failed to log phi_incident: %s", exc)


def guard(
    text: str,
    *,
    direction: str = "request",
    insurer: str = "unknown",
    claim_id: str | None = None,
    profile: dict[str, Any] | None = None,  # noqa: ARG001 — reserved for evolving rules
) -> str:
    """Scan ``text`` for PHI. Return it unchanged, or raise ``PHILeak``.

    ``direction`` is ``"request"`` or ``"response"``. Learned guardrails on
    the insurer profile can tighten redaction upstream in ``tokenize``; this
    function is the last line of defense on every LLM call.
    """
    if not text:
        return text

    hit = _find_leak(text, claim_id)
    if hit is None:
        return text

    pattern, snippet = hit
    guardrail = propose_for_pattern(pattern)
    _log_incident(
        insurer=insurer,
        claim_id=claim_id,
        direction=direction,
        pattern=pattern,
        guardrail=guardrail,
    )
    raise PHILeak(pattern, snippet=snippet)
