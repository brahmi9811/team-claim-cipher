"""Data Forge CLI.

    python -m forge download          # fetch the Synthea sample CSVs into data/synthea/csv
    python -m forge build             # build ~2,000 claims -> data/forge/claims.jsonl (+ phi_canaries.jsonl)
    python -m forge load              # claims (encrypted), policies and PHI canaries -> MongoDB
    python -m forge policies          # only (re)load the published policies
    python -m forge all               # download if needed, build, load
    python -m forge extend --rounds 4 # append rounds 2..4 (clm_r02_00001, ...) for a long run, then `load`
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime

from . import load as out
from .claims import build_claims
from .phi import plant_phi
from .synthea import SYNTHEA_DIR, available, download, read_encounters, read_patients


def cmd_download(_args) -> None:
    print(f"Downloading the Synthea sample into {SYNTHEA_DIR} ...")
    download()
    print("Done.")


def cmd_build(args) -> list[dict]:
    if not available():
        sys.exit("Synthea CSVs not found. Run: python -m forge download")
    patients = read_patients()
    encounters = read_encounters()
    claims = build_claims(patients, encounters, n=args.n, seed=args.seed)
    canaries = plant_phi(claims, share=args.phi_share, seed=args.seed)
    out.write_jsonl(claims, out.CLAIMS_FILE)
    out.write_jsonl(canaries, out.CANARIES_FILE)

    by_insurer = Counter(c["insurer"] for c in claims)
    print(f"Built {len(claims)} claims from {len(patients)} Synthea patients / {len(encounters)} encounters")
    print(f"  per insurer: {dict(sorted(by_insurer.items()))}")
    print(f"  holdout: {sum(c['holdout'] for c in claims)}  planted PHI: {len(canaries)}")
    print(f"  -> {out.CLAIMS_FILE}")
    print("  Check the denial mix with: python -m sim.report")
    return claims


def cmd_load(args) -> None:
    claims = out.read_jsonl(out.CLAIMS_FILE) if out.CLAIMS_FILE.exists() else None
    if claims is None:
        sys.exit("No data/forge/claims.jsonl yet. Run: python -m forge build")
    print("Policies:", out.load_policies())
    print("Claims:", out.load_claims(claims, allow_plaintext=args.allow_plaintext))
    canaries = out.read_jsonl(out.CANARIES_FILE) if out.CANARIES_FILE.exists() else []
    print("PHI canaries (hashed) in sim_truth:", out.load_canaries(canaries))


def cmd_policies(_args) -> None:
    print("Policies:", out.load_policies())


def round_prefix(k: int) -> str:
    """Round 1 keeps clm_00001; later rounds sort after it (and after each other) for the orchestrator's _id cursor."""
    return "clm_" if k == 1 else f"clm_r{k:02d}_"


def cmd_extend(args) -> None:
    if not out.CLAIMS_FILE.exists():
        sys.exit("No data/forge/claims.jsonl yet. Run: python -m forge build")
    if not available():
        sys.exit("Synthea CSVs not found. Run: python -m forge download")
    claims = out.read_jsonl(out.CLAIMS_FILE)
    canaries = out.read_jsonl(out.CANARIES_FILE) if out.CANARIES_FILE.exists() else []
    now = datetime.fromisoformat(claims[0]["created_at"])  # same dates as round 1
    ids = {c["_id"] for c in claims}
    patients, encounters = read_patients(), read_encounters()
    added = 0
    for k in range(2, args.rounds + 1):
        prefix = round_prefix(k)
        if f"{prefix}00001" in ids:
            continue
        batch = build_claims(patients, encounters, n=args.n, seed=args.seed + k - 1, now=now, id_prefix=prefix)
        canaries += plant_phi(batch, share=args.phi_share, seed=args.seed + k - 1)
        claims += batch
        added += len(batch)
    out.write_jsonl(claims, out.CLAIMS_FILE)
    out.write_jsonl(canaries, out.CANARIES_FILE)
    print(f"Added {added} claims; {len(claims)} in total, {sum(c['holdout'] for c in claims)} held out, "
          f"{len(canaries)} with planted PHI.")
    print("  Load the new ones with: python -m forge load")


def cmd_all(args) -> None:
    if not available():
        cmd_download(args)
    cmd_build(args)
    cmd_load(args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m forge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["download", "build", "load", "policies", "all", "extend"])
    ap.add_argument("--n", type=int, default=2000, help="number of claims to build (per round)")
    ap.add_argument("--rounds", type=int, default=4, help="extend: total rounds of claims, including round 1")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--phi-share", type=float, default=0.05, help="share of claims with planted PHI in notes")
    ap.add_argument("--allow-plaintext", action="store_true", help="local dev database only: write claims unencrypted")
    args = ap.parse_args()
    {"download": cmd_download, "build": cmd_build, "load": cmd_load, "policies": cmd_policies, "all": cmd_all,
     "extend": cmd_extend}[args.command](args)


if __name__ == "__main__":
    main()
