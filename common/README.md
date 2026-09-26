# common/ (owner: B)

Shared code every other folder imports. **Only B edits this folder**; ask B for changes. Full specs: [PLAN.md, Member B](../docs/PLAN.md#member-b-mongodb-and-security).

## Files to create

| File | What it provides | Stub (by 11:30) | Real by |
| --- | --- | --- | --- |
| `db.py` | `get_db(role="agent_worker")` returning the `denialfighter` database; collection name constants | Real from the start | 11:00 |
| `models.py` | Pydantic models: `Claim`, `TokenizedClaim`, `Adjudication`, `Verdict`, `Rule`, `ReplayStats`, `Appeal`, `HarnessProfile`, `Event` (fields in PLAN.md) | Real; **frozen at 11:00** | 11:00 |
| `rules.py` | `matches(rule, claim) -> bool`, `to_match(rule) -> dict` (MongoDB `$match`), `apply_fix(rule, claim) -> claim` (refuses changes to diagnosis codes or charges) | Real (small) | 11:30 |
| `search.py` | `vector_search(collection, query, filters, k=5) -> list[dict]` using Automated Embeddings | Returns fixed results from a JSON file | 12:30 |
| `llm.py` | `call_llm(model, system, prompt) -> str` via OpenRouter; runs `firewall.guard()` on input and output; LangSmith tracing | Returns a canned JSON string per model | 12:30 |
| `profiles.py` | `get_profile(insurer)`, `watch(callback)` (change stream on `harness_profiles`) | `watch` polls every 5 s | 2:30 |

## Used by

Everyone. `rules.py` is shared by C's scrubber and B's validator, so both apply exactly the same rule logic.
