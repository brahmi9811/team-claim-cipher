# agents/ (owner: C)

The four LLM agents. Every LLM call goes through `common.llm.call_llm` (never call OpenRouter directly), so the PHI firewall always applies. Full specs: [PLAN.md, Member C](../docs/PLAN.md#member-c-agents).

## Files to create

| File | What it provides | Model | Stub (by 11:30) | Real by |
| --- | --- | --- | --- | --- |
| `scrubber.py` | `scrub(tokenized_claim, rules) -> tokenized_claim`: applies active rules via `common.rules.apply_fix` | Haiku 4.5 (only for uncertain cases) | Returns claim unchanged | 12:30 |
| `judge.py` | `classify(adjudication) -> Verdict`: retrieves policy clauses and similar claims, adds bulk-pattern stats, returns the Verdict JSON | Sonnet 5 | Random verdict | 12:30 |
| `rule_proposer.py` | Groups similar legitimate denials (Vector Search); at 3 or more, proposes a candidate rule in B's rule format and calls `validator.replay` | Sonnet 5 | n/a | 2:30 |
| `appeal_writer.py` | `draft(verdict) -> Appeal`: letter from evidence, following the insurer's appeal strategy | Sonnet 5 | Template letter | 1:00 |
| `evolver.py` | `run_cycle(insurer) -> Event or None`: one profile change per cycle, within the limits table in PLAN.md; automatic rollback | Sonnet 5 | Returns None | 3:30 |
| `trust_ladder.py` | Draft-only → auto-file after 10 correct wrongful calls in a row; back after 2 upheld appeals in a row | None | n/a | 3:30 |
| `prompts/` | `scrubber.md`, `judge.md`, `appeal_writer.md`, `evolver.md`, each ending with the exact JSON output format | n/a | Drafts | 12:30 |

## Rules

- The Evolver may never change `permissions` directly or touch `guardrails.fixed`.
- Every decision is logged to `harness_events`.
