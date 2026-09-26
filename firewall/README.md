# firewall/ (owner: B)

The PHI firewall: no real patient identifier ever reaches the LLM. Full spec: [PLAN.md, PHI firewall](../docs/PLAN.md#phi-firewall-security-layer).

## Files to create

| File | What it provides | Stub (by 11:30) | Real by |
| --- | --- | --- | --- |
| `encryption.py` | Queryable Encryption setup for `claims` and `phi_tokens`; key vault `encryption.__keyVault`; local key file from `QE_KEY_FILE`; **`encrypted_collection(name)`** for forge writes | n/a | 12:00 (timebox; else Client-Side Field Level Encryption) |
| `tokenize.py` | `tokenize(claim) -> TokenizedClaim`: patient replaced by a token, age band and state; dates made relative; notes removed or redacted | Fixed token, notes removed | 12:30 |
| `guard.py` | `guard(text, claim_id=None) -> text`; raises `PHILeak` on a hit, logs to `phi_incidents` | Returns text unchanged | 12:30 |
| `render.py` | `render_letter(tokenized_letter, claim_id) -> letter`: puts real values back, in plain code, after all LLM calls | Returns input unchanged | 1:00 |
| `guardrails.py` | Reads learned guardrails from the insurer's profile (for example `drop_notes_field`); fixed list of guardrails the Evolver may add | n/a | 3:30 |

## Rules

- Only the firewall process holds the key file.
- `guard()` runs on **every** LLM request and response (called from `common/llm.py`).
