"""Frozen document shapes (docs/PLAN.md, Member B specs).

Frozen at the 11:00 whiteboard. Only B edits this file after that, and only
after telling the team. Downstream code may treat documents as plain dicts;
these Pydantic models are the single source of truth for the *shape*.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VerdictLabel(str, Enum):
    LEGITIMATE = "legitimate"
    WRONGFUL_POLICY = "wrongful_policy"
    WRONGFUL_BULK = "wrongful_bulk"
    NEEDS_REVIEW = "needs_review"


class RuleStatus(str, Enum):
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


class AppealMode(str, Enum):
    DRAFT_ONLY = "draft_only"
    AUTO_FILE = "auto_file"


class AppealStatus(str, Enum):
    DRAFTED = "drafted"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"  # a human approved it in the live view; the appeal worker files it
    FILED = "filed"


class AppealOutcome(str, Enum):
    OVERTURNED = "overturned"
    UPHELD = "upheld"


class EventType(str, Enum):
    RULE_PROPOSED = "rule_proposed"
    RULE_PROMOTED = "rule_promoted"
    RULE_REJECTED = "rule_rejected"
    RULE_RETIRED = "rule_retired"
    PROFILE_CHANGED = "profile_changed"
    PERMISSION_CHANGED = "permission_changed"
    GUARDRAIL_ADDED = "guardrail_added"
    ROLLBACK = "rollback"
    PHI_BLOCKED = "phi_blocked"


class EventActor(str, Enum):
    EVOLVER = "evolver"
    JUDGE = "judge"
    VALIDATOR = "validator"
    FIREWALL = "firewall"
    HUMAN = "human"


class Insurer(str, Enum):
    PAYER_A = "payer_a"
    PAYER_B = "payer_b"
    PAYER_C = "payer_c"


# ---------------------------------------------------------------------------
# Nested / leaf models
# ---------------------------------------------------------------------------


class Patient(BaseModel):
    """Encrypted at rest via Queryable Encryption (see firewall/encryption.py)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    dob: str  # YYYY-MM-DD
    member_id: str
    ssn: str
    address: str
    phone: str


class ClaimLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int
    hcpcs: str
    modifiers: list[str] = Field(default_factory=list)
    units: int = 1
    charge_usd: float


class ClaimRecords(BaseModel):
    """What the hospital has on file; fixes copy from here (never invent values)."""

    model_config = ConfigDict(extra="allow")

    prior_auth_id: Optional[str] = None
    referring_provider_id: Optional[str] = None


