"""Restore a known demo state in under 1 minute (owner: D).

NEVER run after 2:30 PM unless the live demo is broken: a fresh reset wipes the
long-run history. Prefer restoring a snapshot of the real long run instead.

    python scripts/reset_demo.py --save before-judging    # snapshot the current run state (safe, read-only)
    python scripts/reset_demo.py --restore before-judging # put that snapshot back
    python scripts/reset_demo.py                          # fresh start: empty run state, v1 profiles and policies
    python scripts/reset_demo.py --fresh --seed-fake      # fresh start, then the fake demo data
    python scripts/reset_demo.py --list                   # list snapshots

"Run state" is what the loop writes: adjudications, appeals, rules, harness_events,
harness_profiles, metrics, phi_incidents, plus the orchestrator's orchestrator_state,
evolver_state and llm_calls. Claims, policies, phi_tokens and the key
vault are never touched (only Payer C's policy is pointed back at version 1 on a
fresh reset). sim_truth is never read by this script: --save copies its adjudication
rows server side (kept in sim_truth as type "snapshot_truth") and --restore puts them
back; a fresh reset deletes truth rows for adjudications that no longer exist, so the
scorer stays consistent.

A fresh reset also puts Payer C's policy back to version 1 through the simulator's
/admin/policy-reset/payer_c (set SIM_ADMIN_TOKEN if the simulator requires one).
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bson import json_util  # noqa: E402

from scripts.seed_fake import Seeder, default_profile  # noqa: E402
from web import db as dbm  # noqa: E402

SIM_URL = os.environ.get("SIM_URL", "http://localhost:8001")
# The orchestrator's own state goes too: its claim cursor (orchestrator_state), the Evolver's
# pending rollback (evolver_state) and LLM spend (llm_calls) all describe the run being reset.
RUN_STATE = [dbm.ADJUDICATIONS, dbm.APPEALS, dbm.RULES, dbm.HARNESS_EVENTS, dbm.HARNESS_PROFILES, dbm.METRICS,
             dbm.PHI_INCIDENTS, "orchestrator_state", "evolver_state", "llm_calls"]
SNAP_DIR = dbm.ROOT / "data" / "snapshots"
BATCH = 1000


def snap_path(name):
    return SNAP_DIR / f"{name}.jsonl"


def save(db, name):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = snap_path(name)
    counts = {}
    with path.open("w", encoding="utf-8") as f:
        f.write(json_util.dumps({"_meta": {"saved_at": datetime.now(timezone.utc), "db": db.name}}) + "\n")
        for coll in RUN_STATE:
            n = 0
            for doc in db[coll].find({}):
                f.write(json_util.dumps({"c": coll, "d": doc}) + "\n")
                n += 1
            counts[coll] = n
    print(f"Saved {path}: {counts}")
    save_truth(db, name)


def clear_collection(db, coll):
    try:
        return db[coll].delete_many({}).deleted_count
    except Exception:  # noqa: BLE001 - old time-series collections refuse unfiltered deletes
        if coll != dbm.METRICS:
            raise
        db.drop_collection(coll)
        dbm.ensure_metrics_collection(db)
        return "dropped"


def insert_batches(db, coll, docs):
    for i in range(0, len(docs), BATCH):
        db[coll].insert_many(docs[i : i + BATCH], ordered=False)


def save_truth(db, name):
    """Copy the adjudication ground truth into the snapshot, server side: the rows go from sim_truth
    back into sim_truth (type "snapshot_truth", ignored by the scorer and the simulator) and never
    pass through this script. Without it, a restore after a fresh wipe leaves the scoreboard blank
    and the simulator can't decide appeals on the restored denials."""
    truth = db[dbm.SIM_TRUTH]
    try:
        truth.delete_many({"type": "snapshot_truth", "snapshot": name})
        truth.aggregate([
            {"$match": {"type": "adjudication"}},
            {"$project": {"_id": {"$concat": [f"snapshot:{name}:", {"$toString": "$_id"}]},
                          "type": {"$literal": "snapshot_truth"}, "snapshot": {"$literal": name}, "truth": "$$ROOT"}},
            {"$merge": {"into": dbm.SIM_TRUTH, "whenMatched": "replace", "whenNotMatched": "insert"}},
        ])
        print(f"  sim_truth: {truth.count_documents({'type': 'snapshot_truth', 'snapshot': name})} truth rows kept with the snapshot")
    except Exception as exc:  # noqa: BLE001 - this user may not have access to sim_truth
        print(f"  sim_truth: not saved ({exc.__class__.__name__}: {exc}); a restore will only prune it")


def restore_truth(db, name):
    """Put back the ground truth saved with the snapshot. Returns False if there is none (older snapshot)."""
    truth = db[dbm.SIM_TRUTH]
    try:
        if not truth.count_documents({"type": "snapshot_truth", "snapshot": name}, limit=1):
            return False
        truth.delete_many({"type": "adjudication"})
        truth.aggregate([
            {"$match": {"type": "snapshot_truth", "snapshot": name}},
            {"$replaceWith": "$truth"},
            {"$merge": {"into": dbm.SIM_TRUTH, "whenMatched": "replace", "whenNotMatched": "insert"}},
        ])
        print(f"  sim_truth: restored {truth.count_documents({'type': 'adjudication'})} truth rows from the snapshot")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  sim_truth: restore failed ({exc.__class__.__name__}: {exc})")
        return False


