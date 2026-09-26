#!/usr/bin/env python3
"""Create all collections, indexes, time-series metrics, Vector Search indexes,
and (when the sandbox allows) database users/roles.

Safe to run twice. Owned by Member B.

    python scripts/setup_db.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow running without an editable install
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import CollectionInvalid, OperationFailure

from common import db as dbmod
from common.models import default_harness_profile
from firewall.encryption import (
    detect_mode,
    ensure_encrypted_claims,
    ensure_master_key,
    get_or_create_data_key,
)


INSURERS = ("payer_a", "payer_b", "payer_c")


def _client() -> MongoClient:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI is not set. Copy .env.example to .env first.")
        sys.exit(1)
    return MongoClient(uri, serverSelectionTimeoutMS=10_000)


def ensure_collections(client: MongoClient) -> None:
    db = client[dbmod.DB_NAME]
    existing = set(db.list_collection_names())

    # Time-series metrics
    if dbmod.METRICS not in existing:
        try:
            db.create_collection(
                dbmod.METRICS,
                timeseries={
                    "timeField": "ts",
                    "metaField": "insurer",
                    "granularity": "seconds",
                },
            )
            print(f"  created time-series collection '{dbmod.METRICS}'")
        except CollectionInvalid:
            print(f"  '{dbmod.METRICS}' already exists")
    else:
        print(f"  '{dbmod.METRICS}' ok")

    for name in dbmod.ALL_APP_COLLECTIONS:
        if name in (dbmod.METRICS, dbmod.CLAIMS):  # claims is created encrypted in ensure_encryption
            continue
        if name not in existing:
            db.create_collection(name)
            print(f"  created '{name}'")
        else:
            print(f"  '{name}' ok")

    # Key vault DB/collection
    kv = client[dbmod.KEY_VAULT_DB]
    if dbmod.KEY_VAULT_COLL not in kv.list_collection_names():
        kv.create_collection(dbmod.KEY_VAULT_COLL)
        print(f"  created '{dbmod.KEY_VAULT_DB}.{dbmod.KEY_VAULT_COLL}'")
    kv[dbmod.KEY_VAULT_COLL].create_index(
        "keyAltNames", unique=True, partialFilterExpression={"keyAltNames": {"$exists": True}}
    )


def ensure_indexes(client: MongoClient) -> None:
    db = client[dbmod.DB_NAME]

    db[dbmod.RULES].create_index(
        [("insurer", ASCENDING), ("status", ASCENDING)], name="rules_insurer_status"
    )
    db[dbmod.ADJUDICATIONS].create_index(
        [("insurer", ASCENDING), ("adjudicated_at", DESCENDING)],
        name="adj_insurer_time",
    )
    db[dbmod.ADJUDICATIONS].create_index(
        [("claim_id", ASCENDING)], name="adj_claim_id"
    )
    db[dbmod.APPEALS].create_index(
        [("insurer", ASCENDING), ("outcome", ASCENDING)], name="appeals_insurer_outcome"
    )
    db[dbmod.APPEALS].create_index([("created_at", DESCENDING)], name="appeals_created_at")
    db[dbmod.POLICIES].create_index(
        [("insurer", ASCENDING), ("version", ASCENDING), ("current", ASCENDING)],
        name="policies_insurer_version",
    )
    db[dbmod.CLAIMS].create_index([("insurer", ASCENDING)], name="claims_insurer")
    db[dbmod.CLAIMS].create_index([("holdout", ASCENDING)], name="claims_holdout")
    db[dbmod.HARNESS_EVENTS].create_index(
        [("insurer", ASCENDING), ("ts", DESCENDING)], name="events_insurer_ts"
    )
    db[dbmod.PHI_INCIDENTS].create_index(
        [("ts", DESCENDING)], name="phi_incidents_ts"
    )
    db[dbmod.PHI_TOKENS].create_index(
        [("member_id_hash", ASCENDING)], name="phi_tokens_member_hash"
    )
    db[dbmod.SIM_TRUTH].create_index(
        [("claim_id", ASCENDING)], name="sim_truth_claim"
    )
    print("  compound indexes ok")


def ensure_vector_search(client: MongoClient) -> None:
    """Create Atlas Vector Search indexes with Automated Embeddings when possible.

    On local Mongo / tiers without Atlas Search, this prints a skip message
    and keyword fallback in common/search.py still works.
    """
    db = client[dbmod.DB_NAME]

    definitions = [
        {
            "name": "policies_vector",
            "collection": dbmod.POLICIES,
            "definition": {
                "fields": [
                    {
                        "type": "text",
                        "path": "clause_text",
                        # Automated Embeddings — Atlas embeds on write when supported
                        "model": "voyage-4",
                    },
                    {"type": "filter", "path": "insurer"},
                    {"type": "filter", "path": "version"},
                    {"type": "filter", "path": "current"},
                ]
            },
        },
        {
            "name": "adjudications_vector",
            "collection": dbmod.ADJUDICATIONS,
            "definition": {
                "fields": [
                    {
                        "type": "text",
                        "path": "denial_text",
                        "model": "voyage-4",
                    },
                    {"type": "filter", "path": "insurer"},
                    {"type": "filter", "path": "status"},
                ]
            },
        },
    ]

    for spec in definitions:
        coll = db[spec["collection"]]
        try:
            # pymongo 4.x SearchIndexModel API
            from pymongo.operations import SearchIndexModel

            model = SearchIndexModel(
                definition=spec["definition"],
                name=spec["name"],
                type="vectorSearch",
            )
            coll.create_search_index(model=model)
            print(f"  vector search index '{spec['name']}' created/submitted")
        except Exception as exc:  # noqa: BLE001
            # Fallback: try classic createSearchIndex command
            try:
                db.command(
                    {
                        "createSearchIndexes": spec["collection"],
                        "indexes": [
                            {
                                "name": spec["name"],
                                "type": "vectorSearch",
                                "definition": spec["definition"],
                            }
                        ],
                    }
                )
                print(f"  vector search index '{spec['name']}' created via command")
            except Exception as exc2:  # noqa: BLE001
                # Last try: a classic "bring your own embeddings" index over the `embedding`
                # field, which common/search.py queries with local all-MiniLM-L6-v2 vectors
                # (384 dimensions; fill them with scripts/embed_policies.py).
                filters = [f for f in spec["definition"]["fields"] if f["type"] == "filter"]
                classic = {"fields": [{"type": "vector", "path": "embedding", "numDimensions": 384,
                                       "similarity": "cosine"}, *filters]}
                try:
                    from pymongo.operations import SearchIndexModel

                    coll.create_search_index(model=SearchIndexModel(definition=classic, name=spec["name"], type="vectorSearch"))
                    print(f"  vector search index '{spec['name']}' created as a classic 384-dim index "
                          "(run scripts/embed_policies.py to fill `embedding`)")
                except Exception as exc3:  # noqa: BLE001
                    print(
                        f"  SKIP vector index '{spec['name']}' "
                        f"(Atlas Search required): {exc3 or exc2 or exc}"
                    )
                    print(
                        "       common/search.py will use keyword / Embedding API fallback"
                    )


def ensure_encryption() -> None:
    ensure_master_key()
    mode = detect_mode()
    print(f"  encryption mode: {mode}")
    if mode in ("queryable", "csfle"):
        try:
            kid = get_or_create_data_key()
            print(f"  data key ready: {kid}")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: data key setup failed: {exc}")
    db = dbmod.get_raw_db("firewall")
    if mode == "queryable":
        status = ensure_encrypted_claims()
        if status == "plain_with_data":
            print(f"  WARNING: '{dbmod.CLAIMS}' exists UNENCRYPTED with documents. Drop it and re-run "
                  "setup_db.py, then reload claims with `python -m forge`.")
        else:
            print(f"  encrypted collection '{dbmod.CLAIMS}': {status}")
    elif dbmod.CLAIMS not in db.list_collection_names():
        db.create_collection(dbmod.CLAIMS)
        print(f"  created '{dbmod.CLAIMS}' (no encryption available)")


def seed_default_profiles(client: MongoClient) -> None:
    db = client[dbmod.DB_NAME]
    for insurer in INSURERS:
        existing = db[dbmod.HARNESS_PROFILES].find_one({"_id": insurer})
        if existing is None:
            db[dbmod.HARNESS_PROFILES].insert_one(default_harness_profile(insurer))
            print(f"  seeded harness_profiles/{insurer}")
        else:
            print(f"  harness_profiles/{insurer} exists")


def try_create_roles(client: MongoClient) -> None:
    """Best-effort custom roles. Sandbox tiers often disallow this — that's ok."""
    roles = [
        {
            "role": "cc_agent_worker",
            "privileges": [
                {
                    "resource": {"db": dbmod.DB_NAME, "collection": c},
                    "actions": ["find", "insert", "update", "remove"],
                }
                for c in dbmod.ALL_APP_COLLECTIONS
                if c != dbmod.SIM_TRUTH
            ],
            "roles": [],
        },
        {
            "role": "cc_firewall",
            "privileges": [
                {
                    "resource": {"db": dbmod.DB_NAME, "collection": c},
                    "actions": ["find", "insert", "update", "remove"],
                }
                for c in dbmod.ALL_APP_COLLECTIONS
                if c != dbmod.SIM_TRUTH
            ]
            + [
                {
                    "resource": {
                        "db": dbmod.KEY_VAULT_DB,
                        "collection": dbmod.KEY_VAULT_COLL,
                    },
                    "actions": ["find", "insert", "update"],
                }
            ],
            "roles": [],
        },
        {
            "role": "cc_simulator",
            "privileges": [
                {
                    "resource": {"db": dbmod.DB_NAME, "collection": dbmod.SIM_TRUTH},
                    "actions": ["find", "insert", "update"],
                },
                {
                    "resource": {"db": dbmod.DB_NAME, "collection": dbmod.CLAIMS},
                    "actions": ["find"],
                },
                {
                    "resource": {"db": dbmod.DB_NAME, "collection": dbmod.POLICIES},
                    "actions": ["find", "insert", "update"],
                },
            ],
            "roles": [],
        },
        {
            "role": "cc_scorer",
            "privileges": [
                {
                    "resource": {"db": dbmod.DB_NAME, "collection": c},
                    "actions": ["find"],
                }
                for c in (
                    dbmod.SIM_TRUTH,
                    dbmod.ADJUDICATIONS,
                    dbmod.APPEALS,
                    dbmod.CLAIMS,
                    dbmod.PHI_INCIDENTS,
                    dbmod.LLM_CALLS,
                    dbmod.RULES,
                    dbmod.HARNESS_EVENTS,
                )
            ]
            + [
                {
                    "resource": {"db": dbmod.DB_NAME, "collection": dbmod.METRICS},
                    "actions": ["find", "insert", "update"],
                }
            ],
            "roles": [],
        },
    ]

    admin = client["admin"]
    for role_def in roles:
        try:
            admin.command(
                {
                    "createRole": role_def["role"],
                    "privileges": role_def["privileges"],
                    "roles": role_def["roles"],
                }
            )
            print(f"  created role {role_def['role']}")
        except OperationFailure as exc:
            if exc.code in (510, 16467, 13):  # already exists / unauthorized
                # try updateRole
                try:
                    admin.command(
                        {
                            "updateRole": role_def["role"],
                            "privileges": role_def["privileges"],
                            "roles": role_def["roles"],
                        }
                    )
                    print(f"  updated role {role_def['role']}")
                except OperationFailure as exc2:
                    print(
                        f"  SKIP role {role_def['role']} "
                        f"(sandbox may not allow custom roles): {exc2.details.get('errmsg', exc2)}"
                    )
                    print(
                        "       common/db.py GuardedDatabase still enforces sim_truth isolation in code"
                    )
            else:
                print(f"  SKIP role {role_def['role']}: {exc}")


def main() -> None:
    print("Claim Cipher — setup_db")
    print("=" * 40)

    client = _client()
    try:
        client.admin.command("ping")
        info = client.server_info()
        print(f"Connected. MongoDB {info.get('version')}")
    except Exception as exc:
        print(f"ERROR: cannot reach MongoDB: {exc}")
        sys.exit(1)

    print("\n[1/5] Collections")
    ensure_collections(client)

    # Encryption before indexes: creating an index on `claims` would create it as a plain collection.
    print("\n[2/5] Encryption")
    ensure_encryption()

    print("\n[3/5] Indexes")
    ensure_indexes(client)

    print("\n[4/5] Vector Search")
    ensure_vector_search(client)

    print("\n[5/5] Default profiles + roles")
    seed_default_profiles(client)
    try_create_roles(client)

    print("\nDone. Collections ready in database", dbmod.DB_NAME)
    mode = detect_mode()
    print(
        json.dumps(
            {
                "db": dbmod.DB_NAME,
                "encryption_mode": mode,
                "insurers": list(INSURERS),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
