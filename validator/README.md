# validator/ (owner: B)

Proves a rule before it goes live, by replaying it on past claims with aggregation pipelines. Lifecycle: [PLAN.md, rule lifecycle](../docs/PLAN.md#rule-lifecycle).

## Files to create

| File | What it provides | Stub (by 11:30) | Real by |
| --- | --- | --- | --- |
| `replay.py` | `replay(rule) -> ReplayStats`: uses `common.rules.to_match(rule)` over the insurer's past claims; counts denials it would have prevented and paid claims it would wrongly block | Always returns `decision: promote` | 2:30 |
| `lifecycle.py` | Moves rules candidate → shadow → active / rejected, and active → retired (precision under 80% over the last 20 uses, or unused for 30 minutes); logs each change to `harness_events` | n/a | 2:30 |
| `threshold_replay.py` | Replays past verdicts with known appeal outcomes, so the Evolver may change Judge thresholds only if the replay improves | n/a | 3:30 |

## Promotion rule

Promote when the rule prevents 3 or more past denials **and** would block 5% or fewer of past paid claims.

## Used by

C's Judge (rule proposals) and Evolver (threshold changes).
