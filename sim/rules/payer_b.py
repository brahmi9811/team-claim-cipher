"""Payer B: algorithmic denier. 6 legitimate rules, 3 planted wrongful behaviors.

All three wrongful behaviors come from the same automated system: they are
decided within 2 seconds and released in batches (shared timestamp and batch
id), while legitimate denials take a normal review time.

- b_wrong_01: auto-denies imaging claims over $2,000 as CO-50.
- b_wrong_02: denies covered follow-up visits (diagnosis Z09) as CO-50.
- b_wrong_03: returns the wrong reason code: "missing authorization" for a
  claim that has its authorization number on it.
"""
from __future__ import annotations

from forge.codes import IMAGING

from .base import (
    HiddenRule,
    billed_together,
    blank,
    drop_fix,
    filed_late,
    first_line,
    has_dx,
    lines,
    missing_modifier,
    missing_prior_auth,
    missing_referring,
    modifier_fix,
    pa_fix,
    ref_fix,
    units_fix,
    units_over,
)

ORIGIN_DESTINATION = {"RH", "HR", "RE", "ER", "RN", "NR", "EH", "HE", "SH", "HS"}
PRIOR_AUTH = {"C8901", "C8908", "J9355"}
REFERRING = {"G0463", "G0283", "E0601"}
BULK_THRESHOLD_USD = 2000.0


def _expensive_imaging(claim):
    if float(claim.get("total_charge_usd") or 0) <= BULK_THRESHOLD_USD:
        return None
    return first_line(claim, IMAGING)


def _follow_up(claim):
    return (lines(claim) or [True])[0] if has_dx(claim, {"Z09"}) else None


def _wrong_reason_code(claim):
    if blank(claim.get("prior_auth_id")):
        return None
    return first_line(claim, PRIOR_AUTH) or (lines(claim) or [True])[0]


RULES_V1: list[HiddenRule] = [
    HiddenRule("b_legit_06", "legit", filed_late(120), "CO-29", None,
               "Filed more than 120 days after the service (clause 11). Not fixable by the scrubber."),
    HiddenRule("b_legit_01", "legit", missing_prior_auth(PRIOR_AUTH), "CO-16", "M62",
               "Prior authorization number missing for advanced imaging or trastuzumab (clause 4).",
               fix=pa_fix(PRIOR_AUTH)),
    HiddenRule("b_legit_02", "legit", missing_referring(REFERRING), "CO-16", "N286",
               "Referring provider missing for a clinic visit, therapy or CPAP (clause 5).",
               fix=ref_fix(REFERRING)),
    HiddenRule("b_legit_03", "legit", missing_modifier("A0429", ORIGIN_DESTINATION), "CO-4", "N822",
               "Ambulance line without an origin/destination modifier (clause 9).",
               fix=modifier_fix("A0429", "RH")),
    HiddenRule("b_legit_04", "legit", billed_together({"Q0084"}, "J7030"), "CO-97", "M80",
               "Saline billed with chemotherapy infusion (clause 10).",
               fix=drop_fix({"Q0084"}, "J7030")),
    HiddenRule("b_legit_05", "legit", units_over("G0108", 4), "CO-151", "N362",
               "Diabetes training over 4 units on one line (clause 7).",
               fix=units_fix("G0108", 4)),
    HiddenRule("b_wrong_01", "wrongful", _expensive_imaging, "CO-50", "N130",
               "Auto-denies imaging claims over $2,000 within 2 seconds, in batches.",
               contradicts_clause=6, bulk=True),
    HiddenRule("b_wrong_02", "wrongful", _follow_up, "CO-50", "N130",
               "Denies covered follow-up visits as not medically necessary.",
               probability=0.9, contradicts_clause=8, bulk=True),
    HiddenRule("b_wrong_03", "wrongful", _wrong_reason_code, "CO-16", "M62",
               "Says the authorization is missing when it is on the claim.",
               probability=0.40, contradicts_clause=4, bulk=True),
]
