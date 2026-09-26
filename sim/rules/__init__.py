"""Hidden rules per insurer and policy version. Never imported by agents."""
from __future__ import annotations

from . import payer_a, payer_b, payer_c
from .base import HiddenRule

INSURERS = ("payer_a", "payer_b", "payer_c")

_RULES: dict[tuple[str, int], list[HiddenRule]] = {
    ("payer_a", 1): payer_a.RULES_V1,
    ("payer_b", 1): payer_b.RULES_V1,
    ("payer_c", 1): payer_c.RULES_V1,
    ("payer_c", 2): payer_c.RULES_V2,
}

LATEST_VERSION = {insurer: max(v for (i, v) in _RULES if i == insurer) for insurer in INSURERS}

ALL_RULES: dict[str, HiddenRule] = {r.id: r for rules in _RULES.values() for r in rules}


def rules_for(insurer: str, version: int = 1) -> list[HiddenRule]:
    return _RULES[(insurer, version)]


__all__ = ["ALL_RULES", "HiddenRule", "INSURERS", "LATEST_VERSION", "rules_for"]
