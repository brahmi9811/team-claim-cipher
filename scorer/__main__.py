"""Run the scorer: `python -m scorer` (every 30 s), `python -m scorer --once --dry-run`."""
from __future__ import annotations

import argparse
import logging
import time

from .score import DEFAULT_WINDOW, run_once

log = logging.getLogger("scorer")


def _line(doc: dict) -> str:
    def pct(key):
        v = doc.get(key)
        return "  -  " if v is None else f"{v * 100:4.0f}%"

    return (f"{doc['insurer']:8} accept {pct('acceptance_rate')} (n={doc['holdout_sample']})  "
            f"judge P {pct('judge_precision')} R {pct('judge_recall')}  "
            f"appeals {pct('appeal_win_rate')}  recovered ${doc['recovered_usd']:,.0f}  "
            f"PHI to LLM {doc['phi_leaks_to_llm']}  blocked {doc['leaks_blocked']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Honest scoreboard: writes metrics every 30 seconds")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="held-out first submissions per insurer for acceptance")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write to metrics")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    while True:
        try:
            for doc in run_once(window=args.window, write=not args.dry_run):
                print(_line(doc))
        except Exception as exc:  # noqa: BLE001 - keep the scoreboard alive through blips
            log.warning("scoring failed: %s", exc)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
