"""Thin convenience wrapper over `common.db` for agents/ and orchestrator/.

Replaces the old in-memory `orchestrator/contracts/store.py`. Every call site
that used to do `db.find("adjudications", query)` now does
`coll("adjudications").find(query)` — a real pymongo Collection from B's
`common.db.get_db()`, which enforces the agent_worker/firewall/simulator/scorer
role boundaries (agent_worker can never read `sim_truth`).

`utcnow()` is used everywhere a timestamp is stored, so every document this
codebase writes uses a real BSON date -- matching A's `sim/app.py` and D's
`scripts/seed_fake.py` -- instead of the ISO-string timestamps
`common.models.now_iso()` produces (mixing the two types in one collection
makes MongoDB's chronological sort unreliable).
"""
from __future__ import annotations

from datetime import datetime, timezone

from common import db as dbmod


def coll(name: str, *, role: str = "agent_worker"):
    return dbmod.get_db(role)[name]


def claims_coll():
    """The `claims` collection through B's firewall: patient fields are encrypted on
    write and decrypted on read (tokenize() needs the real dob/address). Falls back to
    the plain collection when claims isn't encrypted (local dev, --allow-plaintext)."""
    try:
        from firewall.encryption import encrypted_collection

        return encrypted_collection("claims")
    except RuntimeError:
        return coll("claims")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_datetime(value: datetime | str) -> datetime:
    """Normalize a timestamp that might have crossed an HTTP/JSON boundary
    (A's simulator responses, `common.models`' ISO-string builders) back into
    a real datetime before it's persisted."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
