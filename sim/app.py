"""Insurer simulator HTTP service (FastAPI, port 8001).

    POST /submit                          tokenized claim -> paid or denied + CARC/RARC
    POST /appeal                          {claim_id, letter, cited_clause_ids, comparable_claim_ids, pattern_stats}
    POST /admin/policy-change/{payer}     next policy version (Payer C: v1 -> v2); 409 if already latest
    POST /admin/policy-reset/{payer}      back to version 1 (used by scripts/reset_demo.py)
    GET  /health

Environment:
    SIM_MODE=stub          random but valid responses (the 11:15 stub)
    SIM_STORE=memory       keep the ledger in memory even when MONGODB_URI is set
    SIM_ADMIN_TOKEN        if set, /admin/* requires the X-Admin-Token header
"""
from __future__ import annotations

import logging
import os

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .engine import SimError, Simulator
from .store import MemoryLedger, MongoLedger
from .stub import StubSimulator

log = logging.getLogger("sim")


def build_simulator() -> Simulator | StubSimulator:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if os.environ.get("SIM_MODE", "real") == "stub":
        return StubSimulator()
    if os.environ.get("MONGODB_URI") and os.environ.get("SIM_STORE", "mongo") != "memory":
        return Simulator(MongoLedger())
    log.warning("No MONGODB_URI (or SIM_STORE=memory): the ledger lives in memory and sim_truth is not persisted")
    return Simulator(MemoryLedger())


def create_app(sim: Simulator | StubSimulator | None = None) -> FastAPI:
    app = FastAPI(title="Claim Cipher insurer simulator")
    app.state.sim = sim

    def get_sim(request: Request):
        if request.app.state.sim is None:
            request.app.state.sim = build_simulator()
        return request.app.state.sim

    def check_admin(token: str | None) -> None:
        expected = os.environ.get("SIM_ADMIN_TOKEN")
        if expected and token != expected:
            raise HTTPException(403, "missing or wrong X-Admin-Token")

    @app.exception_handler(SimError)
    async def sim_error(_request: Request, exc: SimError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.get("/health")
    def health(request: Request):
        sim = get_sim(request)
        return {"ok": True, "mode": sim.mode, **sim.state()}

    @app.post("/submit")
    def submit(request: Request, claim: dict = Body(...)):
        return get_sim(request).submit(claim)

    @app.post("/appeal")
    def appeal(request: Request, body: dict = Body(...)):
        return get_sim(request).appeal(body)

    @app.post("/admin/policy-change/{payer}")
    def policy_change(payer: str, request: Request, x_admin_token: str | None = Header(None)):
        check_admin(x_admin_token)
        return get_sim(request).policy_change(payer)

    @app.post("/admin/policy-reset/{payer}")
    def policy_reset(payer: str, request: Request, x_admin_token: str | None = Header(None)):
        check_admin(x_admin_token)
        return get_sim(request).policy_reset(payer)

    return app


app = create_app()
