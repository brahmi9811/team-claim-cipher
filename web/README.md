# web/ (owner: D)

The live view. It supports the agent; it is not the product. Full specs: [PLAN.md, Member D](../docs/PLAN.md#member-d-demo-and-product).

## Run it

From the repo root, with `.env` filled in (only `MONGODB_URI`, `MONGODB_DB` and `SIM_URL` are needed here):

```
pip install -e .                           # from pyproject.toml (includes fastapi, uvicorn)
python scripts/seed_fake.py --live         # fake data + a new claim every second (skip once the real loop runs)
python -m web.api                          # http://localhost:8002
```

Open http://localhost:8002. The dot in the top right says `live` when the SSE stream is connected (`live (polling)` if the cluster has no change streams).

## Files

| File | What it does |
| --- | --- |
| `api.py` | FastAPI on port 8002: SSE stream fed by change streams, detail endpoints, button endpoints; also serves `static/` |
| `db.py` | Connection helper: B's `common.db.get_db` (guarded, `agent_worker` role), or `get_raw_db` for admin scripts. Collection names |
| `static/index.html`, `style.css`, `app.js` | Four tabs (Overview with scoreboard and demo moments, Claims, Appeals, Agent learning), "Why?" drawer, buttons. Plain HTML + JS, no build step |
| `static/config.js` | Backend URL, so the same page works locally and on Vercel |

## Endpoints

| Endpoint | What it returns |
| --- | --- |
| `GET /stream` | SSE. Events: `adjudication`, `appeal`, `harness_event`, `profile` (each `{op, doc}` or `{op: "delete", id}`), `metrics` (every 5 s), `reset`, `hello` |
| `GET /adjudications`, `/appeals`, `/events` | Newest first; `?limit=` and `?insurer=` |
| `GET /adjudications/{id}` | "Why?" for a denial: the Verdict, resolved policy clauses, paid comparables with outcomes, pattern stats, the appeal, the claim (allow-listed fields only, never `patient` or `notes`) |
| `GET /events/{id}` | "Why?" for a harness change: reason, before/after, evidence ids resolved to adjudications, appeals or rules, earlier changes for that insurer |
| `GET /appeals/{id}` | Appeal with its tokenized letter |
| `GET /metrics/latest`, `/metrics/history` | Latest `metrics` per insurer; headline totals come from the scorer's `insurer: "all"` row (summed only if it's missing); history for sparklines |
| `GET /profiles`, `/health` | Harness profiles (Trust Ladder state); health shows change-stream vs polling mode |
| `POST /appeals/{id}/approve` | Sets `status: "approved"`, `approved_by: "human"`, `approved_at`. 409 if already approved or decided |
| `POST /appeals/approve-all` | Body `{"insurer": "payer_b" or null, "limit": 20}`: human approval of the oldest drafts (the "Approve drafts" button in the Appeals panel; follows the insurer filter). Draft-only appeals are only decided once approved, and the Trust Ladder earns auto-file from decided appeals |
| `POST /demo/policy-change` | Calls the simulator's `/admin/policy-change/payer_c`; passes through its 409 when Payer C is already on its latest policy |
| `POST /demo/reset` | Body `{"confirm": "RESET"}`; runs `scripts/reset_demo.py --yes` (or `--restore $DEMO_RESET_SNAPSHOT`) |

**Contract for C (appeal_worker):** in draft-only mode, file appeals where `status == "approved"` and `outcome` is null, then set `status: "filed"` and the `outcome`. Appeals the live view shows as needing approval: `outcome` null, `mode != "auto_file"`, `status` not `approved`/`filed`. `orchestrator/appeal_worker.py` implements this.

**Timestamps:** what the loop writes (`adjudicated_at`, `harness_events.ts`, appeal times, `metrics.ts`) is a BSON date, as the orchestrator and simulator write it. Claims' `created_at` and verdicts' `created_at` are ISO strings (forge, `common.models.now_iso`). The API returns both as ISO strings.

## Settings (environment)

| Variable | Default | Use |
| --- | --- | --- |
| `SIM_URL` | `http://localhost:8001` | Simulator, for the policy-change button |
| `SIM_ADMIN_TOKEN` | unset | Must match the simulator's `SIM_ADMIN_TOKEN` if it sets one; sent as `X-Admin-Token` by the policy-change button and `reset_demo.py` |
| `DEMO_BUTTON_TOKEN` | unset | **Set this before exposing the API through a tunnel.** Button endpoints then need header `X-Demo-Token`; the page asks for it once |
| `DEMO_RESET_SNAPSHOT` | unset | Reset button restores this snapshot instead of wiping (see `scripts/reset_demo.py --save`) |
| `WEB_PORT` | `8002` | |

## Vercel

Deploy `web/static/` as a static site (Vercel project root directory `web/static`, no build command). Run `api.py` on the laptop behind a tunnel (for example `cloudflared tunnel --url http://localhost:8002`), then either set `API_URL` in `static/config.js` or open the Vercel page once with `?api=https://<tunnel-host>` (remembered per browser; `?api=` with no value clears it).

## Demo shortcuts

The page has four tabs: **Overview**, **Claims**, **Appeals** (the badge counts drafts waiting for approval) and **Agent learning**. `?page=claims`, `?page=appeals` or `?page=learning` opens one directly.

The Overview's three **demo moment** cards always show the newest wrongful denial, overturned appeal and harness change, and open their "Why?" drawer in one click, so the 0:25, 0:55 and 1:20 moments of the demo script need no bookmarks. To pin a specific one instead, deep links open the drawer directly: `/#adj=adj_00123`, `/#event=evt_0931`, `/#appeal=apl_2217`.

## Demo runbook

For whoever drives the laptop. The script itself is in [PLAN.md, 3-minute live demo](../docs/PLAN.md#3-minute-live-demo).

**About 4:00 PM**
1. `python scripts/reset_demo.py --save before-judging` (read-only snapshot of the long run).
2. Set `DEMO_RESET_SNAPSHOT=before-judging` in `.env` and restart `python -m web.api`, so the Reset button restores the snapshot instead of wiping.
3. Pick the three demo moments in the live view and bookmark their deep links:
    - a Payer B denial with verdict *Wrongful · bulk* (claim stream, filter Payer B);
    - an *Overturned* appeal (appeals panel);
    - a *Profile changed* event by the evolver with a clear reason (harness changes panel).
4. Record the backup video of the full script; keep it open in another tab.

**About 4:15 PM: switch the LLM on** (the long run uses the free heuristic fallback to save the OpenRouter credit)
1. In `.env`: `LLM_ENABLED=true` and `CLAIM_RATE_PER_SEC=1` (about $7-8 an hour on Sonnet 5; 2 per second would use $10 in about 40 minutes).
2. Stop the orchestrator (Ctrl+C) and start it again: `python -m orchestrator.main`. It continues from the database.
3. Check it: new verdicts in the "Why?" drawer show `Model: sonnet-5` instead of `heuristic-fallback`.
4. If the credit runs out, the agents fall back to the heuristic on their own; nothing breaks.

**Before going on stage**
- `/health` shows `"streams"` as `change_stream` for all four collections, and the dot in the top right says `live`.
- The simulator is up (the policy-change button needs it). Payer C must still be on version 1, or the button returns 409 "already at its latest policy version".
- If the tunnel is flaky, demo from http://localhost:8002 and keep the Vercel link for the submission.

**If something breaks:** press Reset (type `RESET`). With `DEMO_RESET_SNAPSHOT` set, it restores the 4:00 PM state in a few seconds.
