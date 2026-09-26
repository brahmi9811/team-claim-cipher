"""Payer A: strict but fair. 8 legitimate rules, 1 planted wrongful behavior.

Wrongful: denies chronic-condition clinic visits, which clause 7 covers
without medical-necessity review, as "not medically necessary" (CO-50) in part
of the cases (about 10% of Payer A's claims).
"""
from __future__ import annotations

from .base import (
    HiddenRule,
    billed_together,
    drop_fix,
    filed_late,
    first_line,
    has_dx,
    missing_modifier,
    missing_prior_auth,
    missing_referring,
    modifier_fix,
    pa_fix,
    ref_fix,
    units_fix,
    units_over,
)

AWV = {"G0438", "G0439"}
ORIGIN_DESTINATION = {"RH", "HR", "RE", "ER", "RN", "NR", "EH", "HE", "SH", "HS"}
PRIOR_AUTH = {"G0439", "C8901", "C8908", "E0601", "J9355"}
REFERRING = {"E0601", "G0283", "C8901", "C8908"}


CHRONIC = {"N18.4", "E11.9", "I10", "E78.5"}


def _chronic_visit(claim):
    """Clause 7: a clinic visit (G0463) for a chronic condition."""
    return first_line(claim, {"G0463"}) if has_dx(claim, CHRONIC) else None


RULES_V1: list[HiddenRule] = [
    HiddenRule("a_legit_01", "legit", filed_late(90), "CO-29", None,
               "Filed more than 90 days after the service (clause 11). Not fixable by the scrubber."),
    HiddenRule("a_legit_02", "legit", missing_prior_auth(PRIOR_AUTH), "CO-16", "M62",
               "Prior authorization number missing for G0439, advanced imaging, CPAP or trastuzumab (clause 4).",
               fix=pa_fix(PRIOR_AUTH)),
    HiddenRule("a_legit_03", "legit", missing_referring(REFERRING), "CO-16", "N286",
               "Referring provider missing for DME, therapy or advanced imaging (clause 5).",
               fix=ref_fix(REFERRING)),
    HiddenRule("a_legit_04", "legit", missing_modifier("G0463", {"25"}, when=AWV), "CO-4", "N822",
               "Clinic visit billed with an annual wellness visit without modifier 25 (clause 9).",
               fix=modifier_fix("G0463", "25", AWV)),
    HiddenRule("a_legit_05", "legit", missing_modifier("A0429", ORIGIN_DESTINATION), "CO-4", "N822",
               "Ambulance line without an origin/destination modifier (clause 8).",
               fix=modifier_fix("A0429", "RH")),
    HiddenRule("a_legit_06", "legit", billed_together({"G0438"}, "G0444"), "CO-97", "M80",
               "Depression screening billed with the initial wellness visit (clause 3).",
               fix=drop_fix({"G0438"}, "G0444")),
    HiddenRule("a_legit_07", "legit", units_over("J1885", 4), "CO-151", "N362",
               "Ketorolac over 4 units on one line (clause 6).",
               fix=units_fix("J1885", 4)),
    HiddenRule("a_legit_08", "legit", units_over("G0283", 4), "CO-151", "N362",
               "Electrical stimulation over 4 units on one line (clause 6).",
               fix=units_fix("G0283", 4)),
    HiddenRule("a_wrong_01", "wrongful", _chronic_visit, "CO-50", "N130",
               "Denies covered chronic-condition clinic visits as not medically necessary.",
               probability=0.45, contradicts_clause=7),
]
