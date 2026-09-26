"""Payer C: policy drifter. 6 legitimate rules, 1 planted wrongful behavior.

Wrongful: denies emergency and clinic visits for chest or abdominal pain as
not medically necessary, against the prudent-layperson standard in clause 5.

The policy-change button moves Payer C from version 1 to version 2: three
hidden rules swap (c_legit_01..03 are replaced by c_legit_07..09) and the
published policy gets new text for clauses 4 to 7. The biggest change is that
clinic visits now need a referring provider, so acceptance dips until the
agent learns it.
"""
from __future__ import annotations

from .base import (
    HiddenRule,
    billed_together,
    drop_fix,
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
PRIOR_AUTH_V1 = {"J9355", "E0601", "G0439"}
REFERRING_V1 = {"C8901", "C8908"}
PRIOR_AUTH_V2 = {"C8901", "C8908", "G0279"}
REFERRING_V2 = {"G0463", "G0283", "E0601"}


def _acute_symptom_visit(claim):
    return first_line(claim, {"G0383", "G0463"}) if has_dx(claim, {"R07.9", "R10.9"}) else None


_WRONGFUL = HiddenRule("c_wrong_01", "wrongful", _acute_symptom_visit, "CO-50", "N130",
                       "Denies visits for chest or abdominal pain as not medically necessary.",
                       probability=0.8, contradicts_clause=5)

_SHARED = [
    HiddenRule("c_legit_04", "legit", billed_together({"G0438"}, "G0444"), "CO-97", "M80",
               "Depression screening billed with the initial wellness visit (clause 3).",
               fix=drop_fix({"G0438"}, "G0444")),
    HiddenRule("c_legit_05", "legit", missing_modifier("G0463", {"25"}, when=AWV), "CO-4", "N822",
               "Clinic visit billed with an annual wellness visit without modifier 25 (clause 3).",
               fix=modifier_fix("G0463", "25", AWV)),
    HiddenRule("c_legit_06", "legit", units_over("G0378", 24), "CO-151", "N362",
               "Observation over 24 hours on one line (clause 6).",
               fix=units_fix("G0378", 24)),
]

RULES_V1: list[HiddenRule] = [
    HiddenRule("c_legit_01", "legit", missing_prior_auth(PRIOR_AUTH_V1), "CO-16", "M62",
               "Prior authorization number missing for trastuzumab, CPAP or G0439 (clause 4, v1).",
               fix=pa_fix(PRIOR_AUTH_V1)),
    HiddenRule("c_legit_02", "legit", missing_referring(REFERRING_V1), "CO-16", "N286",
               "Referring provider missing for advanced imaging (clause 7, v1).",
               fix=ref_fix(REFERRING_V1)),
    HiddenRule("c_legit_03", "legit", units_over("J1885", 4), "CO-151", "N362",
               "Ketorolac over 4 units on one line (clause 6, v1).",
               fix=units_fix("J1885", 4)),
    *_SHARED,
    _WRONGFUL,
]

RULES_V2: list[HiddenRule] = [
    HiddenRule("c_legit_07", "legit", missing_prior_auth(PRIOR_AUTH_V2), "CO-16", "M62",
               "Prior authorization number missing for imaging, including tomosynthesis (clause 4, v2).",
               fix=pa_fix(PRIOR_AUTH_V2)),
    HiddenRule("c_legit_08", "legit", missing_referring(REFERRING_V2), "CO-16", "N286",
               "Referring provider missing for a clinic visit, therapy or CPAP (clause 7, v2).",
               fix=ref_fix(REFERRING_V2)),
    HiddenRule("c_legit_09", "legit", units_over("G0108", 4), "CO-151", "N362",
               "Diabetes training over 4 units on one line (clause 6, v2).",
               fix=units_fix("G0108", 4)),
    *_SHARED,
    _WRONGFUL,
]
