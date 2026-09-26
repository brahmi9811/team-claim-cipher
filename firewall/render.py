"""Detokenize appeal letters in plain code, after all LLM calls finish.

Real patient values are put back only here — never inside an LLM prompt.
"""
from __future__ import annotations

import logging
import re

from firewall.tokenize import lookup_patient

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"PATIENT_(?:\d{8}|\d{4})\b")  # 4-digit tokens predate the 8-digit format


def render_letter(
    tokenized_letter: str,
    claim_id: str | None = None,  # noqa: ARG001 — reserved; tokens are self-describing
) -> str:
    """Replace ``PATIENT_NNNN`` tokens with real names from ``phi_tokens``.

    If a token can't be resolved (offline / missing mapping), leave it as-is
    so the letter remains reviewable rather than crashing the appeal path.
    """
    if not tokenized_letter:
        return tokenized_letter

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        ref = lookup_patient(token)
        if not ref:
            return token
        name = ref.get("name")
        return name if name else token

    try:
        return _TOKEN_RE.sub(_replace, tokenized_letter)
    except Exception as exc:  # noqa: BLE001
        log.warning("render_letter failed (%s); returning tokenized letter", exc)
        return tokenized_letter
