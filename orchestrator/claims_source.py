"""Where the orchestrator gets claims to process, in priority order:

1. Real claims A's `forge/` has loaded into `claims` that this process
   hasn't drawn yet, in `_id` order (forge ids are zero-padded, `clm_00001`),
   tracked by a `last_id` cursor in `orchestrator_state`. Not `created_at`:
   forge stamps every claim of a run with the same `created_at`, so a
   `created_at > last` cursor stopped after the first batch.
2. If none are available (A hasn't loaded real data yet, or the queue is
   drained), freshly generated demo claims (`orchestrator/seed.py`), tagged
   `source: "demo"` so the real-claim cursor never picks them up once stored.

Claims are read through `claims_coll()`, which decrypts patient fields.
"""
from __future__ import annotations

import random

from orchestrator import config
from orchestrator.db import claims_coll, coll
from orchestrator.seed import demo_claims

_CURSOR_ID = "claims_cursor"


def next_batch(batch_size: int = 10) -> list[dict]:
    claims = _next_real_claims(batch_size)
    if claims:
        _advance_cursor(claims)
        return claims
    return demo_claims(random.choice(config.INSURERS), batch_size)


def _next_real_claims(batch_size: int) -> list[dict]:
    state = coll("orchestrator_state").find_one({"_id": _CURSOR_ID})
    last_id = state.get("last_id") if state else None
    query: dict = {"source": {"$ne": "demo"}}
    if last_id:
        query["_id"] = {"$gt": last_id}
    return list(claims_coll().find(query).sort("_id", 1).limit(batch_size))


def _advance_cursor(claims: list[dict]) -> None:
    latest = max(c["_id"] for c in claims)
    coll("orchestrator_state").update_one({"_id": _CURSOR_ID}, {"$set": {"last_id": latest}}, upsert=True)
