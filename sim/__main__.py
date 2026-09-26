"""Run the simulator: `python -m sim` (port 8001), `python -m sim --stub`."""
from __future__ import annotations

import argparse
import logging
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="Claim Cipher insurer simulator")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SIM_PORT", "8001")))
    ap.add_argument("--host", default=os.environ.get("SIM_HOST", "0.0.0.0"))
    ap.add_argument("--stub", action="store_true", help="random but valid responses (the 11:15 stub)")
    ap.add_argument("--memory", action="store_true", help="keep the ledger in memory even if MONGODB_URI is set")
    args = ap.parse_args()
    if args.stub:
        os.environ["SIM_MODE"] = "stub"
    if args.memory:
        os.environ["SIM_STORE"] = "memory"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("sim.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
