"""The 40-code set: 20 ICD-10-CM diagnosis codes and 20 HCPCS Level II procedure codes.

All public code sets. No CPT codes (AMA licensed). Descriptions are shortened.
Prices are one fictional hospital's chargemaster, so the same service always
has the same billed amount.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Procedure:
    hcpcs: str
    description: str
    price_usd: float  # per unit
    category: str


ICD10: dict[str, str] = {
    "Z00.00": "General adult medical exam without abnormal findings",
    "Z00.01": "General adult medical exam with abnormal findings",
    "Z12.11": "Screening for malignant neoplasm of colon",
    "Z12.5": "Screening for malignant neoplasm of prostate",
    "Z12.31": "Screening mammogram for malignant neoplasm of breast",
    "Z09": "Follow-up exam after completed treatment",
    "Z34.90": "Supervision of normal pregnancy, unspecified",
    "E11.9": "Type 2 diabetes mellitus without complications",
    "E78.5": "Hyperlipidemia, unspecified",
    "I10": "Essential (primary) hypertension",
    "N18.4": "Chronic kidney disease, stage 4",
    "J45.909": "Unspecified asthma, uncomplicated",
    "J06.9": "Acute upper respiratory infection, unspecified",
    "G47.33": "Obstructive sleep apnea",
    "C50.911": "Malignant neoplasm of right female breast, unspecified site",
    "N63.0": "Unspecified lump in breast",
    "R07.9": "Chest pain, unspecified",
    "R10.9": "Unspecified abdominal pain",
    "M54.50": "Low back pain, unspecified",
    "S93.401A": "Sprain of right ankle ligament, initial encounter",
}

_PROCEDURES = [
    Procedure("G0438", "Annual wellness visit, initial", 290.0, "wellness"),
    Procedure("G0439", "Annual wellness visit, subsequent", 175.0, "wellness"),
    Procedure("G0463", "Hospital outpatient clinic visit", 140.0, "visit"),
    Procedure("G0444", "Annual depression screening, 15 min", 25.0, "screening"),
    Procedure("G0121", "Screening colonoscopy, not high risk", 1100.0, "screening"),
    Procedure("G0328", "Fecal immunochemical test (FIT)", 25.0, "screening"),
    Procedure("G0103", "Prostate cancer screening (PSA)", 30.0, "screening"),
    Procedure("G0279", "Diagnostic digital breast tomosynthesis", 350.0, "imaging"),
    Procedure("C8908", "MRI breast without and with contrast, bilateral", 2450.0, "advanced_imaging"),
    Procedure("C8901", "MRA abdomen without contrast", 2150.0, "advanced_imaging"),
    Procedure("G0108", "Diabetes self-management training, per 30 min", 60.0, "training"),
    Procedure("G0283", "Electrical stimulation, unattended", 40.0, "therapy"),
    Procedure("G0383", "Emergency department visit, level 4", 650.0, "emergency"),
    Procedure("G0378", "Hospital observation, per hour", 95.0, "observation"),
    Procedure("J1885", "Ketorolac injection, per 15 mg", 12.0, "drug"),
    Procedure("J7030", "Normal saline infusion, 1000 cc", 20.0, "drug"),
    Procedure("J9355", "Trastuzumab injection, per 10 mg", 95.0, "specialty_drug"),
    Procedure("Q0084", "Chemotherapy administration by infusion, per visit", 450.0, "chemo_admin"),
    Procedure("A0429", "Ambulance service, BLS, emergency transport", 650.0, "ambulance"),
    Procedure("E0601", "CPAP device", 950.0, "dme"),
]

PROCEDURES: dict[str, Procedure] = {p.hcpcs: p for p in _PROCEDURES}

ADVANCED_IMAGING = {"C8901", "C8908"}
IMAGING = ADVANCED_IMAGING | {"G0279"}

assert len(ICD10) == 20 and len(PROCEDURES) == 20, "the code set is 20 + 20"


def describe(hcpcs: str) -> str:
    proc = PROCEDURES.get(hcpcs)
    return proc.description if proc else "Unlisted service"
