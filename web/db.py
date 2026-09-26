"""Database access for Role D code (web/api.py, scripts/seed_fake.py, scripts/reset_demo.py).

Uses B's `common.db.get_db` once it exists; until then, falls back to a plain
pymongo connection built from MONGODB_URI / MONGODB_DB. Collection names follow
PLAN.md, "MongoDB data model".
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

CLAIMS = "claims"
POLICIES = "policies"
ADJUDICATIONS = "adjudications"
RULES = "rules"
APPEALS = "appeals"
HARNESS_PROFILES = "harness_profiles"
HARNESS_EVENTS = "harness_events"
METRICS = "metrics"
PHI_INCIDENTS = "phi_incidents"
SIM_TRUTH = "sim_truth"

INSURERS = ["payer_a", "payer_b", "payer_c"]


def get_db(role="agent_worker", raw=False):
    """Sync database handle. Prefers B's common.db so users and roles match.

    raw=True returns B's unguarded pymongo Database (admin scripts like reset_demo.py);
    otherwise B's GuardedDatabase, which blocks sim_truth for agent_worker.
    """
    try:
        from common import db as common_db

        return common_db.get_raw_db(role) if raw else common_db.get_db(role=role)
    except ImportError:
        pass
    from pymongo import MongoClient

    uri = os.environ.get("MONGODB_URI") or "mongodb://localhost:27017/?directConnection=true"
    name = os.environ.get("MONGODB_DB", "denialfighter")
    return MongoClient(uri, tz_aware=True)[name]


def ensure_metrics_collection(db):
    """Create `metrics` as a time-series collection if nobody has yet (B's setup_db.py normally does)."""
    if METRICS not in db.list_collection_names():
        db.create_collection(METRICS, timeseries={"timeField": "ts", "metaField": "insurer", "granularity": "seconds"})
