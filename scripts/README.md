# scripts/ (shared folder, each script has one owner)

| Script | Owner | What it does | By |
| --- | --- | --- | --- |
| `setup_db.py` | B | Creates all collections, indexes, time-series `metrics`, both Vector Search indexes (Automated Embeddings), database users and roles. Safe to run twice | 11:30 |
| `check_b.py` | B | Offline smoke for models, rules, tokenize, guard, search stub, replay, llm (no Atlas required) | anytime |
| `seed_fake.py` | D | Writes about 50 fake claims, adjudications, appeals, events and metrics so everyone can build before the real loop runs | 11:00 |
| `smoke.py` | C | Pushes one claim through the whole loop and checks every collection got its document. Must pass before any merge to `main` after 1:00 PM | 12:30 |
| `reset_demo.py` | D | Restores a known demo state in under 1 minute: `--save NAME` / `--restore NAME` snapshots of the run state, or a fresh wipe to v1 profiles. **Never wipe after 2:30 PM unless the demo is broken** (it would erase the long-run history); restore a snapshot instead | 4:15 |
