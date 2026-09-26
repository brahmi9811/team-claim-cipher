"""Write forge output: a local JSONL file, and MongoDB (`claims`, `policies`, `sim_truth` canaries).

Claims go through B's `firewall.encryption.encrypted_collection("claims")`, so
patient fields and notes land encrypted. `--allow-plaintext` exists only for
a local development database.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sim import policies as pol
from sim.rules import INSURERS

log = logging.getLogger("forge")

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "forge"
CLAIMS_FILE = OUT_DIR / "claims.jsonl"
CANARIES_FILE = OUT_DIR / "phi_canaries.jsonl"
BATCH = 500


def write_jsonl(docs: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(doc) + "\n")
    return path


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_claims(claims: list[dict], *, allow_plaintext: bool = False) -> dict:
    """Insert claims that aren't in `claims` yet. Safe to run twice."""
    coll, mode = _claims_collection(allow_plaintext)
    ids = [c["_id"] for c in claims]
    existing: set[str] = set()
    for i in range(0, len(ids), BATCH):
        existing |= {d["_id"] for d in coll.find({"_id": {"$in": ids[i:i + BATCH]}}, {"_id": 1})}
    new = [c for c in claims if c["_id"] not in existing]
    for i in range(0, len(new), BATCH):
        coll.insert_many(new[i:i + BATCH], ordered=False)
    return {"inserted": len(new), "already_present": len(existing), "encryption": mode}


def _claims_collection(allow_plaintext: bool):
    try:
        from firewall.encryption import detect_mode, encrypted_collection

        return encrypted_collection("claims"), detect_mode()
    except RuntimeError as exc:
        if not allow_plaintext:
            raise RuntimeError(f"{exc}\nRefusing to write patient fields unencrypted.") from exc
        from common.db import get_raw_db

        log.warning("Writing claims WITHOUT encryption (--allow-plaintext): local dev only")
        return get_raw_db("forge")["claims"], "none (plaintext, dev only)"


def load_policies() -> dict:
    """Publish version 1 of every insurer's policy as current. Safe to run twice."""
    from common.db import get_db

    coll = get_db("forge")["policies"]
    counts = {}
    for insurer in INSURERS:
        docs = pol.policy_docs(insurer, 1, current=True)
        for doc in docs:
            coll.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)  # $set keeps a stored `embedding`
        coll.update_many({"insurer": insurer, "version": {"$ne": 1}}, {"$set": {"current": False}})
        counts[insurer] = len(docs)
    return counts


def load_canaries(canaries: list[dict]) -> int:
    from common.db import get_db

    coll = get_db("forge")["sim_truth"]
    for doc in canaries:
        coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return len(canaries)
