# forge/ — the Data Forge (Role A)

Turns the Synthea synthetic-patient sample into about 2,000 realistic claims in the frozen `Claim`
shape (`common/models.py`), plants red-team PHI, and loads everything into MongoDB.

```bash
python -m forge download      # Synthea sample CSVs (about 6 MB) -> data/synthea/csv   (committed, team decision)
python -m forge build         # 2,000 claims -> data/forge/claims.jsonl (+ phi_canaries.jsonl)
python -m forge load          # claims (encrypted), policies v1, hashed PHI canaries -> MongoDB
python -m forge policies      # only (re)publish the policies
python -m forge all           # download if needed, build, load
python -m forge extend --rounds 4   # append rounds 2..4 for a long run, then `load` again
python -m pytest forge/tests
```

Options: `--n 2000` (per round), `--seed 42`, `--phi-share 0.05`, `--rounds 4`, `--allow-plaintext` (local dev database only).
Building is deterministic for a given seed. Loading is safe to run twice (only missing claims are inserted).

**Long runs.** At 2 claims per second the orchestrator drains 2,000 claims in about 17 minutes, then falls
back to its own demo claims, which are never held out, so the acceptance curve stops moving. `extend`
appends more rounds (a different seed per round, so different samples and paperwork gaps, each with its
own 20% holdout and 5% planted PHI). Round 1 keeps `clm_00001`; later rounds are `clm_r02_00001`, ...,
which sort after it, so the orchestrator's `_id` cursor picks them up even mid-run.

## What a claim looks like

- **Codes:** 20 ICD-10-CM diagnoses and 20 HCPCS Level II procedures (`codes.py`). No CPT.
- **Mapping:** encounter reason (or the patient's known conditions) to a diagnosis; visit type
  (wellness, ambulatory, emergency, inpatient) to the services billed.
- **Paperwork:** the hospital's `records` hold the prior authorization and referring provider it got;
  billing staff copy them onto the claim most of the time but not always. That gap is what the
  scrubber learns to close (`attach_prior_auth`, `attach_referring_provider`).
- **Dates:** service dates are moved onto the last 85 days; about 2.5% are filed late (95 to 150 days).
- **Insurer:** one insurer per patient per half-year of the Synthea record.
- **Holdout:** 20% of claims have `holdout: true`. The scorer measures acceptance on these only.
- **PHI:** 5% of claims get a realistic clerk's note with the patient's DOB, phone, SSN, name or
  member ID. The planted values are stored in `sim_truth` as salted hashes only
  (`type: "phi_canary"`), so the scorer can detect a leak without keeping PHI in plain text.

Tuning knobs are in `claims.py` (`MIX`); check the effect with `python -m sim.report`.

## Files

| File | What it does |
|---|---|
| `synthea.py` | download and read patients, encounters, conditions |
| `codes.py` | the 40-code set with prices |
| `claims.py` | encounter to claim mapping, holdout tags |
| `phi.py` | PHI planting and canary hashes |
| `load.py` | JSONL output and MongoDB loading (claims through B's `firewall.encryption`) |

MongoDB roles: `forge` (`FORGE_MONGODB_URI`, falls back to `MONGODB_URI`). Claims are refused
unless the encrypted client is available, unless you pass `--allow-plaintext`.
