"""Published policies: sim/policies/*.md parsed into `policies` clause documents.

Document shape (docs/PLAN.md): {_id: "payer_b_v1_c7", insurer, version,
clause_no, title, clause_text, current}. The markdown files are the source of
truth; the `policies` collection is what the agents read.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

POLICY_DIR = Path(__file__).resolve().parent / "policies"

FILES: dict[tuple[str, int], str] = {
    ("payer_a", 1): "payer_a.md",
    ("payer_b", 1): "payer_b.md",
    ("payer_c", 1): "payer_c.md",
    ("payer_c", 2): "payer_c_v2.md",
}

_META = re.compile(r"^- (insurer|version):\s*(\S+)\s*$", re.M)
_CLAUSE = re.compile(r"^## Clause (\d+): (.+)$", re.M)


def clause_id(insurer: str, version: int, clause_no: int) -> str:
    return f"{insurer}_v{version}_c{clause_no}"


@lru_cache(maxsize=None)
def _parse(insurer: str, version: int) -> tuple[dict, ...]:
    text = (POLICY_DIR / FILES[(insurer, version)]).read_text(encoding="utf-8")
    meta = dict(_META.findall(text))
    if meta.get("insurer") != insurer or int(meta.get("version", 0)) != version:
        raise ValueError(f"{FILES[(insurer, version)]}: header says {meta}, expected {insurer} v{version}")

    headers = list(_CLAUSE.finditer(text))
    clauses = []
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = " ".join(text[match.end():end].split())
        clause_no = int(match.group(1))
        clauses.append({
            "_id": clause_id(insurer, version, clause_no),
            "insurer": insurer,
            "version": version,
            "clause_no": clause_no,
            "title": match.group(2).strip(),
            "clause_text": body,
        })
    return tuple(clauses)


def policy_docs(insurer: str, version: int, *, current: bool) -> list[dict]:
    return [{**c, "current": current} for c in _parse(insurer, version)]


def clause_text(insurer: str, version: int, clause_no: int) -> str:
    for c in _parse(insurer, version):
        if c["clause_no"] == clause_no:
            return c["clause_text"]
    raise KeyError(clause_id(insurer, version, clause_no))


def versions(insurer: str) -> list[int]:
    return sorted(v for (i, v) in FILES if i == insurer)


def normalize(text: str) -> str:
    """For "word for word" checks: same words and punctuation, ignoring case, quote style and spacing."""
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return " ".join(text.lower().split())
