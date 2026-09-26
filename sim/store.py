"""The simulator's ledger: every decision it made, and each insurer's policy version.

Every adjudication is one `sim_truth` document (docs/PLAN.md shape plus extra
fields the scorer and the appeal logic need):

    {_id: adjudication_id, type: "adjudication", adjudication_id, claim_id,
     insurer, kind: legit|wrongful|None (paid), rule_id, status, carc, rarc,
     attempt, holdout, contradicts_clause_no, policy_version, total_charge_usd,
     paid_amount, latency_ms, batch_id, adjudicated_at, appeals: [...]}

MongoLedger writes to `sim_truth` and `policies`; MemoryLedger keeps the same
documents in a dict, for tests and for running without a database.
"""
from __future__ import annotations

import copy
import threading
from typing import Iterable, Protocol

from . import policies as pol

TRUTH = "sim_truth"
POLICIES = "policies"


class Ledger(Protocol):
    kind: str

    def record(self, doc: dict) -> None: ...
    def history(self, claim_id: str) -> list[dict]: ...
    def get(self, adjudication_id: str) -> dict | None: ...
    def latest_denial(self, claim_id: str) -> dict | None: ...
    def paid_claims(self, ids: Iterable[str], insurer: str) -> set[str]: ...
    def add_appeal(self, adjudication_id: str, appeal: dict) -> None: ...
    def policy_version(self, insurer: str) -> int: ...
    def set_policy_version(self, insurer: str, version: int) -> None: ...


class MemoryLedger:
    kind = "memory"

    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}
        self._versions: dict[str, int] = {}
        self._lock = threading.RLock()

    def record(self, doc: dict) -> None:
        with self._lock:
            self._docs[doc["_id"]] = copy.deepcopy(doc)

    def history(self, claim_id: str) -> list[dict]:
        with self._lock:
            docs = [copy.deepcopy(d) for d in self._docs.values() if d.get("claim_id") == claim_id and d.get("type") == "adjudication"]
        return sorted(docs, key=lambda d: d["attempt"])

    def get(self, adjudication_id: str) -> dict | None:
        with self._lock:
            doc = self._docs.get(adjudication_id)
            return copy.deepcopy(doc) if doc else None

    def latest_denial(self, claim_id: str) -> dict | None:
        denied = [d for d in self.history(claim_id) if d["status"] == "denied"]
        return denied[-1] if denied else None

    def paid_claims(self, ids: Iterable[str], insurer: str) -> set[str]:
        wanted = set(ids)
        with self._lock:
            return {
                d["claim_id"] for d in self._docs.values()
                if d.get("type") == "adjudication" and d["insurer"] == insurer and d["status"] == "paid"
                and (d["_id"] in wanted or d["claim_id"] in wanted)
            }

    def add_appeal(self, adjudication_id: str, appeal: dict) -> None:
        with self._lock:
            self._docs[adjudication_id].setdefault("appeals", []).append(copy.deepcopy(appeal))

    def policy_version(self, insurer: str) -> int:
        return self._versions.get(insurer, 1)

    def set_policy_version(self, insurer: str, version: int) -> None:
        self._versions[insurer] = version

    def all_docs(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(d) for d in self._docs.values()]


class MongoLedger:
    kind = "mongo"

    def __init__(self, db=None) -> None:
        if db is None:
            from common.db import get_db

            db = get_db("simulator")
        self._db = db
        self._versions: dict[str, int] = {}
        self._lock = threading.RLock()

    @property
    def _truth(self):
        return self._db[TRUTH]

    def record(self, doc: dict) -> None:
        self._truth.insert_one(dict(doc))

    def history(self, claim_id: str) -> list[dict]:
        return list(self._truth.find({"claim_id": claim_id, "type": "adjudication"}).sort("attempt", 1))

    def get(self, adjudication_id: str) -> dict | None:
        return self._truth.find_one({"_id": adjudication_id, "type": "adjudication"})

    def latest_denial(self, claim_id: str) -> dict | None:
        return self._truth.find_one({"claim_id": claim_id, "type": "adjudication", "status": "denied"}, sort=[("attempt", -1)])

    def paid_claims(self, ids: Iterable[str], insurer: str) -> set[str]:
        ids = list(ids)
        if not ids:
            return set()
        query = {"type": "adjudication", "insurer": insurer, "status": "paid",
                 "$or": [{"_id": {"$in": ids}}, {"claim_id": {"$in": ids}}]}
        return {d["claim_id"] for d in self._truth.find(query, {"claim_id": 1})}

    def add_appeal(self, adjudication_id: str, appeal: dict) -> None:
        self._truth.update_one({"_id": adjudication_id}, {"$push": {"appeals": appeal}})

    def policy_version(self, insurer: str) -> int:
        with self._lock:
            if insurer not in self._versions:
                doc = self._db[POLICIES].find_one({"insurer": insurer, "current": True}, sort=[("version", -1)])
                self._versions[insurer] = int(doc["version"]) if doc else 1
            return self._versions[insurer]

    def set_policy_version(self, insurer: str, version: int) -> None:
        """Publish `version` in `policies`: its clauses become current, every other version is marked not current."""
        coll = self._db[POLICIES]
        for doc in pol.policy_docs(insurer, version, current=True):
            coll.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)  # $set keeps a stored `embedding`
        coll.update_many({"insurer": insurer, "version": {"$ne": version}}, {"$set": {"current": False}})
        with self._lock:
            self._versions[insurer] = version
