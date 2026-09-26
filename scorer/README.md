# scorer/ — the honest scoreboard (Role A)

The only code that reads `sim_truth`. Every 30 seconds it writes one document per insurer plus an
`insurer: "all"` row into the time-series `metrics` collection, which D's scoreboard panel reads.

```bash
python -m scorer                    # every 30 s, forever
python -m scorer --once --dry-run   # print once, write nothing
python -m scorer --interval 10 --window 60
python -m pytest scorer/tests
```

| Field | How it is computed |
|---|---|
| `acceptance_rate` | share paid on first submission among the most recent 60 **held-out** claims (180 for `all`) |
| `judge_precision` | of the denials the Judge called wrongful, the share that really were wrongful |
| `judge_recall` | of the really wrongful denials the Judge looked at, the share it called wrongful |
| `appeal_win_rate` | overturned / all appeal decisions recorded by the simulator |
| `recovered_usd` | money paid on overturned appeals |
| `phi_leaks_to_llm` | stored LLM outputs (verdicts, harness events, rules, appeal letters) that contain a planted PHI value. Must be 0 |
| `leaks_blocked` | `phi_incidents` logged by the firewall |
| `cost_usd_per_claim` | `None` until LLM costs are logged somewhere |
| `holdout_sample`, `judged_denials`, `appeals_decided` | sample sizes, so a reader knows how much to trust the numbers |

Verdicts are joined to the ground truth by `adjudications._id` (the simulator's `adjudication_id`),
falling back to the claim's latest denial. Appeal letters are checked as stored (`letter_tokenized`,
before `render_letter` adds the real name back), so a patient name in one counts as a leak.
LLM prompts aren't stored, so requests are covered by the firewall blocking them (`leaks_blocked`),
not by `phi_leaks_to_llm`.

MongoDB role: `scorer` (`MONGODB_URI_SCORER`, falls back to `MONGODB_URI`).
`compute_metrics()` in `score.py` is pure (lists in, documents out) if you want to check a number by hand.
