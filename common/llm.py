"""OpenRouter LLM client. Every call is wrapped by ``firewall.guard``.

``complete()`` matches the orchestrator contract (JSON dict out).
``call_llm()`` is the lower-level string API from the common/ README.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LANGSMITH_API_KEY = os.environ.get("LANGSMITH_API_KEY", "")
LANGSMITH_PROJECT = os.environ.get("LANGSMITH_PROJECT", "claim-cipher")

# Default models from the plan
MODEL_SONNET = "anthropic/claude-sonnet-4"
MODEL_HAIKU = "anthropic/claude-haiku-4.5"

# USD per million tokens (input, output), used only when OpenRouter doesn't report a cost.
_FALLBACK_PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}


class LLMUnavailable(Exception):
    """No API key, or the call/parse failed. Callers fall back to heuristics."""


def call_llm(
    model: str,
    system: str,
    prompt: str,
    *,
    insurer: str = "unknown",
    claim_id: str = "unknown",
    json_mode: bool = False,
) -> str:
    """Call OpenRouter and return the raw assistant text.

    Runs ``firewall.guard`` on both the request and the response. Raises
    ``PHILeak`` (from the firewall) if a leak is detected, or
    ``LLMUnavailable`` if the key is missing / the HTTP call fails.
    """
    from firewall.guard import guard

    if not OPENROUTER_API_KEY:
        raise LLMUnavailable("OPENROUTER_API_KEY not set")

    guarded_prompt = guard(
        prompt, direction="request", insurer=insurer, claim_id=claim_id
    )
    raw, usage = _call_openrouter(system, guarded_prompt, model, json_mode=json_mode)
    _log_call(model=model, insurer=insurer, claim_id=claim_id, usage=usage)
    guarded_raw = guard(
        raw, direction="response", insurer=insurer, claim_id=claim_id
    )
    _trace(
        model=model,
        insurer=insurer,
        claim_id=claim_id,
        system=system,
        user=guarded_prompt,
        response=guarded_raw,
    )
    return guarded_raw


def complete(
    *,
    system: str,
    user: str,
    model: str,
    insurer: str,
    claim_id: str,
) -> dict[str, Any]:
    """Call the LLM and parse its reply as JSON.

    Raises ``LLMUnavailable`` if there's no key or the call/parse fails —
    never returns malformed data.
    """
    try:
        raw = call_llm(
            model, system, user, insurer=insurer, claim_id=claim_id, json_mode=True
        )
    except LLMUnavailable:
        raise
    except Exception as exc:  # PHILeak and others propagate; wrap network-ish
        from firewall.guard import PHILeak

        if isinstance(exc, PHILeak):
            raise
        raise LLMUnavailable(str(exc)) from exc

    try:
        return json.loads(_extract_json(raw))
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"model did not return valid JSON: {exc}") from exc


def _extract_json(text: str) -> str:
    """Tolerate markdown fences around a JSON object."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop first and last fence lines
        inner = [ln for ln in lines[1:] if not ln.strip().startswith("```")]
        text = "\n".join(inner).strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    return text


def _call_openrouter(
    system: str, user: str, model: str, *, json_mode: bool
) -> tuple[str, dict[str, Any]]:
    """Returns (assistant text, usage). Usage includes OpenRouter's `cost` in USD."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "usage": {"include": True},  # OpenRouter adds token counts and cost to the response
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/claim-cipher",
        "X-Title": "claim-cipher",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(OPENROUTER_URL, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable(str(exc)) from exc

    return body["choices"][0]["message"]["content"], body.get("usage") or {}


def _estimate_cost(model: str, usage: dict[str, Any]) -> float:
    prices = next((p for k, p in _FALLBACK_PRICES.items() if k in model.lower()), _FALLBACK_PRICES["sonnet"])
    return (usage.get("prompt_tokens", 0) * prices[0] + usage.get("completion_tokens", 0) * prices[1]) / 1_000_000


def _log_call(*, model: str, insurer: str, claim_id: str, usage: dict[str, Any]) -> None:
    """One `llm_calls` document per call; the scorer turns these into cost per claim."""
    try:
        from datetime import datetime, timezone

        from common import db as dbmod

        cost = usage.get("cost")
        dbmod.get_db()[dbmod.LLM_CALLS].insert_one({
            "ts": datetime.now(timezone.utc),
            "insurer": insurer,
            "claim_id": claim_id,
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cost_usd": float(cost) if cost is not None else _estimate_cost(model, usage),
            "cost_estimated": cost is None,
        })
    except Exception as exc:  # noqa: BLE001 -- cost logging must never break the loop
        log.debug("llm_calls log skipped: %s", exc)


def _trace(**fields: Any) -> None:
    """Submit a LangSmith run when ``LANGSMITH_API_KEY`` is set."""
    if not LANGSMITH_API_KEY:
        return
    try:
        from langsmith import Client

        client = Client(api_key=LANGSMITH_API_KEY)
        client.create_run(
            name=f"llm:{fields.get('model', 'unknown')}",
            run_type="llm",
            inputs={
                "system": fields.get("system"),
                "user": fields.get("user"),
                "insurer": fields.get("insurer"),
                "claim_id": fields.get("claim_id"),
            },
            outputs={"response": fields.get("response")},
            project_name=LANGSMITH_PROJECT,
            extra={"metadata": {"model": fields.get("model")}},
        )
    except Exception as exc:  # noqa: BLE001 — tracing must never break the loop
        log.debug("LangSmith trace skipped: %s", exc)
