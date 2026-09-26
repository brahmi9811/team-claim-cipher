# Claim Cipher

> **Insurers use algorithms to deny claims. We built a self-improving agent that learns each insurer's hidden behavior, catches wrongful denials, and fights back with evidence-based appeals, without ever showing patient data to the AI.**

Built at the MongoDB Harness Engineering & Model Wrangling Hackathon, Saturday Sept 26, 2026. Billing operations only: no diagnosis, no treatment, no medical advice.

- **Full product plan** (problem, architecture, loops, data model, demo, Q&A): [docs/PLAN.md](docs/PLAN.md)
- **This README:** who builds what, and how four people build it at the same time without blocking each other.

---

## What it does, in one minute

The agent runs in a loop all day, per insurer:

1. **Learn:** works out each insurer's hidden rules from its denials and fixes claims before sending them.
2. **Judge:** decides whether each denial is legitimate or **wrongful** (contradicts the insurer's own policy, or looks like an automated bulk denial).
3. **Fight:** drafts and files evidence-based appeals, then learns which evidence wins with each insurer.
4. **Evolve:** rewrites its own per-insurer harness (rules, context policy, thresholds, appeal strategy, guardrails, tool permissions) with automatic rollback.

Underneath: a **PHI firewall**. Patient fields are encrypted with MongoDB Queryable Encryption, and the LLM only sees tokens.

The engine is generic: any insurance line with a policy, a claim and an appeal can plug in. Health insurance is the first use case.

---

## Team and ownership

Put your name next to your role. **Each role owns its folders.** You only edit your own folders, which is what lets us work side by side without merge conflicts.

| Role | Owner | Owns these folders | Branch prefix |
| --- | --- | --- | --- |
| **A: Simulation and scoring** | ___ | `forge/`, `sim/`, `scorer/` | `a/` |
| **B: MongoDB and security** | ___ | `common/`, `firewall/`, `validator/`, `scripts/setup_db.py` | `b/` |
| **C: Agents** | ___ | `agents/`, `orchestrator/`, `scripts/smoke.py` | `c/` |
| **D: Demo and product** | ___ | `web/`, `scripts/seed_fake.py`, `scripts/reset_demo.py`, `README.md` | `d/` |

With three people: A and D merge. The simulator comes first, the live view after 2:30 PM.

**Changes from the Docs plan, to balance the load:** the scorer moved from B to A (A already owns the ground truth), and `reset_demo.py` plus the fake-data seeder moved from B to D (D needs fake data first, for the live view).

---

## How we work in parallel

The roles do depend on each other. We handle that with **one short shared setup, fixed contracts, and stubs**: simple stand-in versions of each function that return fixed data. After the first 30 minutes, nobody waits on anybody.

### Step 1: shared setup, 10:30 - 11:00 (the only time we block each other)

| Time | Who | What |
| --- | --- | --- |
| 10:30 - 10:45 | **All four**, at one screen | Agree on the models in `common/models.py`: `Claim`, `TokenizedClaim`, `Adjudication`, `Verdict`, `Rule`, `ReplayStats`, `Appeal`, `HarnessProfile`, `Event`. Start from the draft fields in [PLAN.md, Member B specs](docs/PLAN.md#member-b-mongodb-and-security). |
| 10:30 - 11:00 | B | Repo skeleton, `pyproject.toml`, `.env.example`, `common/db.py`, `common/models.py`; Atlas users; everyone connects |
| 10:45 - 11:00 | D | `scripts/seed_fake.py`: about 50 fake claims, adjudications, appeals and events, so everyone has data to code against |
| 10:45 - 11:00 | A | Start loading Synthea data |
| 10:45 - 11:00 | C | Prompts and orchestrator skeleton, coded against the contracts below |

**11:00: `common/models.py` is frozen.** After that, only B edits it, and only after telling the whole team.

### Step 2: contracts and stubs

Every cross-role call goes through one of these functions or endpoints. Each owner **commits a stub by the "stub by" time**. It must have the right signature and return fixed, valid data. The real version replaces it later, with no change for the people calling it.

| Function or endpoint | Owner | Used by | Stub by | Real by |
| --- | --- | --- | --- | --- |
| `POST /submit` (sim) | A | C (orchestrator) | 11:15 (random paid or denied) | 12:00 |
| `POST /appeal` (sim) | A | C (appeal writer) | 11:15 (random outcome) | 1:00 |
| `common.rules`: `matches`, `to_match`, `apply_fix` | B | C (scrubber), B (validator) | 11:30 (real; small) | 11:30 |
| `common.search.vector_search(collection, query, filters)` | B | C (judge) | 11:30 (fixed results) | 12:30 |
| `firewall.tokenize(claim) -> TokenizedClaim` | B | C (orchestrator) | 11:30 (fixed token) | 12:30 |
| `firewall.guard(text) -> text` (raises `PHILeak`) | B | `common/llm.py` | 11:30 (returns text unchanged) | 12:30 |
| `firewall.render_letter(tokenized) -> letter` | B | C (appeal writer) | 11:30 (returns input unchanged) | 1:00 |
| `validator.replay(rule) -> ReplayStats` | B | C (judge, evolver) | 11:30 (always "promote") | 2:30 |
| `common.profiles.watch(callback)` (change stream) | B | C (scrubber, judge), D | 11:30 (polls every 5 s) | 2:30 |
| `judge.classify(adjudication) -> Verdict` | C | C (orchestrator) | 11:30 (random verdict) | 12:30 |
| `appeal_writer.draft(verdict) -> Appeal` | C | C (orchestrator) | 11:30 (template letter) | 1:00 |
| `evolver.run_cycle(insurer) -> Event or None` | C | C (orchestrator) | 11:30 (returns None) | 3:30 |
| `scorer` writes `metrics` every 30 s | A | D (scoreboard) | 12:00 (fake numbers) | 2:30 |
| Collections and their document shapes | B | Everyone | 11:00 (`models.py` frozen) | n/a |

**If you're blocked, don't wait:** write a local fake of the missing piece, keep building, and tell the owner.

### Step 3: who depends on whom

| Role | Can start alone? | Needs | Until then, use |
| --- | --- | --- | --- |
| A | **Yes** | Only the `Claim` model | Nothing else; test the simulator with fake claims |
| B | **Yes** | Nothing; others depend on B | Seeded fake data from D |
| C | Yes, with stubs | A's endpoints; B's firewall, search, replay, watch | The stubs above |
| D | Yes, with stubs | Events and metrics in the database | `seed_fake.py`, plus a loop that writes fake events |

C is the most dependent role, so **A and B are on the critical path**. Their stubs and real versions must land on time.

### Step 4: integration points (merge to `main` and switch stubs to real)

| Time | What gets connected | Who pairs up |
| --- | --- | --- |
| 11:00 | `models.py` frozen, fake data seeded, everyone connected | All |
| 11:30 | All stubs on `main` | All |
| 12:00 | C switches to A's real `/submit` | A + C |
| 12:30 | C switches to B's real `tokenize`, `guard`, `search`; real Judge on | B + C |
| **1:00** | **Whole loop runs for real**; D switches panels to real events; baseline run starts | All |
| 2:30 | Real change streams: Evolver changes reach scrubber and live view; real replay; real scorer | B + C + D, A |
| 3:30 | Evolver, rollback and Trust Ladder live on screen | C + D |
| 4:15 | Feature freeze | All |

---

## Roles

**Each member's full responsibilities are in [docs/PLAN.md, Team responsibilities](docs/PLAN.md#team-responsibilities):** first 30 minutes, deliverables by time, and the specs you own (data formats, rule format, verdict JSON, Evolver limits, panel data sources). The checklists below are the short version.

### Role A: Simulation and scoring

**Owns:** `forge/`, `sim/`, `scorer/`. **You provide:** the insurer simulator and the honest scoreboard. **You use:** `common/models.py`.

- [x] Load Synthea sample data (patients and encounters)
- [x] Map encounters to our 40-code set (ICD-10-CM + HCPCS Level II); build about 2,000 claims
- [x] **11:15:** stub `/submit` and `/appeal` running (random but valid responses)
- [x] **12:00:** real `/submit` with hidden rules for 3 insurers and planted wrongful behavior (see [simulator spec](docs/PLAN.md#insurer-simulator-spec))
- [x] **1:00:** real `/appeal` with per-insurer appeal logic; ground truth written to `sim_truth`
- [x] Write the three published policies (12 clauses each) and load them into `policies`
- [x] Plant realistic PHI in the notes of about 5% of claims; tag 20% `holdout: true`
- [x] **2:30:** scorer writes real metrics every 30 seconds (the only code that reads `sim_truth`)
- [x] Policy-change button endpoint for Payer C
- [x] Tune the mix so cold-start acceptance is about 65% and the curve is visible

**Done by 1:00 PM:** claims loaded; `/submit` and `/appeal` real; `sim_truth` filling.

### Role B: MongoDB and security

**Owns:** `common/`, `firewall/`, `validator/`, `scripts/setup_db.py`. **You provide:** the shared foundation everyone imports. **You use:** nothing; you go first.

- [x] **11:00:** repo skeleton, `common/db.py`, `common/models.py` frozen; Atlas users, IP list, `.env.example`
- [x] `scripts/setup_db.py`: all collections, indexes, time-series `metrics`, both Vector Search indexes with Automated Embeddings (fallback: Embedding and Reranking API)
- [x] **11:30:** stubs for `search`, `tokenize`, `guard`, `render_letter`, `replay`, `profiles.watch`
- [x] `common/llm.py`: OpenRouter client, always calls `firewall.guard()` on input and output, LangSmith tracing
- [x] Queryable Encryption on `claims` and `phi_tokens` (**timebox ends 12:00**, then fall back to Client-Side Field Level Encryption)
- [x] **12:30:** real `tokenize`, `guard` (leak detector), `search`
- [x] **2:30:** real `validator.replay` (aggregation pipelines) and `profiles.watch` (change streams)
- [x] Evolving leak detector: blocked leaks logged to `phi_incidents`, guardrails read from the profile
- [x] Database roles so agents can't read `sim_truth` (if the sandbox allows custom roles)

**Done by 1:00 PM:** everyone connected; firewall and search real; encryption working or fallback chosen.

**Switch imports (C):** replace `orchestrator.contracts.*` with `common.*` / `firewall` / `validator` once ready. Offline check: `python scripts/check_b.py`.

### Role C: Agents

**Owns:** `agents/`, `orchestrator/`, `scripts/smoke.py`. **You provide:** the agent loop. **You use:** A's simulator, and B's firewall, search, replay and watch (stubs until they're real).

- [x] **11:30:** orchestrator skeleton running end to end on stubs; stubs for `classify`, `draft`, `run_cycle`
- [x] `scripts/smoke.py`: pushes one claim through the whole loop. It must pass before any merge to `main` after 1:00 PM
- [x] Scrubber (Haiku): applies active rules for the insurer
- [x] **12:30:** Judge (Sonnet): prompt, structured JSON verdict, evidence retrieval, confidence
- [x] **1:00:** Appeal Writer (Sonnet): letter from evidence, following the insurer's appeal strategy
- [x] Rule proposals from denial clusters, handed to `validator.replay`; rule lifecycle
- [x] **3:30:** Evolver (Sonnet): one change per insurer per cycle, written reason, automatic rollback
- [x] Trust Ladder: draft-only to auto-file, per insurer
- [x] Every agent decision logged to `harness_events`; claim-rate control, retries

**Done by 1:00 PM:** real loop: claim → submit → denial → verdict → appeal → outcome, all in MongoDB.

### Role D: Demo and product

**Owns:** `web/`, `scripts/seed_fake.py`, `scripts/reset_demo.py`, `README.md`. **You provide:** the live view, the demo and the submission. **You use:** collections and change streams (fake data until 1:00 PM).

- [x] **11:00:** `seed_fake.py` with about 50 fake claims, adjudications, appeals and events
- [x] Web skeleton + SSE endpoint that forwards change-stream events to the browser
- [x] Panels: claim stream (12:00), scoreboard (1:00), harness changes (2:30), appeals + approve button (3:30)
- [x] "Why?" drawer: click any rule or profile change to see its evidence and reason
- [x] Policy-change and reset buttons; `reset_demo.py` restores a known state in under 1 minute
- [ ] Deploy the live view to Vercel (hosting decided by 3:30 PM)
- [ ] Record a **backup video** of the full demo by 4:15
- [ ] README "What we built at the event" section; 1-minute video recorded on-site; submission by 4:45
- [ ] Keep time: call every checkpoint below

**Done by 1:00 PM:** live view shows real claims flowing.

---

## Hour-by-hour plan (Saturday, Sept 26)

**The one milestone that matters most: the end-to-end loop works by 1:00 PM.**

| Time | A: Simulation and scoring | B: MongoDB and security | C: Agents | D: Demo | Checkpoint |
| --- | --- | --- | --- | --- | --- |
| 9:00 - 10:30 | Laptops, Atlas, API keys, re-read plan. **No code** | Same | Same | Same | Everyone can log in to Atlas and call the LLM API |
| 10:30 - 11:00 | Models at the whiteboard; load Synthea | Models; repo skeleton, `common/`, Atlas users | Models; prompts, orchestrator skeleton | Models; `seed_fake.py` | `models.py` frozen; everyone connected; OpenRouter credits (else backup key) |
| 11:00 - 12:00 | Stub endpoints (11:15), real `/submit` | `setup_db.py`, stubs (11:30), Queryable Encryption | Loop on stubs; Scrubber, Judge v0 | Web skeleton, SSE, claim stream panel | Simulator denies claims; encryption working or fallback chosen |
| 12:00 - 1:00 | `/appeal`, `sim_truth`, policies loaded | Real firewall and search | Appeal Writer; switch to real sim and firewall | Scoreboard panel | **End-to-end loop real. Baseline run starts** |
| 1:00 - 1:30 | Lunch in shifts; baseline keeps running | | | | Baseline numbers saved |
| 1:30 - 2:30 | Scorer, tune the mix, policy-change | Real replay, change streams | Rule proposals and lifecycle | Harness-changes panel | **Long run started. Database never wiped after 2:30** |
| 2:30 - 3:30 | PHI planting, holdout tags | Evolving leak detector, roles | Evolver, rollback, Trust Ladder | Appeals panel, "Why?" drawer | A profile change goes live on screen with no restart |
| 3:30 - 4:15 | Fix bugs | Fix bugs, index performance | Fix bugs, tune prompts | `reset_demo.py`, Vercel, backup video | **Feature freeze at 4:15** |
| 4:15 - 5:00 | Rehearse | Rehearse | Rehearse | 1-minute video, submit by **4:45** | Submitted, repo public, all members added |
| 5:15 | Judging: D presents, C drives the laptop, A and B answer technical questions | | | | |

Workers can restart at any time; the database keeps the history, so the long run survives code changes.

**Cut list (in this order, if we fall behind):** 1. Trust Ladder (keep a manual approve button). 2. "Why?" drawer (show `harness_events` as a list). 3. Policy-change button (mention it instead). 4. Queryable Encryption (fall back to Client-Side Field Level Encryption).
**Never cut:** validator replay, Evolver, Judge, leak detector.

---

## Git workflow

- **Branches:** short-lived branches off `main`, named with your role prefix: `a/submit-endpoint`, `b/firewall-guard`, `c/judge-v1`, `d/scoreboard`.
- **Merge often:** merge to `main` at least at every integration point above. Small PRs, and you can merge your own after a quick run.
- **Before pushing:** `git pull --rebase origin main`, run your part, and after 1:00 PM also run `scripts/smoke.py`.
- **`main` must always run.** If you break it, fixing it is your top priority.
- **Stay in your folders.** Shared files have these rules:
    - `common/`: B only. Ask B for changes.
    - `pyproject.toml`: anyone may add a dependency; pull first and add one line at a time.
    - `README.md`: D owns it; everyone may edit their own role's checklist.
- **Commit messages:** start with your role, for example `[A] sim: hidden rules for payer B`. Commit small and often; the history proves the work was done at the event.
- **Never commit:** `.env`, the encryption key file, or API keys (see `.gitignore`).
- **Data is committed on purpose (team decision):** `data/synthea/` (the Synthea sample CSVs) and `data/forge/` (the generated claims and PHI canaries) are in the repo, so every teammate builds from the same data without a download. All of it is synthetic; there is no real patient data. Don't commit anything else under `data/` (demo snapshots in `data/snapshots/` stay ignored).

---

## Repo layout

**Every folder has its own `README.md`** listing the files to create, the functions or endpoints with their stub behavior, and deadlines. On Saturday, open your folder and start there.

```
claim-cipher/
  README.md              # D: this file
  docs/PLAN.md           # full product plan
  .env.example           # MONGODB_URI, OPENROUTER_API_KEY, LANGSMITH_API_KEY, KEY_FILE path
  pyproject.toml         # B creates; anyone adds dependencies
  common/                # B: db.py, models.py, rules.py, search.py, llm.py, profiles.py
  forge/                 # A: Synthea loader, code mapping, PHI planting, policy loader
  sim/                   # A: FastAPI insurer simulator
    policies/            #    payer_a.md, payer_b.md, payer_c.md
    rules/               #    hidden rules per insurer (never imported by agents)
  scorer/                # A: metrics writer (only code allowed to read sim_truth)
  firewall/              # B: encryption setup, tokenize, detokenize, leak detector
  validator/             # B: replay aggregation pipelines
  agents/                # C: scrubber, judge, appeal_writer, evolver, prompts/
  orchestrator/          # C: async runner, workers, claim-rate control
  web/                   # D: api.py (SSE + buttons, port 8002), static/ (live view, deployed to Vercel)
  scripts/               # setup_db.py (B), smoke.py (C), seed_fake.py and reset_demo.py (D)
  data/                  # A: synthea/ sample CSVs and forge/ generated claims + canaries (committed, all synthetic)
```

---

## Setup and running

*Filled in on Saturday as the pieces land. Planned:*

1. Python 3.12. Install with `pip install -e .`
2. Copy `.env.example` to `.env` and fill in your keys. Never commit `.env`.
3. `python scripts/setup_db.py` creates collections and indexes (run once, by B).
4. Start the simulator, the orchestrator and the live view (exact commands added by each owner).

**Claims and policies (A):** `python -m forge all` downloads the Synthea sample, builds 2,000 claims and loads claims (encrypted), policies and hashed PHI canaries. `python -m sim.report` shows the denial mix. Details: [forge/README.md](forge/README.md).

**Simulator (A):** `python -m sim` serves http://localhost:8001 (`--memory` without MongoDB, `--stub` for random responses). Payer C policy change: `POST /admin/policy-change/payer_c`; reset: `POST /admin/policy-reset/payer_c`. Contract: [sim/README.md](sim/README.md).

**Scoreboard (A):** `python -m scorer` writes `metrics` every 30 seconds (`--once --dry-run` to just print). Details: [scorer/README.md](scorer/README.md).

**Agent loop (C):** `python -m orchestrator.main` runs forever at `CLAIM_RATE_PER_SEC` (set `MAX_CLAIMS=300` for a fixed run). Run only **one** orchestrator against the shared cluster: two share the claim cursor and process some claims twice. `python scripts/smoke.py` runs in its own `denialfighter_smoke` database, so it never touches demo data.

**Live view (D):** `python -m web.api`, then open http://localhost:8002. Details, endpoints and Vercel setup: [web/README.md](web/README.md).

**Fake data (D):** `python scripts/seed_fake.py` writes about 50 claims with adjudications, verdicts, appeals, events, metrics, profiles and placeholder policies (all tagged `fake: true`). Add `--live` to keep writing new activity every second; `--clear` removes only the fake documents.

**Demo reset (D):** `python scripts/reset_demo.py --save <name>` snapshots the run state (safe, read-only); `--restore <name>` puts it back; with no flag it wipes the run state to v1 profiles. Takes about a second. **Never wipe after 2:30 PM unless the demo is broken**: take a snapshot at about 4:00 PM and restore that instead.

---

## What we built at the event

*For the judges. Filled in on Saturday by D: every component above was written on Sept 26, 2026, starting at 10:30 AM. The commit history shows it. Any official boilerplate we reused is listed here.*

**Public data and tools used:** [Synthea](https://github.com/synthetichealth/synthea) synthetic patients (the sample CSVs are committed in `data/synthea/`), ICD-10-CM and HCPCS Level II code sets, CARC/RARC denial code definitions, MongoDB Atlas.