class Claim(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    insurer: str
    patient: Patient
    service_date: str
    encounter_type: str
    diagnosis_codes: list[str]
    lines: list[ClaimLine]
    total_charge_usd: float
    prior_auth_id: Optional[str] = None
    referring_provider_id: Optional[str] = None
    records: ClaimRecords = Field(default_factory=ClaimRecords)
    notes: Optional[str] = None
    holdout: bool = False
    created_at: str

    def model_dump_doc(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=False)
        data["_id"] = data.pop("id", data.get("_id"))
        return data


class TokenizedClaim(BaseModel):
    """What the LLM is allowed to see. No real patient identifiers."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    insurer: str
    patient_token: str
    age_band: str
    state: str
    service_day: int
    encounter_type: str
    diagnosis_codes: list[str]
    lines: list[ClaimLine]
    total_charge_usd: float
    prior_auth_id: Optional[str] = None
    referring_provider_id: Optional[str] = None
    records: ClaimRecords = Field(default_factory=ClaimRecords)
    notes_redacted: Optional[str] = None
    holdout: bool = False


class VerdictEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    clause_ids: list[str] = Field(default_factory=list)
    comparable_claim_ids: list[str] = Field(default_factory=list)
    pattern_stats: dict[str, Any] = Field(default_factory=dict)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: Literal["legitimate", "wrongful_policy", "wrongful_bulk", "needs_review"]
    confidence: float
    reason: str
    evidence: VerdictEvidence = Field(default_factory=VerdictEvidence)
    suggested_rule: Optional[dict[str, Any]] = None
    model: str = "sonnet-5"
    created_at: str = Field(default_factory=now_iso)


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    claim_id: str
    insurer: str
    attempt: int = 1
    status: Literal["paid", "denied"]
    carc: Optional[str] = None
    rarc: Optional[str] = None
    denial_text: Optional[str] = None
    paid_amount_usd: float = 0.0
    submitted_at: str
    adjudicated_at: str
    latency_ms: int = 0
    verdict: Optional[Verdict] = None
    holdout: bool = False


class RuleReplay(BaseModel):
    model_config = ConfigDict(extra="allow")

    prevented: int = 0
    false_blocks: int = 0
    precision: float = 0.0


class Rule(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    insurer: str
    version: int = 1
    status: Literal["candidate", "shadow", "active", "retired", "rejected"]
    condition: dict[str, Any]
    fix: dict[str, Any]
    evidence_ids: list[str] = Field(default_factory=list)
    replay: Optional[RuleReplay] = None
    hit_count: int = 0
    recent_outcomes: list[bool] = Field(default_factory=list)
    last_used_at: Optional[str] = None
    parent_version: Optional[int] = None
    created_by: str = "judge"
    created_at: str = Field(default_factory=now_iso)


class ReplayStats(BaseModel):
    model_config = ConfigDict(extra="allow")

    prevented: int
    false_blocks: int
    precision: float
    sample_size: int
    decision: Literal["promote", "reject", "retire"]
    false_block_rate: float = 0.0


class Appeal(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    claim_id: str
    insurer: str
    verdict: str
    confidence: float
    evidence: dict[str, Any] = Field(default_factory=dict)
    letter_tokenized: str
    mode: Literal["draft_only", "auto_file"]
    status: Literal["drafted", "pending_approval", "approved", "filed", "filing_failed"] = "drafted"
    outcome: Optional[Literal["overturned", "upheld"]] = None
    recovered_usd: float = 0.0
    filed_at: Optional[str] = None


class ContextPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    policy_clauses: int = 3
    paid_comparables: int = 3
    include_pattern_stats: bool = True


class JudgeConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    min_confidence: float = 0.75
    bulk_window_sec: int = 60
    bulk_min_identical: int = 5  # matches default_harness_profile


class AppealStrategy(BaseModel):
    model_config = ConfigDict(extra="allow")

    lead_with: Literal["clause", "paid_comparables", "pattern_stats"] = "clause"
    quote_clause_verbatim: bool = False
    include_pattern_stats: bool = True


class Permissions(BaseModel):
    model_config = ConfigDict(extra="allow")

    appeals: Literal["draft_only", "auto_file"] = "draft_only"
    earned_at: Optional[str] = None


class Guardrails(BaseModel):
    model_config = ConfigDict(extra="allow")

    fixed: list[str] = Field(default_factory=lambda: ["no_upcoding", "no_phi_to_llm"])
    learned: list[str] = Field(default_factory=list)


class HarnessProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    version: int = 1
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    appeal_strategy: AppealStrategy = Field(default_factory=AppealStrategy)
    permissions: Permissions = Field(default_factory=Permissions)
    guardrails: Guardrails = Field(default_factory=Guardrails)
    updated_by: str = "system"
    last_event_id: Optional[str] = None


class Event(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(alias="_id")
    ts: str = Field(default_factory=now_iso)
    insurer: str
    actor: Literal["evolver", "judge", "validator", "firewall", "human"]
    type: str
    before: Optional[dict[str, Any]] = None
    after: Optional[dict[str, Any]] = None
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    reverted: bool = False


class PhiIncident(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str = Field(default_factory=now_iso)
    insurer: str
    claim_id: Optional[str] = None
    direction: Literal["request", "response"]
    pattern: str
    action: Literal["blocked"] = "blocked"
    guardrail_proposed: Optional[str] = None


# ---------------------------------------------------------------------------
# Builders (dict-shaped, matching what C's orchestrator already uses)
# ---------------------------------------------------------------------------


def _default_appeal_mode() -> str:
    """DEFAULT_APPEAL_MODE=auto_file lets a demo file appeals without approval clicks.
    Anything else (or unset) keeps the safe default: a human approves every appeal."""
    mode = os.environ.get("DEFAULT_APPEAL_MODE", "").strip()
    return mode if mode in {m.value for m in AppealMode} else AppealMode.DRAFT_ONLY.value


def default_harness_profile(insurer: str) -> dict[str, Any]:
    return {
        "_id": insurer,
        "version": 1,
        "context_policy": {
            "policy_clauses": 3,
            "paid_comparables": 3,
            "include_pattern_stats": True,
        },
        "judge": {
            "min_confidence": 0.75,
            "bulk_window_sec": 60,
            # At the demo's 2 claims/s, Payer B's bursts reach about 8 identical denials a
            # minute, so 20 never fired. 5 is the Evolver's lower bound; the Judge also
            # requires a sub-2-second median latency before calling a burst bulk.
            "bulk_min_identical": 5,
        },
        "appeal_strategy": {
            "lead_with": "clause",
            "quote_clause_verbatim": False,
            "include_pattern_stats": True,
        },
        "permissions": {
            "appeals": _default_appeal_mode(),
            "earned_at": None,
        },
        "guardrails": {
            "fixed": ["no_upcoding", "no_phi_to_llm"],
            "learned": [],
        },
        "updated_by": "system",
        "last_event_id": None,
    }


def new_event(
    *,
    insurer: str,
    actor: str,
    type_: str,
    reason: str,
    before: dict | None = None,
    after: dict | None = None,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "_id": new_id("evt"),
        "ts": now_iso(),
        "insurer": insurer,
        "actor": actor,
        "type": type_,
        "before": before,
        "after": after,
        "reason": reason,
        "evidence_ids": evidence_ids or [],
        "reverted": False,
    }


def new_verdict(
    *,
    label: str,
    confidence: float,
    reason: str,
    evidence: dict[str, Any],
    suggested_rule: dict | None = None,
    model: str = "sonnet-5",
) -> dict[str, Any]:
    return {
        "label": label,
        "confidence": confidence,
        "reason": reason,
        "evidence": evidence,
        "suggested_rule": suggested_rule,
        "model": model,
        "created_at": now_iso(),
    }


def new_rule(
    *,
    insurer: str,
    condition: dict,
    fix: dict,
    evidence_ids: list[str],
    created_by: str = "judge",
) -> dict[str, Any]:
    return {
        "_id": new_id("rule"),
        "insurer": insurer,
        "version": 1,
        "status": RuleStatus.CANDIDATE.value,
        "condition": condition,
        "fix": fix,
        "evidence_ids": evidence_ids,
        "replay": None,
        "hit_count": 0,
        "recent_outcomes": [],
        "last_used_at": None,
        "parent_version": None,
        "created_by": created_by,
        "created_at": now_iso(),
    }


def new_appeal(
    *,
    claim_id: str,
    insurer: str,
    verdict: dict,
    letter_tokenized: str,
    mode: str,
) -> dict[str, Any]:
    return {
        "_id": new_id("apl"),
        "claim_id": claim_id,
        "insurer": insurer,
        "verdict": verdict["label"],
        "confidence": verdict["confidence"],
        "evidence": verdict["evidence"],
        "letter_tokenized": letter_tokenized,
        "mode": mode,
        "status": (
            AppealStatus.PENDING_APPROVAL.value
            if mode == AppealMode.DRAFT_ONLY.value
            else AppealStatus.DRAFTED.value
        ),
        "outcome": None,
        "recovered_usd": 0.0,
        "filed_at": None,
    }
