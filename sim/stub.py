"""The 11:15 stub: random but valid responses, same endpoints and shapes as the
real simulator. Writes no ground truth. Start with `python -m sim --stub`."""
from __future__ import annotations

from datetime import datetime, timezone

from .carc import denial_text
from .rules.base import claim_id, roll

_DENIALS = [("CO-16", "M62"), ("CO-151", "N362"), ("CO-97", "M80"), ("CO-50", "N130")]


class StubSimulator:
    mode = "stub"

    def __init__(self) -> None:
        self._attempts: dict[str, int] = {}

    def submit(self, claim: dict) -> dict:
        cid = claim_id(claim)
        attempt = self._attempts[cid] = self._attempts.get(cid, 0) + 1
        adjudication_id = f"adj_{cid}_{attempt}"
        denied = roll(cid, str(attempt), "stub") < 0.35
        carc, rarc = _DENIALS[int(roll(cid, "code") * len(_DENIALS))] if denied else (None, None)
        return {
            "claim_id": cid,
            "adjudication_id": adjudication_id,
            "_adjudication_id": adjudication_id,
            "attempt": attempt,
            "status": "denied" if denied else "paid",
            "carc": carc,
            "rarc": rarc,
            "denial_text": denial_text(carc, rarc) if denied else None,
            "paid_amount": 0.0 if denied else round(float(claim.get("total_charge_usd") or 0) * 0.8, 2),
            "adjudicated_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": int(500 + roll(cid, "latency") * 5000),
        }

    def appeal(self, request: dict) -> dict:
        cid = request.get("claim_id", "")
        won = roll(cid, "appeal") < 0.5
        return {
            "appeal_id": f"simapl_stub_{cid}",
            "claim_id": cid,
            "outcome": "overturned" if won else "upheld",
            "paid_amount": 100.0 if won else 0.0,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "note": "stub",
        }

    def policy_change(self, insurer: str) -> dict:
        return {"insurer": insurer, "policy_version": 2, "policy_version_id": f"{insurer}_v2", "note": "stub"}

    def policy_reset(self, insurer: str) -> dict:
        return {"insurer": insurer, "policy_version": 1, "policy_version_id": f"{insurer}_v1", "note": "stub"}

    def state(self) -> dict:
        return {"policy_versions": {}, "store": "none"}
