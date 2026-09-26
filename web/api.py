"""Live view backend (owner: D). FastAPI on port 8002.

    uvicorn web.api:app --port 8002          # from the repo root
    python -m web.api                        # same thing

- GET  /stream                  Server-Sent Events: change streams on adjudications,
                                harness_events, appeals, harness_profiles, plus metrics every 5 s
- GET  /adjudications, /adjudications/{id}, /events, /events/{id}, /appeals, /appeals/{id},
       /metrics/latest, /metrics/history, /profiles     (initial load and the "Why?" drawer)
- POST /appeals/{id}/approve, /demo/policy-change, /demo/reset   (buttons)
- GET  /                        the static live view (web/static), for localhost demos

Read-only on everything except the approve button. Never returns patient fields:
claims are read with an explicit allow-list projection.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from web import db as dbm

log = logging.getLogger("web.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

SIM_URL = os.environ.get("SIM_URL", "http://localhost:8001")
SIM_ADMIN_TOKEN = os.environ.get("SIM_ADMIN_TOKEN")  # the simulator's /admin/* endpoints check this when set
BUTTON_TOKEN = os.environ.get("DEMO_BUTTON_TOKEN")  # set this when the API is exposed through a tunnel
RESET_SNAPSHOT = os.environ.get("DEMO_RESET_SNAPSHOT")  # if set, the reset button restores this snapshot
METRICS_EVERY_SEC = 5
POLL_EVERY_SEC = 1.0
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Collection -> SSE event name
WATCHED = {
    dbm.ADJUDICATIONS: "adjudication",
    dbm.HARNESS_EVENTS: "harness_event",
    dbm.APPEALS: "appeal",
    dbm.HARNESS_PROFILES: "profile",
}
# Fields a claim may expose to the browser. Everything else (patient.*, notes, records) stays in the database.
CLAIM_SAFE_FIELDS = ["insurer", "encounter_type", "diagnosis_codes", "lines", "total_charge_usd", "prior_auth_id", "referring_provider_id", "holdout"]
# Appeal ids are random (new_id), so appeals sort by created_at to list the newest first.
SORT_FIELD = {dbm.ADJUDICATIONS: "adjudicated_at", dbm.HARNESS_EVENTS: "ts", dbm.APPEALS: "created_at", dbm.HARNESS_PROFILES: "_id"}


# --- JSON ------------------------------------------------------------------


def clean(value):
    """BSON -> JSON-safe. Encrypted fields (Binary) never leave as bytes."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)) or type(value).__name__ == "Binary":
        return "[encrypted]"
    if type(value).__name__ in ("ObjectId", "Decimal128", "Timestamp"):
        return str(value)
    return value


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(clean(data), separators=(',', ':'))}\n\n"


# --- fan-out hub -----------------------------------------------------------


class Hub:
    """Background threads publish; each connected browser has its own queue."""

    def __init__(self):
        self.loop = None
        self.subscribers = set()
        self.mode = {}  # collection -> "change_stream" | "polling"

    def publish(self, event, data):
        if self.loop is None:
            return
        msg = sse(event, data)
        self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg):
        for q in list(self.subscribers):
            if q.qsize() < 1000:  # a stalled browser must not grow memory forever
                q.put_nowait(msg)


hub = Hub()
stop = threading.Event()


def watch_collection(db, coll_name, event_name):
    """Change stream on one collection; falls back to polling when change streams aren't available."""
    coll = db[coll_name]
    resume_token = None
    while not stop.is_set():
        try:
            with coll.watch(full_document="updateLookup", resume_after=resume_token, max_await_time_ms=1000) as stream:
                hub.mode[coll_name] = "change_stream"
                log.info("change stream open on %s", coll_name)
                while not stop.is_set() and stream.alive:
                    change = stream.try_next()
                    if change is None:
                        continue
                    resume_token = stream.resume_token
                    op = change["operationType"]
                    if op in ("insert", "update", "replace") and change.get("fullDocument"):
                        hub.publish(event_name, {"op": op, "doc": change["fullDocument"]})
                    elif op == "delete":
                        hub.publish(event_name, {"op": "delete", "id": change["documentKey"]["_id"]})
                    elif op in ("drop", "invalidate", "dropDatabase"):
                        resume_token = None
                        hub.publish("reset", {"collection": coll_name})
                        break
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            if code == 40573 or "replica set" in str(exc).lower() or "only supported on replica" in str(exc).lower():
                log.warning("change streams unavailable on %s (%s); polling instead", coll_name, exc)
                poll_collection(db, coll_name, event_name)
                return
            if code == 286 or "resume" in str(exc).lower():  # history lost: start fresh
                resume_token = None
            log.warning("change stream on %s failed: %s; retrying in 2 s", coll_name, exc)
            stop.wait(2)


