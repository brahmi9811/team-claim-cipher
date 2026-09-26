"""In-loop metrics the Evolver and Trust Ladder react to.

This is deliberately NOT the honest scoreboard (Member A's `scorer/`, the
only code allowed to read `sim_truth`). It computes only from what the
agents themselves already produced -- `adjudications` and `appeals` -- which
is exactly the constraint docs/PLAN.md puts on the harness: it may only
learn from signals a real billing team would have.
"""
from __future__ import annotations

from orchestrator.db import coll


def recent_metrics(insurer: str, window: int = 50) -> dict:
    adjudications = list(coll("adjudications").find({"insurer": insurer}).sort("submitted_at", -1).limit(window))
    # Decided appeals only, newest by filing time: drafts awaiting approval must not use up the window.
    appeals = list(coll("appeals").find({"insurer": insurer, "outcome": {"$ne": None}}).sort("filed_at", -1).limit(window))

    total = len(adjudications)
    paid = sum(1 for a in adjudications if a["status"] == "paid")
    overturned = sum(1 for a in appeals if a["outcome"] == "overturned")

    return {
        "insurer": insurer,
        "sample_size": total,
        "acceptance_rate": (paid / total) if total else None,
        "appeal_sample_size": len(appeals),
        "appeal_win_rate": (overturned / len(appeals)) if appeals else None,
    }
