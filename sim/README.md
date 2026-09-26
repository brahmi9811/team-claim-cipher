# sim/ — the insurer simulator (Role A)

A FastAPI service that plays three insurers. It decides every claim with **hidden rules**
(no LLM, fully deterministic), records the ground truth in `sim_truth`, and decides appeals
with each insurer's evidence rule. Agents only ever see what a real insurer returns: status,
CARC/RARC codes and denial text.

```bash
python -m sim                  # http://localhost:8001, ledger in MongoDB if MONGODB_URI is set
python -m sim --memory         # ledger in memory (no MongoDB needed; sim_truth not persisted)
python -m sim --stub           # the 11:15 stub: random but valid responses
python -m sim.report           # denial mix of data/forge/claims.jsonl (cold start, ceiling, Payer C v2)
python -m pytest sim/tests
```

| Env var | Default | Meaning |
|---|---|---|
| `SIM_MODE` | `real` | `stub` for random but valid responses |
| `SIM_STORE` | `mongo` | `memory` keeps the ledger in memory even when `MONGODB_URI` is set |
| `SIM_ADMIN_TOKEN` | unset | if set, `/admin/*` needs header `X-Admin-Token` |
| `MONGODB_URI_SIMULATOR` / `MONGODB_URI` | | database user for the `simulator` role (`common/db.py`) |

## Endpoints

### `POST /submit`

Body: a tokenized claim (`TokenizedClaim` in `common/models.py`). A raw `Claim` also works
(`service_date` and `created_at` are used instead of `service_day`).

```json
{
  "claim_id": "clm_00042", "adjudication_id": "adj_clm_00042_1", "_adjudication_id": "adj_clm_00042_1",
  "attempt": 1, "status": "denied", "carc": "CO-16", "rarc": "M62",
  "denial_text": "CO-16 / M62: Claim/service lacks information or has submission/billing error(s). Missing/incomplete/invalid treatment authorization code. Service line 1 (C8901 MRA abdomen without contrast).",
  "paid_amount": 0.0, "adjudicated_at": "2026-09-26T14:03:11.402238+00:00", "latency_ms": 5719
}
```

- `adjudication_id` is `adj_{claim_id}_{attempt}`. The orchestrator uses `_adjudication_id` as its
  `adjudications._id`, so the scorer can join verdicts to the ground truth.
- Resubmitting the same claim is attempt 2, 3, ... A claim that was already paid comes back `CO-18` (duplicate).
- Payer B's automated denials also carry `batch_id`; their `adjudicated_at` is shared by the whole batch
  (2-second windows) and `latency_ms` is under 2,000.
- 422 for a claim without `_id` or with an unknown insurer.

### `POST /appeal`

```json
{"claim_id": "clm_00042", "adjudication_id": "adj_clm_00042_1", "letter": "...",
 "cited_clause_ids": ["payer_a_v1_c7"], "comparable_claim_ids": ["clm_00017", "clm_00031"],
 "pattern_stats": {"identical_denials_60s": 14}}
```

`adjudication_id` is optional (default: the latest denial of the claim). Response:

```json
{"appeal_id": "simapl_adj_clm_00042_1_1", "claim_id": "clm_00042", "adjudication_id": "adj_clm_00042_1",
 "outcome": "overturned", "paid_amount": 119.0, "decided_at": "...", "note": "..."}
```

| Insurer | Overturns a wrongful denial when the appeal ... |
|---|---|
| Payer A | cites the violated clause: `payer_a_v*_cN` in `cited_clause_ids`, or "clause N" in the letter |
| Payer B | lists 2 or more claims Payer B **paid** (claim or adjudication ids) **and** sends `pattern_stats` with a positive number |
| Payer C | quotes the violated clause word for word from the **current** policy version (case, spacing, quote style ignored) |

A legitimate denial is always upheld. Once overturned, later appeals return the same result (no double payment).
404 if the claim has no denial.

### `POST /admin/policy-change/{payer}` and `POST /admin/policy-reset/{payer}`

Change moves an insurer to its next policy version (only Payer C has one: v1 to v2), 409 if it is
already on the latest. Reset goes back to v1. Both swap the hidden rules and republish the
`policies` collection (`current: true` on the new version):

```json
{"insurer": "payer_c", "policy_version": 2, "policy_version_id": "payer_c_v2", "previous_version": 1,
 "clauses": 12, "changed_clauses": [4, 5, 6, 7]}
```

### `GET /health`

`{"ok": true, "mode": "real", "policy_versions": {"payer_a": 1, ...}, "store": "mongo"}`

## Published policies

`sim/policies/payer_a.md`, `payer_b.md`, `payer_c.md`, `payer_c_v2.md`: 12 clauses each, loaded into
`policies` by `python -m forge load` as `{_id: "payer_a_v1_c7", insurer, version, clause_no, title, clause_text, current}`.
Every wrongful behavior contradicts one of these clauses, so the Judge can find the evidence.

## Identifier formats (for B's leak detector)

| Field | Format |
|---|---|
| Member ID | Payer A `PAM#########`, Payer B `BXH-########`, Payer C `PC##########` |
| Phone | `(508) 555-01NN` style (area codes 508, 617, 781, 978, 413) |
| Prior authorization | `PA-XXXXXX` (6 letters/digits); not PHI |
| Referring provider | `PRV-######`; not PHI |

Denial texts contain no dates or patient data.

## Files

| File | What it does |
|---|---|
| `engine.py` | `Simulator`: submit, appeal, policy change/reset |
| `rules/` | the hidden rules per insurer and version (`base.py` has the format and helpers) |
| `policies.py` | parses the policy markdown into clause documents |
| `store.py` | the ledger: `MongoLedger` (`sim_truth`, `policies`) or `MemoryLedger` |
| `carc.py` | CARC/RARC texts |
| `stub.py` | random but valid responses |
| `app.py` | the HTTP layer |
| `report.py` | the tuning report |

**Agents must never import `sim/` or read `sim_truth`.** Only the scorer reads the ground truth.
