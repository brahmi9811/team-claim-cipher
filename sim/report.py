"""Denial-mix report for tuning: `python -m sim.report [claims.jsonl]`.

Runs every forge claim through the hidden rules and prints, per insurer:

- cold start: what the agents face with no learned rules (target: about 65%
  paid, and of the denials about 60% legitimate / 40% wrongful);
- ceiling: what a perfect scrubber could reach by learning every fixable
  legitimate rule, using B's real `common.rules.apply_fix` (proves the rules are
  learnable, and shows the headroom for the demo curve);
- Payer C after the policy change: how far acceptance drops when v2 swaps rules.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

from .rules import INSURERS, rules_for
from .rules.base import HiddenRule


def first_match(claim: dict, rules: list[HiddenRule]) -> HiddenRule | None:
    return next((r for r in rules if r.fires(claim)), None)


def perfect_scrub(claim: dict, rules: list[HiddenRule]) -> tuple[dict, HiddenRule | None]:
    """Apply the fix for each fixable legit denial until the claim passes or can't be fixed."""
    from common.rules import apply_fix

    for _ in range(12):
        rule = first_match(claim, rules)
        if rule is None or rule.kind != "legit" or not rule.fix or rule.fix["fix"]["action"] == "hold_for_review":
            return claim, rule
        fixed = apply_fix(rule.fix, claim)
        if fixed == claim:
            return claim, rule
        claim = fixed
    return claim, first_match(claim, rules)


def summarize(outcomes: list[HiddenRule | None]) -> dict:
    n = len(outcomes)
    legit = sum(1 for r in outcomes if r and r.kind == "legit")
    wrongful = sum(1 for r in outcomes if r and r.kind == "wrongful")
    denied = legit + wrongful
    return {
        "n": n,
        "paid": (n - denied) / n if n else 0,
        "legit": legit / n if n else 0,
        "wrongful": wrongful / n if n else 0,
        "legit_share_of_denials": legit / denied if denied else 0,
    }


def run(claims: list[dict]) -> dict:
    by_insurer: dict[str, list[dict]] = defaultdict(list)
    for c in claims:
        by_insurer[c["insurer"]].append(c)

    report = {"cold": {}, "ceiling": {}, "rules": {}, "payer_c_v2": None}
    all_cold, all_ceiling = [], []
    for insurer in INSURERS:
        rules = rules_for(insurer, 1)
        cold = [first_match(c, rules) for c in by_insurer[insurer]]
        scrubbed = [perfect_scrub(c, rules) for c in by_insurer[insurer]]
        ceiling = [r for _c, r in scrubbed]
        report["cold"][insurer] = summarize(cold)
        report["ceiling"][insurer] = summarize(ceiling)
        report["rules"][insurer] = Counter(r.id for r in cold if r)
        all_cold += cold
        all_ceiling += ceiling
        if insurer == "payer_c":
            v2 = rules_for("payer_c", 2)
            report["payer_c_v2"] = summarize([first_match(c, v2) for c, _r in scrubbed])
    report["cold"]["all"] = summarize(all_cold)
    report["ceiling"]["all"] = summarize(all_ceiling)
    return report


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def print_report(report: dict) -> None:
    print("\nCOLD START (no learned rules)            target: ~65% paid, denials ~60% legit / 40% wrongful")
    print(f"{'insurer':10} {'claims':>6} {'paid':>7} {'legit':>7} {'wrongful':>9} {'legit/denials':>14}")
    for insurer, s in report["cold"].items():
        print(f"{insurer:10} {s['n']:6d} {_pct(s['paid'])} {_pct(s['legit'])} {_pct(s['wrongful']):>9} {_pct(s['legit_share_of_denials']):>14}")

    print("\nCEILING (perfect scrubber: every fixable legit rule learned)")
    for insurer, s in report["ceiling"].items():
        print(f"{insurer:10} {s['n']:6d} {_pct(s['paid'])} {_pct(s['legit'])} {_pct(s['wrongful']):>9}")

    s = report["payer_c_v2"]
    print(f"\nPAYER C AFTER POLICY CHANGE (v1 rules learned, v2 active): paid {_pct(s['paid']).strip()}, "
          f"legit {_pct(s['legit']).strip()}, wrongful {_pct(s['wrongful']).strip()}")

    print("\nDENIALS PER HIDDEN RULE (cold start)")
    for insurer, counts in report["rules"].items():
        print(f"  {insurer}: " + ", ".join(f"{rid} {n}" for rid, n in sorted(counts.items())))


def main() -> None:
    from forge.load import CLAIMS_FILE, read_jsonl

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else CLAIMS_FILE
    if not path.exists():
        sys.exit(f"{path} not found. Run: python -m forge build")
    print_report(run(read_jsonl(path)))


if __name__ == "__main__":
    main()
