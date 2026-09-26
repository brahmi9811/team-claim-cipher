"""Files appeals a human approved in the live view.

This was the missing piece flagged in review: `web/api.py`'s
`POST /appeals/{id}/approve` sets `status: "approved"` on a `draft_only`
appeal and says, in its own docstring, "C's appeal_worker files appeals with
status 'approved'" -- but nothing did. `orchestrator/pipeline.py` only ever
files an appeal inline, and only for `auto_file` mode. This is the counterpart
for the human-in-the-loop path (docs/PLAN.md, Loop 3: "Draft-only mode: the
appeal waits for a human click in the live view").

Run periodically from `orchestrator/main.py`, independent of any single
claim's processing, since approval happens asynchronously from the web UI.
"""
from __future__ import annotations

import logging

from agents import appeal_writer, trust_ladder
from orchestrator.contracts.sim_client import SimulatorError
from orchestrator.db import coll

log = logging.getLogger(__name__)


def file_approved_appeals() -> list[dict]:
    """File every appeal a human has approved but that hasn't been filed or
    decided yet. Returns the filed appeals (with their outcome)."""
    filed: list[dict] = []
    for appeal in list(coll("appeals").find({"status": "approved", "outcome": None})):
        try:
            result = appeal_writer.file(appeal)
            trust_ladder.update(appeal["insurer"])
            filed.append(result)
        except SimulatorError as exc:
            # The simulator refused it (e.g. 404: no denial on record). Retrying every poll would
            # loop forever; the live view shows Approve again so a human can retry.
            log.warning("simulator refused approved appeal %s: %s", appeal.get("_id"), exc)
            coll("appeals").update_one({"_id": appeal["_id"]}, {"$set": {"status": "filing_failed", "filing_error": str(exc)}})
        except Exception:  # noqa: BLE001 -- one bad appeal must not block the others
            log.exception("failed to file approved appeal %s", appeal.get("_id"))
    return filed
