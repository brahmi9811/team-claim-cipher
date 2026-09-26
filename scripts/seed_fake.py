"""Fake data so everyone can build before the real loop runs (owner: D).

Writes about 50 claims with adjudications, verdicts, appeals, harness events,
metrics history, harness profiles and policy clauses, all in the shapes from
docs/PLAN.md. Every document gets `fake: true`, so `--clear` removes exactly
what this script wrote and nothing else.

    python scripts/seed_fake.py              # seed about 50 claims
    python scripts/seed_fake.py --live       # seed, then keep writing new activity (Ctrl+C to stop)
    python scripts/seed_fake.py --clear      # remove all fake documents
"""

import argparse
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import db as dbm  # noqa: E402

FAKE = {"fake": True}

DIAGNOSES = ["E11.9", "I10", "J45.909", "M54.50", "Z00.00", "R07.9", "J20.9", "N39.0", "K21.9", "M17.11", "Z12.31"]
# code, description, price, encounter type
PROCEDURES = [
    ("G0439", "Annual wellness visit, subsequent", 175, "wellness"),
    ("G0463", "Hospital outpatient clinic visit", 140, "outpatient"),
    ("G2211", "Visit complexity add-on", 16, "outpatient"),
    ("J1100", "Dexamethasone injection, 1 mg", 12, "outpatient"),
    ("J0696", "Ceftriaxone injection, 250 mg", 25, "outpatient"),
    ("G0008", "Influenza vaccine administration", 30, "wellness"),
    ("A0428", "Ambulance service, BLS, non-emergency", 480, "ambulance"),
    ("E0601", "CPAP device", 950, "dme"),
    ("C8903", "MRI breast with contrast, unilateral", 2450, "imaging"),
    ("C8910", "MRA chest with contrast", 2150, "imaging"),
    ("C8918", "MRA pelvis with contrast", 2300, "imaging"),
    ("G0121", "Colorectal cancer screening colonoscopy", 1100, "outpatient"),
]
# carc, rarc, text, kind the fake truth leans toward
DENIALS = [
    ("CO-50", "N115", "These are non-covered services because this is not deemed a medical necessity by the payer.", "wrongful"),
    ("CO-197", "N54", "Precertification/authorization/notification absent.", "legit"),
    ("CO-16", "MA130", "Claim/service lacks information or has submission/billing error(s).", "legit"),
    ("CO-97", "M15", "The benefit for this service is included in the payment for another service already adjudicated.", "legit"),
    ("CO-151", "N362", "Payment adjusted because the payer deems the information submitted does not support this many units.", "legit"),
    ("CO-4", "N519", "The procedure code is inconsistent with the modifier used or a required modifier is missing.", "legit"),
]
FIRST = ["Maria", "James", "Aisha", "Wei", "Carlos", "Priya", "John", "Fatima", "Liam", "Grace", "Omar", "Elena"]
LAST = ["Garcia", "Smith", "Khan", "Chen", "Lopez", "Patel", "Nguyen", "Brown", "Okafor", "Rossi", "Kim", "Silva"]
STATES = ["MA", "NY", "CA", "TX", "WA", "IL"]

POLICY_TITLES = [
    "Definitions", "Covered outpatient services", "Preventive care", "Prior authorization",
    "Durable medical equipment", "Ambulance transport", "Advanced imaging", "Follow-up visits",
    "Bundled services", "Units of service", "Timely filing", "Appeals",
]

# Fallback only; B's common.models.default_harness_profile is the source of truth.
DEFAULT_PROFILE = {
    "version": 1,
    "context_policy": {"policy_clauses": 3, "paid_comparables": 2, "include_pattern_stats": False},
    "judge": {"min_confidence": 0.75, "bulk_window_sec": 60, "bulk_min_identical": 20},
    "appeal_strategy": {"lead_with": "clause", "include_pattern_stats": False, "quote_clause_verbatim": False},
    "permissions": {"appeals": "draft_only", "earned_at": None},
    "guardrails": {"fixed": ["no_upcoding", "no_phi_to_llm"], "learned": []},
    "updated_by": "seed",
    "last_event_id": None,
}