def prune_truth(db):
    """Delete truth rows for claims with no adjudication left. A filter-only delete: sim_truth is never read.

    Matches on claim_id, not adjudication_id: the orchestrator's adjudication _id need not equal
    the simulator's adjudication_id, and matching on it could wipe all ground truth.
    """
    live_claims = db[dbm.ADJUDICATIONS].distinct("claim_id")
    try:
        # Only adjudication rows: sim_truth also holds forge's PHI canaries (type "phi_canary"),
        # which the scorer needs whether or not their claim has been submitted yet.
        n = db[dbm.SIM_TRUTH].delete_many({"type": "adjudication", "claim_id": {"$nin": live_claims}}).deleted_count
        print(f"  sim_truth: removed {n} orphaned rows")
    except Exception as exc:  # noqa: BLE001 - this user may not have access to sim_truth
        print(f"  sim_truth: skipped ({exc.__class__.__name__}: {exc})")


def restore(db, name):
    path = snap_path(name)
    if not path.exists():
        sys.exit(f"No snapshot {path}. Use --list.")
    docs = {c: [] for c in RUN_STATE}
    with path.open(encoding="utf-8") as f:
        meta = json_util.loads(f.readline())["_meta"]
        for line in f:
            row = json_util.loads(line)
            docs[row["c"]].append(row["d"])
    print(f"Restoring {path.name} (saved {meta['saved_at']})")
    for coll in RUN_STATE:
        cleared = clear_collection(db, coll)
        insert_batches(db, coll, docs[coll])
        print(f"  {coll}: cleared {cleared}, restored {len(docs[coll])}")
    if not restore_truth(db, name):
        prune_truth(db)


def fresh(db):
    for coll in RUN_STATE:
        print(f"  {coll}: cleared {clear_collection(db, coll)}")
    db[dbm.HARNESS_PROFILES].insert_many([{**default_profile(ins), "updated_by": "reset_demo"} for ins in dbm.INSURERS])
    print("  harness_profiles: v1 defaults for", ", ".join(dbm.INSURERS))
    reset_payer_c_policy(db)
    prune_truth(db)


def reset_payer_c_policy(db):
    """The simulator caches the policy version in memory, so reset through its endpoint."""
    headers = {"X-Admin-Token": os.environ["SIM_ADMIN_TOKEN"]} if os.environ.get("SIM_ADMIN_TOKEN") else {}
    try:
        r = httpx.post(f"{SIM_URL}/admin/policy-reset/payer_c", headers=headers, timeout=10)
        r.raise_for_status()
        print(f"  policies: payer_c back to version 1 via the simulator ({r.json().get('policy_version_id')})")
        return True
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  policies: simulator reset failed ({exc}); setting payer_c v1 current in the database.")
        print("            Restart the simulator so its cached policy version matches.")
        if db[dbm.POLICIES].find_one({"insurer": "payer_c", "version": 1}):
            db[dbm.POLICIES].update_many({"insurer": "payer_c"}, [{"$set": {"current": {"$eq": ["$version", 1]}}}])
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--save", metavar="NAME", help="snapshot the current run state")
    g.add_argument("--restore", metavar="NAME", help="restore a snapshot")
    g.add_argument("--fresh", action="store_true", help="empty run state with v1 profiles (the default)")
    g.add_argument("--list", action="store_true", help="list snapshots")
    ap.add_argument("--seed-fake", action="store_true", help="after a fresh reset, write the fake demo data")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()

    if args.list:
        for p in sorted(SNAP_DIR.glob("*.jsonl")) if SNAP_DIR.exists() else []:
            print(f"{p.stem:30} {p.stat().st_size / 1e6:6.1f} MB  {datetime.fromtimestamp(p.stat().st_mtime):%H:%M:%S}")
        return

    db = dbm.get_db(raw=True)  # admin script: needs drop_collection and sim_truth cleanup
    started = time.time()
    if args.save:
        save(db, args.save)
        return

    what = f"restore snapshot '{args.restore}' over" if args.restore else "WIPE"
    if not args.yes:
        print(f"This will {what} the run state in database '{db.name}': {', '.join(RUN_STATE)}.")
        if datetime.now().hour * 60 + datetime.now().minute >= 14 * 60 + 30:
            print("It is after 2:30 PM: only do this if the live demo is broken.")
        if input("Type RESET to continue: ").strip() != "RESET":
            sys.exit("Cancelled.")

    if args.restore:
        restore(db, args.restore)
    else:
        fresh(db)
        if args.seed_fake:
            seeder = Seeder(db, __import__("random").Random(7))
            seeder.resume_counters()  # claims survive the reset, so continue their ids
            seeder.seed(50)
    print(f"Done in {time.time() - started:.1f} s.")


if __name__ == "__main__":
    main()
