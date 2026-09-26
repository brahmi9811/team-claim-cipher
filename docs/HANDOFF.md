# Handoff context — Claim Denial Fighter (Member C's side)

Written for whoever (or whichever fresh Claude session) picks this codebase back up
in a new repo/folder for the live hackathon. It has no memory of how this was built —
this document is the substitute. Read `docs/PLAN.md` and `README.md` for the full
product spec and team workflow; this file is the "what's actually true right now,
and why" layer on top of them.

---

## The product, in one paragraph

Insurers auto-deny claims in bulk; almost nobody appeals. This system runs a loop —
**Learn** (fix claims before submission, from a growing set of insurer-specific
prevention rules), **Judge** (decide if a denial is legitimate or wrongful, citing
the insurer's own policy or a bulk-denial pattern as evidence), **Fight** (draft and
file evidence-backed appeals), **Evolve** (a meta-agent that rewrites each insurer's
own harness — thresholds, appeal strategy, guardrails, permissions — with automatic
rollback if a change makes things worse). Underneath: a PHI firewall so the LLM never
sees a real patient identifier, only a token.

## Team roles and what each owns

| Role | Owns | Mission |
| --- | --- | --- |
| A | `forge/`, `sim/`, `scorer/` | Loads Synthea data into claims, runs the deterministic insurer simulator (3 fictional insurers, hidden rules), scores honestly against ground truth |
| B | `common/`, `firewall/`, `validator/`, `scripts/setup_db.py` | The shared foundation: DB access, models, PHI tokenize/guard/render, rule replay/lifecycle |
| **C (you)** | `agents/`, `orchestrator/`, `scripts/smoke.py` | The agent loop itself: Scrubber, Judge, Appeal Writer, Evolver, Trust Ladder, and the async orchestrator wiring them together |
| D | `web/`, `scripts/seed_fake.py`, `scripts/reset_demo.py` | The live view (SSE-fed dashboard) and the demo/submission |

In practice on day one, ownership blurred under time pressure — a "D - bug fixes"
commit touched files across `common/`, `firewall/`, and `orchestrator/` too. That's
normal for a hackathon; just know the folder-ownership table is a starting
convention, not a hard wall, and cross-file fixes got made (and reviewed) when
something was actually broken.

## Architecture, and the one thing to internalize about Member C's code