def default_profile(insurer):
    try:
        from common.models import default_harness_profile

        return default_harness_profile(insurer)
    except ImportError:
        return {"_id": insurer, **DEFAULT_PROFILE}


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    """Claims' created_at and verdicts' created_at are ISO strings (forge, common.models.now_iso).
    Everything the loop writes (adjudication times, event ts, appeal created_at) is a BSON date,
    matching the orchestrator (orchestrator/db.py utcnow) and the simulator."""
    return dt.isoformat()


class Seeder:
    def __init__(self, db, rng):
        self.db = db
        self.rng = rng
        self.counter = {"clm": 90000, "adj": 90000, "apl": 90000, "evt": 90000}
        self._claims_phi_ok = True
        self._metric_state = {ins: {"acc": rng.uniform(0.6, 0.66), "win": 0.3, "rec": 0.0, "blocked": 0} for ins in dbm.INSURERS}

    def next_id(self, prefix):
        self.counter[prefix] += 1
        return f"{prefix}_{self.counter[prefix]:05d}"

    def resume_counters(self):
        """Continue numbering after existing fake documents, so --live can run after a plain seed."""
        for prefix, coll in [("clm", dbm.CLAIMS), ("adj", dbm.ADJUDICATIONS), ("apl", dbm.APPEALS), ("evt", dbm.HARNESS_EVENTS)]:
            doc = self.db[coll].find_one({"fake": True, "_id": {"$regex": f"^{prefix}_9"}}, sort=[("_id", -1)], projection={"_id": 1})
            if doc:
                self.counter[prefix] = max(self.counter[prefix], int(doc["_id"].split("_")[1]))
        for ins in dbm.INSURERS:  # continue the metric curves instead of restarting them
            m = self.db[dbm.METRICS].find_one({"insurer": ins, "fake": True}, sort=[("ts", -1)])
            if m:
                self._metric_state[ins] = {"acc": m["acceptance_rate"], "win": m["appeal_win_rate"], "rec": m["recovered_usd"], "blocked": m["leaks_blocked"]}

    # --- documents -----------------------------------------------------

    def claim(self, insurer, created_at):
        r = self.rng
        n_lines = r.choice([1, 1, 2, 3])
        procs = r.sample(PROCEDURES, n_lines)
        lines = []
        for i, (code, _desc, price, _enc) in enumerate(procs, start=1):
            units = r.choice([1, 1, 1, 2, 4]) if code.startswith("J") else 1
            lines.append({"line_no": i, "hcpcs": code, "modifiers": r.choice([[], [], ["25"], ["59"]]), "units": units, "charge_usd": float(price * units)})
        has_auth = r.random() < 0.7
        auth = f"PA-{r.randint(100000, 999999)}"
        ref = f"NPI-{r.randint(1000000000, 1999999999)}"
        dob = datetime(r.randint(1940, 2005), r.randint(1, 12), r.randint(1, 28))
        return {
            "_id": self.next_id("clm"),
            "insurer": insurer,
            "patient": {
                "name": f"{r.choice(FIRST)} {r.choice(LAST)}",
                "dob": dob.strftime("%Y-%m-%d"),
                "member_id": f"M{r.randint(10000000, 99999999)}",
                "ssn": f"9{r.randint(10, 99)}-{r.randint(10, 99)}-{r.randint(1000, 9999)}",
                "address": f"{r.randint(1, 999)} Fake St, Springfield, {r.choice(STATES)}",
                "phone": f"555-{r.randint(100, 999)}-{r.randint(1000, 9999)}",
            },
            "service_date": (created_at - timedelta(days=r.randint(1, 30))).strftime("%Y-%m-%d"),
            "encounter_type": procs[0][3],
            "diagnosis_codes": r.sample(DIAGNOSES, r.choice([1, 2])),
            "lines": lines,
            "total_charge_usd": float(sum(l["charge_usd"] for l in lines)),
            "prior_auth_id": auth if has_auth else None,
            "referring_provider_id": ref if r.random() < 0.8 else None,
            "records": {"prior_auth_id": auth, "referring_provider_id": ref},
            "notes": "Fake claim written by seed_fake.py.",
            "holdout": r.random() < 0.2,
            "created_at": iso(created_at),
            **FAKE,
        }

    def insert_claim(self, claim):
        """Claims may be a Queryable Encryption collection; plaintext PHI is then refused, so drop it and retry."""
        if not self._claims_phi_ok:
            claim = {k: v for k, v in claim.items() if k not in ("patient", "notes")}
        try:
            self.db[dbm.CLAIMS].insert_one(claim)
        except Exception as exc:  # noqa: BLE001 - any encryption/validation error
            if not self._claims_phi_ok or type(exc).__name__ == "DuplicateKeyError":
                raise
            print(f"  claims refused plaintext patient fields ({type(exc).__name__}); seeding claims without them")
            self._claims_phi_ok = False
            self.insert_claim(claim)

    def verdict(self, adj, lean, created_at):
        r = self.rng
        if r.random() < 0.08:
            label = "needs_review"
        elif lean == "wrongful":
            label = "wrongful_bulk" if adj["insurer"] == "payer_b" and r.random() < 0.7 else "wrongful_policy"
        else:
            label = "legitimate" if r.random() < 0.9 else "wrongful_policy"
        clause_no = r.choice([2, 3, 5, 7, 8])
        ins = adj["insurer"]
        stats = {"identical_denials_60s": r.randint(20, 60) if label == "wrongful_bulk" else r.randint(0, 4), "median_latency_ms": adj["latency_ms"]}
        reasons = {
            "legitimate": f"{adj['carc']} matches a real gap in the claim; a prevention rule would fix it before submission.",
            "wrongful_policy": f"Clause {clause_no} of the {ins} policy covers this service; the {adj['carc']} denial contradicts it.",
            "wrongful_bulk": f"Clause {clause_no} covers this code; {stats['identical_denials_60s']} identical denials in 60 s, {adj['latency_ms']} ms after submission.",
            "needs_review": "Confidence below the insurer's threshold; left for a human.",
        }
        suggested = None
        if label == "legitimate" and adj["carc"] == "CO-197":
            suggested = {
                "condition": {"all": [{"field": "lines.hcpcs", "op": "eq", "value": "C8903"}, {"field": "prior_auth_id", "op": "missing"}]},
                "fix": {"action": "attach_prior_auth"},
            }
        comparables = self.db[dbm.ADJUDICATIONS].find({"insurer": ins, "status": "paid", "fake": True}, {"claim_id": 1}).limit(20)
        comp_ids = [c["claim_id"] for c in comparables]
        return {
            "label": label,
            "confidence": round(r.uniform(0.55, 0.7) if label == "needs_review" else r.uniform(0.76, 0.97), 2),
            "reason": reasons[label],
            "evidence": {
                "clause_ids": [f"{ins}_v1_c{clause_no}"],
                "comparable_claim_ids": r.sample(comp_ids, min(len(comp_ids), 2)),
                "pattern_stats": stats,
            },
            "suggested_rule": suggested,
            "model": "sonnet-5",
            "created_at": iso(created_at),
        }

    def adjudication(self, claim, submitted_at, deny=None):
        """Returns (adjudication, lean, adjudicated_at as datetime)."""
        r = self.rng
        ins = claim["insurer"]
        deny = r.random() < 0.35 if deny is None else deny
        latency = r.randint(700, 1900) if ins == "payer_b" and deny else r.randint(1500, 9000)
        adj = {
            "_id": self.next_id("adj"),
            "claim_id": claim["_id"],
            "insurer": ins,
            "attempt": 1,
            "status": "denied" if deny else "paid",
            "carc": None,
            "rarc": None,
            "denial_text": None,
            "paid_amount_usd": 0.0 if deny else round(claim["total_charge_usd"] * r.uniform(0.6, 0.9), 2),
            "submitted_at": submitted_at,
            "adjudicated_at": submitted_at + timedelta(milliseconds=latency),
            "latency_ms": latency,
            "verdict": None,
            "holdout": claim["holdout"],
            **FAKE,
        }
        lean = None
        if deny:
            carc, rarc, text, lean = r.choice(DENIALS[:1] * 2 + DENIALS[1:]) if ins != "payer_b" else r.choice(DENIALS[:1] * 4 + DENIALS[1:])
            adj.update(carc=carc, rarc=rarc, denial_text=f"{carc} {rarc}: {text}")
        return adj, lean, submitted_at + timedelta(milliseconds=latency)

    def appeal(self, adj, claim, verdict, created_at, outcome="random"):
        r = self.rng
        ev = verdict["evidence"]
        profile = self.db[dbm.HARNESS_PROFILES].find_one({"_id": adj["insurer"]}) or default_profile(adj["insurer"])
        mode = profile.get("permissions", {}).get("appeals", "draft_only")
        if outcome == "random":
            outcome = None if mode == "draft_only" and r.random() < 0.4 else r.choice(["overturned", "overturned", "upheld"])
        letter = (
            f"Re: claim {claim['_id']} for PATIENT_{r.randint(1000, 9999)}, age band {r.choice(['30-39', '40-49', '50-59', '60-69'])}, service day 0.\n\n"
            f"We request reconsideration of the {adj['carc']} denial. {verdict['reason']}\n\n"
            f"Policy clause cited: {', '.join(ev['clause_ids'])}.\n"
            f"Comparable paid claims: {', '.join(ev['comparable_claim_ids']) or 'none'}.\n"
            f"Denial pattern: {ev['pattern_stats']['identical_denials_60s']} identical denials in 60 s; median latency {ev['pattern_stats']['median_latency_ms']} ms.\n\n"
            "Billing operations request only; no clinical judgment is expressed."
        )
        return {
            "_id": self.next_id("apl"),
            "claim_id": claim["_id"],
            "adjudication_id": adj["_id"],
            "insurer": adj["insurer"],
            "verdict": verdict["label"],
            "confidence": verdict["confidence"],
            "evidence": ev,
            "letter_tokenized": letter,
            "mode": mode,
            "status": "pending_approval" if outcome is None else "filed",
            "outcome": outcome,
            "recovered_usd": round(claim["total_charge_usd"] * 0.85, 2) if outcome == "overturned" else 0.0,
            "created_at": created_at,
            **FAKE,
        }

    def event(self, ts, insurer=None):
        r = self.rng
        insurer = insurer or r.choice(dbm.INSURERS)
        kind = r.choice(["profile", "profile", "rule_proposed", "rule_promoted", "rule_rejected", "rule_retired", "rollback", "phi_blocked", "guardrail_added", "permission_changed"])
        eid = self.next_id("evt")
        base = {"_id": eid, "ts": ts, "insurer": insurer, "evidence_ids": [f"adj_{r.randint(90001, 90050):05d}" for _ in range(r.randint(1, 3))], "reverted": False, **FAKE}
        rule = {"condition": {"all": [{"field": "lines.hcpcs", "op": "eq", "value": r.choice(["G0439", "C8903", "E0601"])}, {"field": "prior_auth_id", "op": "missing"}]}, "fix": {"action": "attach_prior_auth"}}
        if kind == "profile":
            field, before, after, reason = r.choice([
                ("context_policy.paid_comparables", 2, 5, "Appeals citing 2+ paid comparables were overturned 7/9 times vs 1/6 without."),
                ("judge.bulk_min_identical", 20, 12, "Missed bulk denials arrived in groups of 12-18; a lower threshold would have caught 9 of 11."),
                ("appeal_strategy.lead_with", "clause", "paid_comparables", "Overturn rate rises when comparables lead the letter."),
                ("appeal_strategy.quote_clause_verbatim", False, True, "Paraphrased clause citations were upheld 5 times in a row."),
                ("judge.min_confidence", 0.75, 0.82, "4 of the last 10 wrongful calls under 0.82 were upheld on appeal."),
            ])
            return {**base, "actor": "evolver", "type": "profile_changed", "before": {field: before}, "after": {field: after}, "reason": reason}
        if kind.startswith("rule_"):
            reasons = {
                "rule_proposed": "3 similar CO-197 denials for the same code with no prior-auth id.",
                "rule_promoted": "Replay: prevents 11 past denials, blocks 2% of past paid claims.",
                "rule_rejected": "Replay: would block 14% of past paid claims (limit 5%).",
                "rule_retired": "Precision 70% over its last 20 uses (limit 80%).",
            }
            actor = "judge" if kind == "rule_proposed" else "validator"
            st = {"rule_proposed": (None, "candidate"), "rule_promoted": ("shadow", "active"), "rule_rejected": ("shadow", "rejected"), "rule_retired": ("active", "retired")}[kind]
            return {**base, "actor": actor, "type": kind, "before": {"status": st[0]} if st[0] else None, "after": {"status": st[1], "rule": rule}, "reason": reasons[kind]}
        if kind == "rollback":
            return {**base, "actor": "evolver", "type": "rollback", "before": {"judge.min_confidence": 0.9}, "after": {"judge.min_confidence": 0.8}, "reason": "Wrongful-denial recall fell from 74% to 58% in the cycle after the change.", "reverted": True}
        if kind == "phi_blocked":
            return {**base, "actor": "firewall", "type": "phi_blocked", "before": None, "after": {"pattern": r.choice(["ssn", "phone", "dob"]), "direction": "request"}, "reason": "Planted PHI in free-text notes matched the leak detector; call blocked."}
        if kind == "guardrail_added":
            return {**base, "actor": "evolver", "type": "guardrail_added", "before": {"guardrails.learned": []}, "after": {"guardrails.learned": ["drop_notes_field"]}, "reason": "2 leaks blocked from the notes field this hour; stop sending notes for this insurer."}
        return {**base, "actor": "judge", "type": "permission_changed", "before": {"permissions.appeals": "draft_only"}, "after": {"permissions.appeals": "auto_file"}, "reason": "10 wrongful calls in a row confirmed by overturned appeals."}

    def metric(self, ts, insurer, step=True):
        r = self.rng
        s = self._metric_state[insurer]
        if step:
            s["acc"] = min(0.93, s["acc"] + r.uniform(-0.004, 0.012))
            s["win"] = min(0.8, s["win"] + r.uniform(-0.01, 0.02))
            s["rec"] += r.choice([0, 0, 0, r.uniform(200, 2500)])
            s["blocked"] += 1 if r.random() < 0.05 else 0
        return {
            "ts": ts,
            "insurer": insurer,
            "acceptance_rate": round(s["acc"], 4),
            "judge_precision": round(r.uniform(0.82, 0.93), 4),
            "judge_recall": round(r.uniform(0.66, 0.8), 4),
            "appeal_win_rate": round(s["win"], 4),
            "recovered_usd": round(s["rec"], 2),
            "phi_leaks_to_llm": 0,
            "leaks_blocked": s["blocked"],
            "cost_usd_per_claim": round(r.uniform(0.004, 0.012), 4),
            **FAKE,
        }

    # --- bulk seeding -------------------------------------------------

    def seed_profiles_and_policies(self):
        for ins in dbm.INSURERS:
            self.db[dbm.HARNESS_PROFILES].update_one({"_id": ins}, {"$setOnInsert": {**default_profile(ins), "updated_by": "seed", **FAKE}}, upsert=True)
            if not self.db[dbm.POLICIES].find_one({"insurer": ins}):
                self.db[dbm.POLICIES].insert_many([
                    {
                        "_id": f"{ins}_v1_c{n}",
                        "insurer": ins,
                        "version": 1,
                        "clause_no": n,
                        "title": title,
                        "clause_text": f"{ins.replace('_', ' ').title()} clause {n} ({title}): placeholder text from seed_fake.py until the real policy is loaded.",
                        "current": True,
                        **FAKE,
                    }
                    for n, title in enumerate(POLICY_TITLES, start=1)
                ])

    def seed(self, n_claims):
        t0 = now() - timedelta(minutes=30)
        self.seed_profiles_and_policies()
        pending = []
        for i in range(n_claims):
            ts = t0 + timedelta(seconds=i * (1800 / n_claims))
            claim = self.claim(self.rng.choice(dbm.INSURERS), ts)
            self.insert_claim(claim)
            adj, lean, adj_at = self.adjudication(claim, ts)
            self.db[dbm.ADJUDICATIONS].insert_one(adj)
            if adj["status"] == "denied":
                pending.append((adj, claim, lean, adj_at))
        # verdicts after all paid claims exist, so comparables resolve
        n_appeals = 0
        for adj, claim, lean, adj_at in pending:
            v = self.verdict(adj, lean, adj_at + timedelta(seconds=4))
            self.db[dbm.ADJUDICATIONS].update_one({"_id": adj["_id"]}, {"$set": {"verdict": v}})
            if v["label"].startswith("wrongful"):
                self.db[dbm.APPEALS].insert_one(self.appeal(adj, claim, v, adj_at + timedelta(seconds=20)))
                n_appeals += 1
        n_events = 18
        self.db[dbm.HARNESS_EVENTS].insert_many([self.event(t0 + timedelta(seconds=i * 100)) for i in range(n_events)])
        dbm.ensure_metrics_collection(self.db)
        metrics = []
        if not self.db[dbm.METRICS].find_one(FAKE):  # a second history would overlap the first
            metrics = [self.metric(t0 + timedelta(seconds=30 * i), ins) for i in range(60) for ins in dbm.INSURERS]
            self.db[dbm.METRICS].insert_many(metrics)
        print(f"Seeded {n_claims} claims, {n_claims} adjudications ({len(pending)} denied), {n_appeals} appeals, {n_events} events, {len(metrics)} metrics points")

    def live(self, interval):
        """Keep writing activity so the SSE panels move. Mirrors the real loop's write pattern: insert, then update."""
        print(f"Writing fake activity every {interval}s. Ctrl+C to stop.")
        waiting = []  # (due_time, fn)
        last_metrics = last_event = time.time()
        while True:
            t = now()
            claim = self.claim(self.rng.choice(dbm.INSURERS), t)
            self.insert_claim(claim)
            adj, lean, _ = self.adjudication(claim, t)
            self.db[dbm.ADJUDICATIONS].insert_one(adj)
            if adj["status"] == "denied":
                waiting.append((time.time() + 3, self._judge_later(adj, claim, lean)))
            for item in [w for w in waiting if w[0] <= time.time()]:
                waiting.remove(item)
                item[1]()
            if time.time() - last_event > 12:
                self.db[dbm.HARNESS_EVENTS].insert_one(self.event(now()))
                last_event = time.time()
            if time.time() - last_metrics > 5:
                self.db[dbm.METRICS].insert_many([self.metric(now(), ins) for ins in dbm.INSURERS])
                last_metrics = time.time()
            time.sleep(interval)

    def _judge_later(self, adj, claim, lean):
        def run():
            v = self.verdict(adj, lean, now())
            self.db[dbm.ADJUDICATIONS].update_one({"_id": adj["_id"]}, {"$set": {"verdict": v}})
            if v["label"].startswith("wrongful"):
                self.db[dbm.APPEALS].insert_one(self.appeal(adj, claim, v, now()))

        return run


def clear(db):
    for coll in [dbm.CLAIMS, dbm.ADJUDICATIONS, dbm.APPEALS, dbm.HARNESS_EVENTS, dbm.METRICS, dbm.POLICIES, dbm.HARNESS_PROFILES]:
        n = db[coll].delete_many(FAKE).deleted_count
        print(f"  {coll}: removed {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claims", type=int, default=50)
    ap.add_argument("--seed", type=int, default=None, help="random seed, for repeatable data")
    ap.add_argument("--live", action="store_true", help="after seeding, keep writing new activity")
    ap.add_argument("--live-only", action="store_true", help="skip the initial seed; only write live activity")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between new claims in --live mode")
    ap.add_argument("--clear", action="store_true", help="remove every document with fake: true, then exit")
    args = ap.parse_args()

    db = dbm.get_db()
    if args.clear:
        clear(db)
        return
    seeder = Seeder(db, random.Random(args.seed))
    seeder.resume_counters()
    if not args.live_only:
        seeder.seed(args.claims)
    if args.live or args.live_only:
        try:
            seeder.live(args.interval)
        except KeyboardInterrupt:
            print("stopped")


if __name__ == "__main__":
    main()
