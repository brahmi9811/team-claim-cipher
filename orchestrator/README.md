# orchestrator/ (owner: C)

One async Python process that runs the whole loop as workers. See [PLAN.md, the four loops](../docs/PLAN.md#the-four-loops).

## Files to create

| File | What it does | By |
| --- | --- | --- |
| `run.py` | Entry point: starts the workers below; `CLAIM_RATE_PER_SEC` controls speed | 11:30 (on stubs) |
| `submit_worker.py` | Takes the next claim → `firewall.tokenize` → `scrubber.scrub` → `POST /submit` → writes an `adjudications` document | 11:30 on stubs, 12:00 real sim |
| `judge_worker.py` | For each denial → `judge.classify` → stores the verdict; legitimate → `rule_proposer`; wrongful → `appeal_writer` | 12:30 |
| `appeal_worker.py` | Files appeals (auto-file mode) or waits for approval from the live view (draft-only mode) → `POST /appeal` → stores the outcome | 1:00 |
| `evolve_worker.py` | Calls `evolver.run_cycle` per insurer every 5 minutes or 50 outcomes | 3:30 |

## Rules

- Workers can restart at any time. All state lives in MongoDB, never in memory.
- Retries with backoff on LLM and simulator errors; never crash the loop.
- `scripts/smoke.py` (also C's) pushes one claim through all of this and must pass before any merge to `main` after 1:00 PM.