def poll_collection(db, coll_name, event_name, window=100):
    """Fallback: re-read the newest `window` documents each second and publish any that changed."""
    hub.mode[coll_name] = "polling"
    coll = db[coll_name]
    seen = {}
    first = True
    while not stop.is_set():
        try:
            docs = list(coll.find({}).sort(SORT_FIELD[coll_name], -1).limit(window))
            current = {}
            for d in docs:
                h = hashlib.md5(json.dumps(clean(d), sort_keys=True).encode()).hexdigest()
                current[d["_id"]] = h
                if not first and seen.get(d["_id"]) != h:
                    hub.publish(event_name, {"op": "insert" if d["_id"] not in seen else "update", "doc": d})
            if not first and seen and not current:
                hub.publish("reset", {"collection": coll_name})
            seen, first = current, False
        except Exception as exc:  # noqa: BLE001
            log.warning("polling %s failed: %s", coll_name, exc)
        stop.wait(POLL_EVERY_SEC)


def metrics_loop(db):
    while not stop.is_set():
        try:
            hub.publish("metrics", latest_metrics(db))
        except Exception as exc:  # noqa: BLE001
            log.warning("metrics read failed: %s", exc)
        stop.wait(METRICS_EVERY_SEC)


# --- queries ---------------------------------------------------------------

METRIC_FIELDS = ["acceptance_rate", "judge_precision", "judge_recall", "appeal_win_rate", "recovered_usd", "phi_leaks_to_llm", "leaks_blocked", "cost_usd_per_claim"]


def latest_metrics(db):
    rows = list(db[dbm.METRICS].aggregate([
        {"$sort": {"ts": -1}},
        {"$group": {"_id": "$insurer", "doc": {"$first": "$$ROOT"}}},
    ]))
    firsts = {r["_id"]: r["doc"] for r in db[dbm.METRICS].aggregate([
        {"$sort": {"ts": 1}},
        {"$group": {"_id": "$insurer", "doc": {"$first": "$$ROOT"}}},
    ])}
    per = {}
    for r in rows:
        d = {k: r["doc"].get(k) for k in ["ts", *METRIC_FIELDS]}
        start = firsts.get(r["_id"], {})
        d["start_acceptance_rate"] = start.get("acceptance_rate")
        d["start_ts"] = start.get("ts")
        per[r["_id"]] = d
    # The scorer also writes an insurer "all" row with the true cross-insurer numbers; prefer it
    # over summing (which would double-count if "all" were included).
    overall = per.pop("all", None)
    per = {k: v for k, v in per.items() if k in dbm.INSURERS}
    vals = list(per.values())

    def total(k):
        return sum((v.get(k) or 0) for v in vals)

    def mean(k):
        xs = [v[k] for v in vals if v.get(k) is not None]
        return sum(xs) / len(xs) if xs else None

    if overall:
        totals = {k: overall.get(k) for k in ["start_acceptance_rate", *METRIC_FIELDS]}
    else:
        totals = {
            "acceptance_rate": mean("acceptance_rate"),
            "start_acceptance_rate": mean("start_acceptance_rate"),
            "judge_precision": mean("judge_precision"),
            "judge_recall": mean("judge_recall"),
            "appeal_win_rate": mean("appeal_win_rate"),
            "recovered_usd": total("recovered_usd"),
            "phi_leaks_to_llm": total("phi_leaks_to_llm"),
            "leaks_blocked": total("leaks_blocked"),
            "cost_usd_per_claim": mean("cost_usd_per_claim"),
        }
    return {"insurers": per, "totals": totals, "at": datetime.now(timezone.utc)}


def recent(db, coll_name, limit, insurer=None, sort=None):
    q = {"insurer": insurer} if insurer else {}
    return list(db[coll_name].find(q).sort(sort or SORT_FIELD[coll_name], -1).limit(limit))


def safe_claims(db, ids):
    proj = {f: 1 for f in CLAIM_SAFE_FIELDS}
    return {c["_id"]: c for c in db[dbm.CLAIMS].find({"_id": {"$in": list(ids)}}, proj)}


