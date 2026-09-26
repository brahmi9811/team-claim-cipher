"""Replay past Judge verdicts against known appeal outcomes.

The Evolver may change Judge thresholds only when this replay shows an
improvement. Used at the 3:30 deliverable.
"""
from __future__ import annotations

import logging
from typing import Any

from common import db as dbmod

log = logging.getLogger(__name__)


def replay_thresholds(
    insurer: str,
    *,
    min_confidence: float,
) -> dict[str, Any]:
    """Score how ``min_confidence`` would have performed on past verdicts.

    A wrongful verdict is "correct" when the subsequent appeal was overturned;
    "incorrect" when it was upheld. Legitimate verdicts that never appealed
    aren't scored here (no ground truth without sim_truth, which we can't read).

    Returns ``{precision, recall_proxy, n, improved_vs_current}``-style stats
    plus a ``recommend_change`` bool.
    """
    try:
        db = dbmod.get_db("agent_worker")
        adjudications = list(
            db[dbmod.ADJUDICATIONS].find(
                {
                    "insurer": insurer,
                    "status": "denied",
                    "verdict": {"$ne": None},
                    "holdout": {"$ne": True},
                }
            )
        )
        appeals = {
            a["claim_id"]: a
            for a in db[dbmod.APPEALS].find(
                {"insurer": insurer, "outcome": {"$ne": None}}
            )
        }
        current = db[dbmod.HARNESS_PROFILES].find_one({"_id": insurer}) or {}
        current_conf = (
            current.get("judge", {}).get("min_confidence", 0.75)
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("threshold replay unavailable (%s)", exc)
        return {
            "precision": 0.0,
            "n_scored": 0,
            "recommend_change": False,
            "reason": f"unavailable: {exc}",
        }

    def score(threshold: float) -> tuple[float, int, int]:
        """Return (precision, true_wrongful_caught, predicted_wrongful)."""
        tp = fp = 0
        predicted = 0
        for adj in adjudications:
            verdict = adj.get("verdict") or {}
            conf = float(verdict.get("confidence") or 0)
            label = verdict.get("label")
            if conf < threshold:
                continue
            if label not in ("wrongful_policy", "wrongful_bulk"):
                continue
            predicted += 1
            appeal = appeals.get(adj["claim_id"])
            if not appeal:
                continue
            if appeal.get("outcome") == "overturned":
                tp += 1
            elif appeal.get("outcome") == "upheld":
                fp += 1
        scored = tp + fp
        precision = tp / scored if scored else 0.0
        return precision, tp, predicted

    new_precision, new_tp, new_pred = score(min_confidence)
    old_precision, old_tp, old_pred = score(current_conf)

    improved = new_precision > old_precision + 1e-9 or (
        abs(new_precision - old_precision) < 1e-9 and new_tp > old_tp
    )
    return {
        "precision": round(new_precision, 4),
        "baseline_precision": round(old_precision, 4),
        "n_scored": new_tp + (new_pred - new_tp),  # rough
        "true_positives": new_tp,
        "predicted_wrongful": new_pred,
        "recommend_change": improved,
        "proposed_min_confidence": min_confidence,
        "current_min_confidence": current_conf,
        "reason": (
            f"precision {new_precision:.2f} vs baseline {old_precision:.2f}"
        ),
    }
