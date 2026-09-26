#!/usr/bin/env python3
"""Member C's smoke test (docs/PLAN.md, Role C: "Done when: scripts/smoke.py
passes on main, and a profile change made by the Evolver shows up in
harness_events with a reason.").

Pushes claims through the whole loop -- Learn, Judge, Fight, Evolve -- and
checks the observable result at each stage.

Requires `MONGODB_URI` (see `.env.example`): every real module this depends
on (common, firewall, validator) talks to MongoDB, so this is a real
integration test now, not an offline unit test. It runs in its own database
(`SMOKE_MONGODB_DB`, default `denialfighter_smoke`), dropped at the end, so
the rules and appeals the live demo has learned can't change its result and
the demo database is never touched.

    python scripts/smoke.py
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MONGODB_DB"] = os.environ.get("SMOKE_MONGODB_DB", "denialfighter_smoke")

from common import profiles  # noqa: E402
from common.models import RuleStatus, new_appeal, new_verdict  # noqa: E402

from agents import appeal_writer, evolver  # noqa: E402
from orchestrator.contracts import sim_client  # noqa: E402
from orchestrator.db import coll, utcnow  # noqa: E402
from orchestrator.pipeline import process_claim  # noqa: E402
from orchestrator.seed import (  # noqa: E402
    missing_prior_auth_claim,
    seed_policies,
    wrongful_policy_claim,
)

# This is a test of C's own code path (Scrubber/Judge/Appeal Writer/Evolver),
# not of A's simulator -- A's real payer_a wrongful rule is probabilistic
# ("about 10% of cases"), which would make this flaky if it happened to be
# running. Pointing SIM_URL somewhere unreachable forces the deterministic
# local fallback in orchestrator/contracts/sim_client.py every time.
sim_client.SIM_URL = "http://127.0.0.1:1"

_PASS, _FAIL = [], []
_TRACKED_COLLECTIONS = ["claims", "adjudications", "rules", "appeals", "harness_events"]


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        _PASS.append(label)
        print(f"  PASS  {label}")
    else:
        _FAIL.append(label)
        print(f"  FAIL  {label}  {detail}")


def _snapshot(insurers: list[str]) -> dict:
    return {
        "ids": {c: {ins: {d["_id"] for d in coll(c).find({"insurer": ins}, {"_id": 1})} for ins in insurers} for c in _TRACKED_COLLECTIONS},
        "profiles": {ins: coll("harness_profiles").find_one({"_id": ins}) for ins in insurers},
        "evolver_state": {ins: coll("evolver_state").find_one({"_id": ins}) for ins in insurers},
    }


def _restore(snap: dict, insurers: list[str]) -> None:
    for c in _TRACKED_COLLECTIONS:
        for ins in insurers:
            before_ids = snap["ids"][c][ins]
            after_ids = {d["_id"] for d in coll(c).find({"insurer": ins}, {"_id": 1})}
            new_ids = list(after_ids - before_ids)
            if new_ids:
                coll(c).delete_many({"_id": {"$in": new_ids}})
    for ins in insurers:
        if snap["profiles"][ins] is None:
            coll("harness_profiles").delete_one({"_id": ins})
        else:
            coll("harness_profiles").replace_one({"_id": ins}, snap["profiles"][ins], upsert=True)
        if snap["evolver_state"][ins] is None:
            coll("evolver_state").delete_one({"_id": ins})
        else:
            coll("evolver_state").replace_one({"_id": ins}, snap["evolver_state"][ins], upsert=True)


def phase_1_learn() -> None:
    """3 identical fixable denials should cluster into a candidate rule,
    which replay should promote to active; the 4th matching claim should
    then be auto-fixed and paid."""
    print("\n[Phase 1] Learn: cluster -> candidate rule -> replay -> active -> auto-fix")
    rng = random.Random(1)
    insurer = "payer_c"

    for i in range(3):
        summary = process_claim(missing_prior_auth_claim(insurer, rng))
        check(f"claim {i + 1}/3 denied CO-16 (missing prior auth)", summary["status"] == "denied", summary)

    rule = coll("rules").find_one({"insurer": insurer})
    check("a candidate rule was proposed from the cluster", rule is not None)
    if rule:
        check("the rule was promoted to active by replay", rule["status"] == RuleStatus.ACTIVE.value, rule.get("replay"))

    fixed_summary = process_claim(missing_prior_auth_claim(insurer, rng))
    check("the active rule auto-fixed the 4th claim", bool(fixed_summary["applied_rule_ids"]))
    check("the auto-fixed claim was paid", fixed_summary["status"] == "paid", fixed_summary)


def phase_2_judge_and_fight() -> None:
    """A denial that contradicts the insurer's own published clause should
    be judged wrongful_policy and win on appeal once filed. Also plants 3
    deliberately weak (evidence-free) appeals so Phase 3 has the losing
    appeal_win_rate it needs -- appeal_win_rate is the metric a
    judge.min_confidence change is actually supposed to move, unlike
    acceptance_rate (see agents/evolver.py)."""
    print("\n[Phase 2] Judge + Fight: wrongful denial -> appeal -> overturned")
    rng = random.Random(2)
    insurer = "payer_a"

    for i in range(2):
        summary = process_claim(wrongful_policy_claim(rng))
        check(f"claim {i + 1}/2 denied CO-50 (not medically necessary)", summary["status"] == "denied", summary)
        check(f"claim {i + 1}/2 judged wrongful_policy", summary.get("verdict") == "wrongful_policy", summary)
        appeal = coll("appeals").find_one({"_id": summary.get("appeal_id")})
        check(f"claim {i + 1}/2 has a drafted appeal citing a clause", bool(appeal and appeal["evidence"]["clause_ids"]))
        filed = appeal_writer.file(appeal)
        check(f"claim {i + 1}/2 appeal overturned", filed["outcome"] == "overturned", filed)

    for i in range(3):
        weak_verdict = new_verdict(
            label="wrongful_policy", confidence=0.76, reason="smoke test: deliberately weak evidence",
            evidence={"clause_ids": [], "comparable_claim_ids": [], "pattern_stats": {}},
        )
        weak_appeal = new_appeal(
            claim_id=f"smoke_weak_{i}", insurer=insurer, verdict=weak_verdict,
            letter_tokenized="smoke test appeal with no cited evidence", mode="auto_file",
        )
        weak_appeal["created_at"] = utcnow()
        coll("appeals").insert_one(weak_appeal)
        filed = appeal_writer.file(weak_appeal)
        check(f"weak appeal {i + 1}/3 upheld (no evidence to win on)", filed["outcome"] == "upheld", filed)


def phase_3_evolve() -> None:
    """2 wins + 3 losses (40% win rate) should be enough for one bounded,
    explained profile change -- gated through validator.threshold_replay
    when the change is judge.min_confidence -- and it must be visible in
    harness_events, per the plan's own "done when" bar."""
    print("\n[Phase 3] Evolve: a losing metric should produce one explained, gated profile change")
    insurer = "payer_a"
    before_profile = profiles.get_profile(insurer)

    event = evolver.run_cycle(insurer)
    check("the Evolver produced an event", event is not None)
    if not event:
        return

    check("the event type is profile_changed or guardrail_added", event["type"] in ("profile_changed", "guardrail_added"), event)
    check("the event has a non-empty reason", bool(event.get("reason")))
    stored = coll("harness_events").find_one({"_id": event["_id"]})
    check("the event is recorded in harness_events", stored is not None)

    after_profile = profiles.get_profile(insurer)
    check("the profile version was bumped", after_profile["version"] > before_profile["version"])


def main() -> int:
    insurers = ["payer_c", "payer_a"]
    snap = _snapshot(insurers)
    try:
        seed_policies()  # additive/idempotent -- left in place, never cleaned up
        phase_1_learn()
        phase_2_judge_and_fight()
        phase_3_evolve()
    finally:
        _restore(snap, insurers)
        from common import db as dbmod

        if dbmod.DB_NAME != "denialfighter":
            smoke_db = dbmod.get_raw_db()
            for name in smoke_db.list_collection_names():  # Atlas users may lack dropDatabase
                smoke_db.drop_collection(name)

    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed.")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
