"""Env-driven config. Names match `.env.example` (README, Member B specs) so
nothing here needs to change once B's real settings module lands."""
from __future__ import annotations

import os

CLAIM_RATE_PER_SEC = float(os.environ.get("CLAIM_RATE_PER_SEC", "2"))
EVOLVER_CYCLE_SECONDS = float(os.environ.get("EVOLVER_CYCLE_SECONDS", "300"))
EVOLVER_CYCLE_OUTCOMES = int(os.environ.get("EVOLVER_CYCLE_OUTCOMES", "50"))
APPEAL_POLL_SECONDS = float(os.environ.get("APPEAL_POLL_SECONDS", "5"))
MAX_CLAIMS = int(os.environ.get("MAX_CLAIMS", "0"))  # 0 = run forever, per docs/PLAN.md's "runs all day"
BATCH_SIZE = int(os.environ.get("CLAIM_BATCH_SIZE", "10"))
# Claims processed at once. The simulator holds each claim 2.5-6 s, so at
# CLAIM_RATE_PER_SEC=2 about 12 are in flight; 20 leaves headroom.
MAX_IN_FLIGHT = int(os.environ.get("MAX_IN_FLIGHT", "20"))
INSURERS = ["payer_a", "payer_b", "payer_c"]
