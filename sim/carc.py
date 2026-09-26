"""Standard denial codes: CARC (claim adjustment reason) and RARC (remark) texts.

Shortened from the public X12 code lists. These are what a real remittance
(X12 835) would carry back to the hospital.
"""
from __future__ import annotations

CARC: dict[str, str] = {
    "CO-4": "The procedure code is inconsistent with the modifier used, or a required modifier is missing.",
    "CO-16": "Claim/service lacks information or has submission/billing error(s).",
    "CO-18": "Exact duplicate claim/service.",
    "CO-29": "The time limit for filing has expired.",
    "CO-50": "These are non-covered services because this is not deemed a 'medical necessity' by the payer.",
    "CO-96": "Non-covered charge(s).",
    "CO-97": "The benefit for this service is included in the payment/allowance for another service/procedure that has already been adjudicated.",
    "CO-151": "Payment adjusted because the payer deems the information submitted does not support this many/frequency of services.",
}

RARC: dict[str, str] = {
    "M62": "Missing/incomplete/invalid treatment authorization code.",
    "M80": "Not covered when performed during the same session/date as a previously processed service for the patient.",
    "N130": "Consult plan benefit documents/guidelines for information about restrictions for this service.",
    "N286": "Missing/incomplete/invalid referring provider primary identifier.",
    "N362": "The number of days or units of service exceeds our acceptable maximum.",
    "N822": "Missing procedure modifier(s).",
}


def denial_text(carc: str, rarc: str | None, line: dict | None = None) -> str:
    """The human-readable denial the hospital sees. Never contains dates or patient data."""
    from forge.codes import describe

    code = f"{carc} / {rarc}" if rarc else carc
    parts = [f"{code}: {CARC[carc]}"]
    if rarc:
        parts.append(RARC[rarc])
    if line:
        parts.append(f"Service line {line['line_no']} ({line['hcpcs']} {describe(line['hcpcs'])}).")
    return " ".join(parts)
