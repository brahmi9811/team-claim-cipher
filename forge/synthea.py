"""Download and read the public Synthea sample data (patients, encounters, conditions)."""
from __future__ import annotations

import csv
import io
import re
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNTHEA_DIR = ROOT / "data" / "synthea" / "csv"
SAMPLE_URL = "https://synthetichealth.github.io/synthea-sample-data/downloads/latest/synthea_sample_data_csv_latest.zip"
NEEDED = ("patients.csv", "encounters.csv", "conditions.csv")

US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
}


@dataclass
class Patient:
    id: str
    first: str
    last: str
    birthdate: str
    gender: str
    ssn: str
    address: str
    city: str
    state: str  # two-letter code
    zip: str
    conditions: list[str] = field(default_factory=list)  # SNOMED descriptions

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"

    def age_on(self, day: date) -> int:
        born = date.fromisoformat(self.birthdate[:10])
        return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


@dataclass
class Encounter:
    id: str
    patient_id: str
    start: str
    encounter_class: str
    description: str
    reason: str


def download(target: Path = SYNTHEA_DIR, url: str = SAMPLE_URL) -> Path:
    """Fetch the sample zip (about 6 MB) and extract the CSVs we use."""
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = resp.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for member in zf.namelist():
            name = Path(member).name
            if name in NEEDED:
                (target / name).write_bytes(zf.read(member))
    missing = [n for n in NEEDED if not (target / n).exists()]
    if missing:
        raise RuntimeError(f"Synthea download is missing {missing}")
    return target


def available(source: Path = SYNTHEA_DIR) -> bool:
    return all((source / n).exists() for n in ("patients.csv", "encounters.csv"))


def _clean_name(value: str) -> str:
    """Synthea appends digits to names (Guillermo498); drop them."""
    return re.sub(r"\d+", "", value or "").strip()


def _rows(path: Path):
    with path.open(encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f)


def read_patients(source: Path = SYNTHEA_DIR) -> dict[str, Patient]:
    conditions: dict[str, list[str]] = defaultdict(list)
    if (source / "conditions.csv").exists():
        for row in _rows(source / "conditions.csv"):
            if row.get("DESCRIPTION") and "(disorder)" in row["DESCRIPTION"]:
                conditions[row["PATIENT"]].append(row["DESCRIPTION"])

    patients = {}
    for row in _rows(source / "patients.csv"):
        state = row.get("STATE", "")
        patients[row["Id"]] = Patient(
            id=row["Id"],
            first=_clean_name(row.get("FIRST", "")),
            last=_clean_name(row.get("LAST", "")),
            birthdate=row["BIRTHDATE"],
            gender=row.get("GENDER", ""),
            ssn=row.get("SSN", ""),
            address=row.get("ADDRESS", ""),
            city=row.get("CITY", ""),
            state=US_STATES.get(state, state[:2].upper()),
            zip=row.get("ZIP", "") or "",
            conditions=sorted(set(conditions.get(row["Id"], []))),
        )
    return patients


def read_encounters(source: Path = SYNTHEA_DIR) -> list[Encounter]:
    return [
        Encounter(
            id=row["Id"],
            patient_id=row["PATIENT"],
            start=row["START"],
            encounter_class=row.get("ENCOUNTERCLASS", ""),
            description=row.get("DESCRIPTION", ""),
            reason=row.get("REASONDESCRIPTION", "") or "",
        )
        for row in _rows(source / "encounters.csv")
    ]
