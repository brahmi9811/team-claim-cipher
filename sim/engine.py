"""The insurer simulator's decisions. Deterministic, no LLM.

`Simulator.submit` adjudicates one claim against the insurer's hidden rules
(first match decides, no match means paid) and records the ground truth.
`Simulator.appeal` decides overturned or upheld using each insurer's evidence
rule (docs/PLAN.md, "Appeal logic"):

- A legitimate denial that is appealed is always upheld.
- Payer A overturns a wrongful denial if the appeal cites the right clause number.
- Payer B overturns if the appeal includes 2 or more claims Payer B paid and the
  denial-pattern statistics.
- Payer C overturns if the appeal quotes the clause word for word from the
  current policy version.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Callable

from . import policies as pol
from .carc import denial_text
from .rules import INSURERS, LATEST_VERSION, rules_for
from .rules.base import HiddenRule, claim_id as _claim_id, roll
from .store import Ledger, MemoryLedger

ALLOWED_RATIO = {"payer_a": 0.85, "payer_b": 0.80, "payer_c": 0.82}
LATENCY_MS = {"payer_a": (1500, 6000), "payer_b": (2000, 8000), "payer_c": (1500, 7000)}
BULK_LATENCY_MS = (700, 1900)
BATCH_SECONDS = 2


class SimError(Exception):
    status_code = 400


class NotFound(SimError):
    status_code = 404


class Conflict(SimError):
    status_code = 409


class BadRequest(SimError):
    status_code = 422


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Simulator:
    mode = "real"

    def __init__(self, ledger: Ledger | None = None, clock: Callable[[], datetime] = _utcnow) -> None:
        self.ledger = ledger or MemoryLedger()
        self.clock = clock
        # FastAPI runs these handlers in a thread pool; attempt numbers and appeal
        # payouts are read-then-write, so concurrent calls for one claim must not interleave.
        self._lock = threading.RLock()

    # --- POST /submit -------------------------------------------------------

    def submit(self, claim: dict) -> dict:
        with self._lock:
            return self._submit(claim)

    def _submit(self, claim: dict) -> dict:
        cid, insurer = self._validate_claim(claim)
        history = self.ledger.history(cid)
        attempt = len(history) + 1
        version = self.ledger.policy_version(insurer)

        rule, line = self._first_match(claim, insurer, version, history)
        denied = rule is not None
        bulk = bool(rule and rule.bulk)

        low, high = BULK_LATENCY_MS if bulk else LATENCY_MS[insurer]
        latency_ms = int(low + roll(cid, str(attempt), "latency") * (high - low))
        now = self.clock()
        batch_id = None
        if bulk:
            # Payer B holds matching claims and denies them together: every denial in the
            # same 2-second window shares one timestamp and batch id.
            epoch = int(now.timestamp()) // BATCH_SECONDS * BATCH_SECONDS
            now = datetime.fromtimestamp(epoch, tz=timezone.utc)
            batch_id = f"batch_{insurer}_{epoch}"

        total = round(float(claim.get("total_charge_usd") or 0.0), 2)
        paid_amount = 0.0 if denied else round(total * ALLOWED_RATIO[insurer], 2)
        adjudication_id = f"adj_{cid}_{attempt}"
        carc = rule.carc if denied else None
        rarc = rule.rarc if denied else None
        text = denial_text(carc, rarc, line if isinstance(line, dict) else None) if denied else None

        self.ledger.record({
            "_id": adjudication_id,
            "type": "adjudication",
            "adjudication_id": adjudication_id,
            "claim_id": cid,
            "insurer": insurer,
            "kind": rule.kind if denied else None,
            "rule_id": rule.id if denied else None,
            "status": "denied" if denied else "paid",
            "carc": carc,
            "rarc": rarc,
            "attempt": attempt,
            "holdout": bool(claim.get("holdout", False)),
            "contradicts_clause_no": rule.contradicts_clause if denied else None,
            "policy_version": version,
            "total_charge_usd": total,
            "paid_amount": paid_amount,
            "latency_ms": latency_ms,
            "batch_id": batch_id,
            "adjudicated_at": _iso(now),
            "appeals": [],
        })

        response = {
            "claim_id": cid,
            "adjudication_id": adjudication_id,
            "_adjudication_id": adjudication_id,  # the orchestrator uses this as its adjudication _id
            "attempt": attempt,
            "status": "denied" if denied else "paid",
            "carc": carc,
            "rarc": rarc,
            "denial_text": text,
            "paid_amount": paid_amount,
            "adjudicated_at": _iso(now),
            "latency_ms": latency_ms,
        }
        if batch_id:
            response["batch_id"] = batch_id
        return response

    def _validate_claim(self, claim: dict) -> tuple[str, str]:
        if not isinstance(claim, dict):
            raise BadRequest("claim must be a JSON object")
        cid = _claim_id(claim)
        if not cid:
            raise BadRequest("claim needs an _id (or claim_id)")
        insurer = claim.get("insurer")
        if insurer not in INSURERS:
            raise BadRequest(f"unknown insurer {insurer!r}; expected one of {', '.join(INSURERS)}")
        if not isinstance(claim.get("lines") or [], list):
            raise BadRequest("lines must be a list")
        return cid, insurer

    def _first_match(self, claim: dict, insurer: str, version: int, history: list[dict]) -> tuple[HiddenRule | None, object]:
        if any(h["status"] == "paid" for h in history):
            return _duplicate_rule(insurer), True
        for rule in rules_for(insurer, version):
            hit = rule.fires(claim)
            if hit:
                return rule, hit
        return None, None

    # --- POST /appeal -------------------------------------------------------

    def appeal(self, request: dict) -> dict:
        with self._lock:
            return self._appeal(request)

    def _appeal(self, request: dict) -> dict:
        cid = request.get("claim_id")
        if not cid:
            raise BadRequest("appeal needs a claim_id")
        adj_id = request.get("adjudication_id")
        target = self.ledger.get(adj_id) if adj_id else self.ledger.latest_denial(cid)
        if not target or target["status"] != "denied" or target["claim_id"] != cid:
            raise NotFound(f"no denied adjudication for claim {cid}")

        previous = target.get("appeals") or []
        won = next((a for a in previous if a["outcome"] == "overturned"), None)
        if won:
            return {**won, "note": "Already overturned; no further payment."}

        overturned = self._overturns(target, request)
        now = self.clock()
        result = {
            "appeal_id": f"simapl_{target['_id']}_{len(previous) + 1}",
            "claim_id": cid,
            "adjudication_id": target["_id"],
            "outcome": "overturned" if overturned else "upheld",
            "paid_amount": round(target["total_charge_usd"] * ALLOWED_RATIO[target["insurer"]], 2) if overturned else 0.0,
            "decided_at": _iso(now),
            "note": "Denial reversed on appeal; claim reprocessed for payment." if overturned else "Original determination upheld.",
        }
        self.ledger.add_appeal(target["_id"], {k: result[k] for k in ("appeal_id", "outcome", "paid_amount", "decided_at")})
        return result

    def _overturns(self, target: dict, request: dict) -> bool:
        if target.get("kind") != "wrongful":
            return False
        insurer = target["insurer"]
        clause_no = target.get("contradicts_clause_no")
        letter = str(request.get("letter") or "")
        if insurer == "payer_a":
            return cites_clause(insurer, clause_no, request.get("cited_clause_ids") or [], letter)
        if insurer == "payer_b":
            comparables = [i for i in request.get("comparable_claim_ids") or [] if i not in (target["claim_id"], target["_id"])]
            paid = self.ledger.paid_claims(comparables, insurer) - {target["claim_id"]}
            return len(paid) >= 2 and has_pattern_stats(request.get("pattern_stats"))
        if insurer == "payer_c":
            version = self.ledger.policy_version(insurer)
            return quotes_clause(pol.clause_text(insurer, version, clause_no), letter)
        return False

    # --- POST /admin/policy-change/{payer} and /admin/policy-reset/{payer} --

    def policy_change(self, insurer: str) -> dict:
        self._known(insurer)
        with self._lock:
            current = self.ledger.policy_version(insurer)
            if current >= LATEST_VERSION[insurer]:
                raise Conflict(f"{insurer} is already at its latest policy version (v{current})")
            return self._publish(insurer, current + 1, current)

    def policy_reset(self, insurer: str) -> dict:
        self._known(insurer)
        with self._lock:
            return self._publish(insurer, 1, self.ledger.policy_version(insurer))

    def _publish(self, insurer: str, version: int, previous: int) -> dict:
        self.ledger.set_policy_version(insurer, version)
        old = {c["clause_no"]: c["clause_text"] for c in pol.policy_docs(insurer, previous, current=False)}
        new = pol.policy_docs(insurer, version, current=True)
        return {
            "insurer": insurer,
            "policy_version": version,
            "policy_version_id": f"{insurer}_v{version}",
            "previous_version": previous,
            "clauses": len(new),
            "changed_clauses": [c["clause_no"] for c in new if old.get(c["clause_no"]) != c["clause_text"]],
        }

    def _known(self, insurer: str) -> None:
        if insurer not in INSURERS:
            raise NotFound(f"unknown insurer {insurer!r}")

    def state(self) -> dict:
        return {"policy_versions": {i: self.ledger.policy_version(i) for i in INSURERS}, "store": self.ledger.kind}


# --- appeal evidence checks (shared with tests) ------------------------------


def cites_clause(insurer: str, clause_no: int | None, cited_ids: list[str], letter: str) -> bool:
    if clause_no is None:
        return False
    pattern = re.compile(rf"^{insurer}_v\d+_c{clause_no}$")
    if any(pattern.match(str(c)) for c in cited_ids):
        return True
    return re.search(rf"\bclause\s*(?:no\.?|number|#)?\s*{clause_no}\b", letter, re.I) is not None


def has_pattern_stats(stats) -> bool:
    if not isinstance(stats, dict):
        return False
    return any(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in stats.values())


def quotes_clause(clause: str, letter: str) -> bool:
    return pol.normalize(clause).rstrip(".") in pol.normalize(letter)


def _duplicate_rule(insurer: str) -> HiddenRule:
    return HiddenRule(f"{insurer[-1]}_legit_dup", "legit", lambda _c: True, "CO-18", None,
                      "Claim already paid (duplicate).")