Every agent and orchestrator function talks to MongoDB through **real, shared
modules** — `common.db`, `common.profiles`, `common.rules`, `common.search`,
`common.llm`, `firewall.tokenize`/`guard`/`render_letter`, `validator.replay`/
`lifecycle` — never a private in-memory stand-in. That wasn't always true: C's
code originally shipped with local "contract stub" implementations
(`orchestrator/contracts/*.py`) so the agent loop could be built and tested before
B's and A's real modules existed. Once those landed, C's code was rewired to use
them directly and the stubs were deleted — only `orchestrator/contracts/sim_client.py`
(the HTTP client for A's simulator) still exists, because it's genuinely C's own
integration code, not a stand-in for someone else's module.

**The one surviving piece of that "don't get blocked" pattern**: every LLM call in
`agents/*.py` catches `common.llm.LLMUnavailable` and falls back to a deterministic,
rule-based heuristic that produces the same JSON shape. This means the whole loop
(Learn → Judge → Fight → Evolve) is genuinely testable with zero API key, and
upgrades to real Sonnet/Haiku reasoning the instant `OPENROUTER_API_KEY` is set, with
no code change anywhere. Keep this pattern if you extend the agents — it's what
makes `scripts/smoke.py` fast and free to run constantly.

## What Member C's code actually does (file by file)

- `agents/scrubber.py` — applies every `active` prevention rule to a tokenized claim
  before submission. Deterministic (`common.rules.apply_fix`), not an LLM decision —
  Haiku is used only to write a one-line audit sentence, never to decide the fix.
- `agents/judge.py` — classifies a denial as `legitimate` / `wrongful_policy` /
  `wrongful_bulk` / `needs_review` using policy-clause retrieval
  (`common.search.vector_search`), comparable paid claims, and bulk-timing stats.
  When it calls something `legitimate`, it also clusters similar past denials and
  proposes a candidate prevention rule once 3+ share the same reason code — this is
  half of Loop 1 (Learn).
- `agents/appeal_writer.py` — drafts an appeal letter from the verdict's evidence,
  ordered by the insurer's current `appeal_strategy`, then files it via
  `orchestrator/contracts/sim_client.py`.
- `agents/evolver.py` — the meta-agent. One bounded profile-field change per insurer
  per cycle, gated: a `judge.min_confidence` change specifically must be validated
  through `validator.threshold_replay` first (a replay over past verdicts + known
  appeal outcomes) before it's allowed to apply. Automatic rollback on the *next*
  cycle if the field's target metric got worse.
- `agents/trust_ladder.py` — the only code allowed to touch `harness_profiles.permissions`
  (draft-only ↔ auto-file), based on a fixed streak rule, deliberately separate from
  the Evolver's LLM-driven proposals.
- `orchestrator/pipeline.py::process_claim()` — one claim's full journey: tokenize →
  scrub → submit → (if denied) judge → (legitimate → rule lifecycle via
  `validator.lifecycle.advance`) or (wrongful → draft, auto-file if permitted).
- `orchestrator/claims_source.py` — where claims to process come from: real,
  not-yet-drained ones in `claims` (A's forge-loaded data) first, cursored by `_id`
  (see the gotcha below), backfilled with freshly generated demo claims
  (`orchestrator/seed.py`, tagged `source: "demo"`) whenever the real queue is empty —
  so the loop never just stops.
- `orchestrator/appeal_worker.py` — files any appeal a human approved via the live
  view's `/approve` endpoint (`status: "approved"` → filed). This was a real gap
  found in review: nothing picked those up until this file existed.
- `orchestrator/main.py` — the async runner: claims processed concurrently (up to
  `MAX_IN_FLIGHT`), a background Evolver cycle every `EVOLVER_CYCLE_SECONDS` (or
  sooner, after `EVOLVER_CYCLE_OUTCOMES` new outcomes), a background poll for
  approved appeals. Runs forever by default; `MAX_CLAIMS` bounds it for a quick check.
- `scripts/smoke.py` — an actual integration test (needs `MONGODB_URI`), not a unit
  test: pushes claims through all four loops and checks the observable result.
  Snapshots and restores everything it touches, so it's safe to run against a real,
  shared cluster. Pins `SIM_URL` to an unreachable address on purpose, so it always
  exercises C's own deterministic fallback rather than A's real (probabilistic)
  simulator — this is a test of C's code, not of A's.

## Bugs found and fixed on day one — worth knowing so you don't reintroduce them

| Bug | Root cause | Fix |
| --- | --- | --- |
| Approved appeals never filed | Nothing watched for `status: "approved"` | `orchestrator/appeal_worker.py`, polled from `main.py` |
| LLM calls could crash a claim | Agents only caught `LLMUnavailable`, not a real `PHILeak` | All four agents now catch `PHILeak` too, degrading to `needs_review` (Judge) or a PHI-safe heuristic template (others) |
| Evolver ratcheted `judge.min_confidence` to its floor over and over | It was gated on `acceptance_rate`, which nothing the Evolver can change actually affects (that's fixed upstream by A's hidden rules + the Scrubber) | Regated on `appeal_win_rate` (which it can move), and now must pass `validator.threshold_replay` before applying |
| Adjudication ids never matched A's simulator | Code read a leftover stub-only key instead of the simulator's real `adjudication_id` | Read the real key directly |
| Mixed timestamp types (string vs. real date) across `adjudications`/`harness_events`/`appeals` | Different builders used `.isoformat()` strings vs. real `datetime` | Normalized to real `datetime` everywhere *except* `claims.created_at`, which forge/tokenize.py specifically expects as a string — see next row |
| Real claims stopped draining after the first batch | `claims_source.py` originally cursored on `created_at`, but forge stamps an entire 2,000-claim batch with one shared timestamp, so `$gt` never advanced | Recursored on `_id` (forge's ids are zero-padded and sequential) |
| The Atlas Vector Search index sat completely unused | `common/search.py`'s `_embed()` only called the paid Voyage API and returned `None` without a key, so the classic `queryVector` path always failed silently and fell back to keyword search | `_embed()` now uses the same free, local `all-MiniLM-L6-v2` model the documents were embedded with (query and document vectors **must** come from the same model, or the similarity comparison is meaningless) |

## Vector Search: the concrete setup that's known to work

Two indexes are needed, by these **exact names** (`common/search.py` looks them up
by name): `policies_vector` on `policies.clause_text`, `adjudications_vector` on
`adjudications.denial_text`. We're using **"bring your own embeddings"**, not
Atlas's Automated Embeddings, specifically to avoid the per-call Voyage API cost:

1. Get real documents into the collection first (Atlas's UI won't let you pick an
   empty/nonexistent collection when defining the index) — for policies, that's
   `python -m forge policies` (12 clauses × 3 insurers, no encryption needed).
2. Run `scripts/embed_policies.py` (`pip install -e ".[embeddings]"` first) — embeds
   every document's `clause_text` with `all-MiniLM-L6-v2` (free, local, 384
   dimensions) and writes it back as an `embedding` field.
3. In Atlas → Search & Vector Search → Create Search Index → Vector Search → JSON
   Editor:
   ```json
   {
     "fields": [
       { "type": "vector", "path": "embedding", "numDimensions": 384, "similarity": "cosine" },
       { "type": "filter", "path": "insurer" },
       { "type": "filter", "path": "current" }
     ]
   }
   ```
   Name it `policies_vector` exactly. Same pattern later for `adjudications_vector`
   (filter fields `insurer`/`status` instead), once real denials exist to embed.
4. Verify it's actually being hit (not silently keyword-falling-back) by checking for
   a `_score` in the results:
   ```python
   from common.search import vector_search
   hits = vector_search("policies", "chemotherapy administration coverage",
                         {"insurer": "payer_a", "current": True}, top_k=3)
   for h in hits: print(h["_id"], h.get("_score"))
   ```
   Verified on the previous cluster: top hit was genuinely the chemotherapy clause,
   score ~0.74 — real semantic ranking, not coincidence.

## Known-open items, not yet resolved as of this handoff

- Real Queryable Encryption on `claims`/`phi_tokens`: `firewall/encryption.py` got a
  substantial rewrite and appeared to auto-provision successfully in one test run
  against a fresh local MongoDB (no manual `QE_KEY_FILE` needed) — but this was not
  exhaustively verified against a real Atlas cluster. `--allow-plaintext` on
  `python -m forge load` remains the fallback for local/dev use (synthetic data only,
  never real patients).
- A minor, non-blocking concurrency note: `agents/judge.py`'s duplicate-rule check
  has a small race window under `main.py`'s concurrent claim processing — two claims
  clearing the same clustering threshold at nearly the same instant could both
  propose a near-identical candidate rule. Worst case is redundant (not incorrect)
  work; only worth fixing if you want it airtight before judges specifically probe it.
- `adjudications_vector` (the second Vector Search index) hasn't been built yet —
  same steps as `policies_vector`, once real denials exist.

## Everything else you need

- `docs/PLAN.md` — the full product plan: architecture diagram, the four loops in
  detail, the PHI firewall spec, the insurer simulator spec, the MongoDB data model,
  metrics/scoreboard definitions, the demo script, likely judge Q&A.
- `README.md` — team workflow: branch naming, the contracts/stubs table, the
  hour-by-hour plan, git conventions.
- Commit history on `main` up to `342bdf4` reflects everything above.