# --- app -------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app):
    hub.loop = asyncio.get_running_loop()
    db = dbm.get_db()
    threads = [threading.Thread(target=watch_collection, args=(db, c, e), daemon=True, name=f"watch-{c}") for c, e in WATCHED.items()]
    threads.append(threading.Thread(target=metrics_loop, args=(db,), daemon=True, name="metrics"))
    for t in threads:
        t.start()
    app.state.db = db
    yield
    stop.set()


app = FastAPI(title="Claim Cipher live view", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


def get(request: Request):
    return request.app.state.db


def check_token(token):
    if BUTTON_TOKEN and token != BUTTON_TOKEN:
        raise HTTPException(401, "missing or wrong X-Demo-Token")


@app.get("/health")
def health(request: Request):
    get(request).command("ping")
    return {"ok": True, "streams": hub.mode, "clients": len(hub.subscribers), "buttons_locked": bool(BUTTON_TOKEN)}


@app.get("/stream")
async def stream(request: Request):
    q = asyncio.Queue()
    hub.subscribers.add(q)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            yield sse("hello", {"streams": hub.mode, "at": datetime.now(timezone.utc)})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.subscribers.discard(q)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.get("/adjudications")
def list_adjudications(request: Request, limit: int = Query(60, le=500), insurer: str | None = None):
    return clean(recent(get(request), dbm.ADJUDICATIONS, limit, insurer))


@app.get("/adjudications/{adj_id}")
def adjudication_detail(adj_id: str, request: Request):
    """The Verdict and its evidence, resolved: clause text, comparable claims' outcomes, the appeal."""
    db = get(request)
    adj = db[dbm.ADJUDICATIONS].find_one({"_id": adj_id})
    if not adj:
        raise HTTPException(404, "adjudication not found")
    ev = ((adj.get("verdict") or {}).get("evidence")) or {}
    clauses = list(db[dbm.POLICIES].find({"_id": {"$in": ev.get("clause_ids") or []}}, {"embedding": 0}))
    comp_ids = ev.get("comparable_claim_ids") or []
    # Comparable ids may be claim ids (the Verdict spec) or adjudication ids; accept both.
    comparables = list(db[dbm.ADJUDICATIONS].find(
        {"$or": [{"claim_id": {"$in": comp_ids}}, {"_id": {"$in": comp_ids}}], "status": "paid"},
        {"claim_id": 1, "insurer": 1, "status": 1, "paid_amount_usd": 1, "carc": 1, "adjudicated_at": 1},
    ).sort("attempt", 1))
    claims = safe_claims(db, [adj["claim_id"], *(c["claim_id"] for c in comparables)])
    for c in comparables:
        c["claim"] = claims.get(c["claim_id"])
    appeal = db[dbm.APPEALS].find_one({"claim_id": adj["claim_id"]}, sort=[("created_at", -1)])
    attempts = list(db[dbm.ADJUDICATIONS].find({"claim_id": adj["claim_id"]}, {"attempt": 1, "status": 1, "carc": 1, "adjudicated_at": 1}).sort("attempt", 1))
    return clean({"adjudication": adj, "claim": claims.get(adj["claim_id"]), "clauses": clauses, "comparables": comparables, "appeal": appeal, "attempts": attempts})


@app.get("/events")
def list_events(request: Request, limit: int = Query(60, le=500), insurer: str | None = None):
    return clean(recent(get(request), dbm.HARNESS_EVENTS, limit, insurer))


@app.get("/events/{event_id}")
def event_detail(event_id: str, request: Request):
    """An event with its evidence resolved to adjudications, appeals or rules, whichever the ids point at."""
    db = get(request)
    ev = db[dbm.HARNESS_EVENTS].find_one({"_id": event_id})
    if not ev:
        raise HTTPException(404, "event not found")
    ids = ev.get("evidence_ids") or []
    evidence = {
        "adjudications": list(db[dbm.ADJUDICATIONS].find({"$or": [{"_id": {"$in": ids}}, {"claim_id": {"$in": ids}}]}, {"denial_text": 0}).limit(20)),
        "appeals": list(db[dbm.APPEALS].find({"_id": {"$in": ids}}, {"letter_tokenized": 0}).limit(20)),
        "rules": list(db[dbm.RULES].find({"_id": {"$in": ids}}).limit(20)),
    }
    related = list(db[dbm.HARNESS_EVENTS].find({"insurer": ev.get("insurer"), "_id": {"$ne": event_id}, "ts": {"$lte": ev.get("ts")}}).sort("ts", -1).limit(5)) if ev.get("ts") else []
    return clean({"event": ev, "evidence": evidence, "earlier_events": related})


@app.get("/appeals")
def list_appeals(request: Request, limit: int = Query(60, le=500), insurer: str | None = None):
    return clean(recent(get(request), dbm.APPEALS, limit, insurer))


@app.get("/appeals/{appeal_id}")
def appeal_detail(appeal_id: str, request: Request):
    db = get(request)
    appeal = db[dbm.APPEALS].find_one({"_id": appeal_id})
    if not appeal:
        raise HTTPException(404, "appeal not found")
    if not appeal.get("adjudication_id"):  # the orchestrator links appeals by claim_id only
        adj = db[dbm.ADJUDICATIONS].find_one({"claim_id": appeal["claim_id"], "status": "denied"}, {"_id": 1}, sort=[("attempt", -1)])
        appeal["adjudication_id"] = adj and adj["_id"]
    return clean(appeal)


@app.get("/metrics/latest")
def metrics_latest(request: Request):
    return clean(latest_metrics(get(request)))


@app.get("/metrics/history")
def metrics_history(request: Request, points: int = Query(120, le=2000)):
    """Newest `points` samples per insurer, oldest first, for the sparklines."""
    db = get(request)
    out = {}
    for ins in dbm.INSURERS:
        rows = list(db[dbm.METRICS].find({"insurer": ins}, {"_id": 0, "ts": 1, "acceptance_rate": 1, "appeal_win_rate": 1, "recovered_usd": 1}).sort("ts", -1).limit(points))
        out[ins] = rows[::-1]
    return clean(out)


@app.get("/profiles")
def profiles(request: Request):
    return clean(list(get(request)[dbm.HARNESS_PROFILES].find({})))


@app.post("/appeals/{appeal_id}/approve")
def approve_appeal(appeal_id: str, request: Request, x_demo_token: str | None = Header(None)):
    """Human approval for a draft-only appeal. C's appeal_worker files appeals with status "approved"."""
    check_token(x_demo_token)
    res = get(request)[dbm.APPEALS].find_one_and_update(
        {"_id": appeal_id, "outcome": None, "status": {"$nin": ["approved", "filed"]}},
        {"$set": {"status": "approved", "approved_by": "human", "approved_at": datetime.now(timezone.utc)}},
        return_document=True,
    )
    if not res:
        raise HTTPException(409, "appeal not found, already approved, or already decided")
    return clean(res)


@app.post("/demo/policy-change")
def policy_change(x_demo_token: str | None = Header(None)):
    check_token(x_demo_token)
    headers = {"X-Admin-Token": SIM_ADMIN_TOKEN} if SIM_ADMIN_TOKEN else {}
    try:
        r = httpx.post(f"{SIM_URL}/admin/policy-change/payer_c", headers=headers, timeout=10)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"simulator unreachable: {exc}") from exc
    try:
        body = r.json()
    except ValueError:
        body = r.text
    if r.status_code >= 400:  # e.g. 409 "already at its latest policy version", 403 bad admin token
        detail = body.get("detail", body) if isinstance(body, dict) else body
        raise HTTPException(r.status_code if r.status_code in (403, 409) else 502, f"simulator: {detail}")
    return {"ok": True, "simulator": body}


@app.post("/demo/reset")
async def reset(request: Request, x_demo_token: str | None = Header(None)):
    """Runs scripts/reset_demo.py. The browser must send {"confirm": "RESET"}."""
    check_token(x_demo_token)
    body = await request.json() if await request.body() else {}
    if body.get("confirm") != "RESET":
        raise HTTPException(400, 'send {"confirm": "RESET"} to reset the demo')
    cmd = [sys.executable, str(dbm.ROOT / "scripts" / "reset_demo.py"), "--yes"]
    if RESET_SNAPSHOT:
        cmd += ["--restore", RESET_SNAPSHOT]
    started = time.time()
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=120, cwd=dbm.ROOT)
    out = {"ok": proc.returncode == 0, "seconds": round(time.time() - started, 1), "output": (proc.stdout + proc.stderr)[-4000:]}
    if proc.returncode != 0:
        raise HTTPException(500, out)
    hub.publish("reset", {"collection": "*"})
    return out


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.api:app", host="0.0.0.0", port=int(os.environ.get("WEB_PORT", "8002")))
