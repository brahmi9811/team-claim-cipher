"""Hidden rule format (docs/PLAN.md, "Specs A owns").

Each insurer has an ordered list of rules; the first match decides. No match
means paid. Agents never import this package: they only ever see the denial
codes the simulator returns.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

Line = dict
Claim = dict

# applies(claim) returns the offending service line, True for a claim-level
# match, or None/False for no match.
Applies = Callable[[Claim], "Line | bool | None"]


@dataclass(frozen=True)
class HiddenRule:
    id: str
    kind: Literal["legit", "wrongful"]
    applies: Applies
    carc: str
    rarc: Optional[str]
    description: str
    probability: float = 1.0
    contradicts_clause: Optional[int] = None  # wrongful rules: the clause number the denial violates
    bulk: bool = False  # Payer B's batch behavior: fast, grouped denials
    # How a perfect scrubber would prevent this denial, in B's rule-fix format.
    # Used only by sim.report and the tests to prove each legit rule is learnable.
    fix: dict = field(default_factory=dict)

    def fires(self, claim: Claim) -> "Line | bool | None":
        hit = self.applies(claim)
        if not hit:
            return None
        if self.probability < 1.0 and roll(claim_id(claim), self.id) >= self.probability:
            return None
        return hit


def roll(*parts: str) -> float:
    """Deterministic number in [0, 1): the same claim always gets the same outcome."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def claim_id(claim: Claim) -> str:
    return str(claim.get("_id") or claim.get("claim_id") or claim.get("id") or "")


# --- helpers the rule files share ------------------------------------------


def lines(claim: Claim) -> list[Line]:
    return list(claim.get("lines") or [])


def has_code(claim: Claim, codes: set[str]) -> bool:
    return any(ln.get("hcpcs") in codes for ln in lines(claim))


def first_line(claim: Claim, codes: set[str]) -> Line | None:
    return next((ln for ln in lines(claim) if ln.get("hcpcs") in codes), None)


def has_dx(claim: Claim, codes: set[str]) -> bool:
    return any(dx in codes for dx in claim.get("diagnosis_codes") or [])


def blank(value) -> bool:
    return value is None or value == ""


def missing_prior_auth(codes: set[str]) -> Applies:
    def applies(claim: Claim):
        return first_line(claim, codes) if blank(claim.get("prior_auth_id")) else None

    return applies


def missing_referring(codes: set[str]) -> Applies:
    def applies(claim: Claim):
        return first_line(claim, codes) if blank(claim.get("referring_provider_id")) else None

    return applies


def units_over(code: str, max_units: int) -> Applies:
    def applies(claim: Claim):
        return next((ln for ln in lines(claim) if ln.get("hcpcs") == code and int(ln.get("units", 1)) > max_units), None)

    return applies


def billed_together(primary: set[str], bundled: str) -> Applies:
    """NCCI-style edit: `bundled` is not paid on the same claim as any `primary` code."""

    def applies(claim: Claim):
        return first_line(claim, {bundled}) if has_code(claim, primary) else None

    return applies


def missing_modifier(code: str, modifiers: set[str], when: set[str] | None = None) -> Applies:
    """`code` needs one of `modifiers`; if `when` is given, only when one of those codes is also billed."""

    def applies(claim: Claim):
        if when is not None and not has_code(claim, when):
            return None
        for ln in lines(claim):
            if ln.get("hcpcs") == code and not set(ln.get("modifiers") or []) & modifiers:
                return ln
        return None

    return applies


def service_day(claim: Claim) -> int | None:
    """Days from the claim's creation back to the service (negative = in the past).

    Tokenized claims carry `service_day`; raw claims carry the dates.
    """
    if claim.get("service_day") is not None:
        return int(claim["service_day"])
    from datetime import datetime

    try:
        service = datetime.strptime(str(claim["service_date"])[:10], "%Y-%m-%d").date()
        created = datetime.fromisoformat(str(claim["created_at"]).replace("Z", "+00:00")).date()
        return (service - created).days
    except (KeyError, ValueError):
        return None


def filed_late(max_days: int) -> Applies:
    def applies(claim: Claim):
        day = service_day(claim)
        return day is not None and day < -max_days

    return applies


# --- the prevention rule a perfect scrubber would learn (B's common/rules.py format)


def _code_cond(codes: set[str] | str) -> dict:
    if isinstance(codes, str):
        return {"field": "lines.hcpcs", "op": "eq", "value": codes}
    return {"field": "lines.hcpcs", "op": "in", "value": sorted(codes)}


def pa_fix(codes: set[str]) -> dict:
    return {"condition": {"all": [_code_cond(codes), {"field": "prior_auth_id", "op": "missing"}]},
            "fix": {"action": "attach_prior_auth"}}


def ref_fix(codes: set[str]) -> dict:
    return {"condition": {"all": [_code_cond(codes), {"field": "referring_provider_id", "op": "missing"}]},
            "fix": {"action": "attach_referring_provider"}}


def units_fix(code: str, max_units: int) -> dict:
    return {"condition": {"all": [_code_cond(code), {"field": "lines.units", "op": "gt", "value": max_units}]},
            "fix": {"action": "split_units", "params": {"max_units": max_units}}}


def drop_fix(primary: set[str], bundled: str) -> dict:
    return {"condition": {"all": [_code_cond(bundled), _code_cond(primary)]},
            "fix": {"action": "drop_line", "params": {"hcpcs": bundled}}}


def modifier_fix(code: str, modifier: str, when: set[str] | None = None) -> dict:
    conds = [_code_cond(code)] + ([_code_cond(when)] if when else [])
    return {"condition": {"all": conds}, "fix": {"action": "add_modifier", "params": {"modifier": modifier}}}
