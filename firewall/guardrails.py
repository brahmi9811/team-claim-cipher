"""Learned + fixed guardrails the leak detector and tokenizer consult.

Fixed guardrails (``no_upcoding``, ``no_phi_to_llm``) can never be removed.
Learned ones are proposed by the Evolver / leak detector and stored on the
insurer's harness profile.
"""
from __future__ import annotations

from typing import Any

# Guardrails the Evolver is allowed to add (docs/PLAN.md limits table).
LEARNABLE_GUARDRAILS = (
    "drop_notes_field",
    "redact_notes_strict",
)

FIXED_GUARDRAILS = (
    "no_upcoding",
    "no_phi_to_llm",
)


def learned_from_profile(profile: dict[str, Any] | None) -> set[str]:
    if not profile:
        return set()
    learned = profile.get("guardrails", {}).get("learned", []) or []
    return {g for g in learned if g in LEARNABLE_GUARDRAILS}


def fixed_from_profile(profile: dict[str, Any] | None) -> set[str]:
    if not profile:
        return set(FIXED_GUARDRAILS)
    fixed = profile.get("guardrails", {}).get("fixed", []) or []
    return set(fixed) | set(FIXED_GUARDRAILS)


def propose_for_pattern(pattern: str) -> str:
    """Map a leak pattern to a learnable guardrail the Evolver may adopt."""
    if pattern in {"ssn", "phone", "email", "dob", "member_id", "name", "exact_phi"}:
        return "redact_notes_strict"
    if pattern == "notes_phi":
        return "drop_notes_field"
    return "redact_notes_strict"
