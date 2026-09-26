from __future__ import annotations

from datetime import datetime, timezone

from forge.phi import value_hash
from scorer.score import candidate_hashes, compute_metrics

NOW = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)


def truth(n: int, insurer: str = "payer_a", *, status: str = "paid", kind: str | None = None, attempt: int = 1,
          holdout: bool = True, appeals: list | None = None, claim: str | None = None) -> dict:
    cid = claim or f"clm_{insurer}_{n:04d}"
    return {"_id": f"adj_{cid}_{attempt}", "type": "adjudication", "claim_id": cid, "insurer": insurer,
            "status": status, "kind": kind, "attempt": attempt, "holdout": holdout,
            "adjudicated_at": f"2026-09-25T14:{n // 60:02d}:{n % 60:02d}+00:00", "appeals": appeals or []}


def by_insurer(docs):
    return {d["insurer"]: d for d in docs}


def test_acceptance_uses_recent_holdout_first_attempts():
    docs = [truth(i, status="denied", kind="legit") for i in range(40)]  # old, all denied
    docs += [truth(40 + i, status="paid") for i in range(60)]  # recent, all paid
    docs += [truth(200 + i, status="denied", holdout=False) for i in range(50)]  # not held out
    docs += [truth(300, status="paid", attempt=2, claim="clm_payer_a_0001")]  # resubmission
    m = by_insurer(compute_metrics(docs, [], window=60, now=NOW))
    assert m["payer_a"]["acceptance_rate"] == 1.0
    assert m["payer_a"]["holdout_sample"] == 60
    assert m["payer_b"]["acceptance_rate"] is None
    assert m["all"]["acceptance_rate"] == 0.6  # window of 180 over 100 held-out first attempts


def test_judge_precision_and_recall():
    docs = [truth(1, status="denied", kind="wrongful"), truth(2, status="denied", kind="wrongful"),
            truth(3, status="denied", kind="legit"), truth(4, status="denied", kind="legit")]
    verdicts = [
        {"_id": docs[0]["_id"], "claim_id": docs[0]["claim_id"], "verdict": {"label": "wrongful_policy"}},  # TP
        {"_id": "orch_x", "claim_id": docs[1]["claim_id"], "verdict": {"label": "legitimate"}},  # FN, joined by claim
        {"_id": docs[2]["_id"], "claim_id": docs[2]["claim_id"], "verdict": {"label": "wrongful_bulk"}},  # FP
        {"_id": docs[3]["_id"], "claim_id": docs[3]["claim_id"], "verdict": {"label": "legitimate"}},  # TN
        {"_id": "adj_unknown", "claim_id": "clm_unknown", "verdict": {"label": "wrongful_bulk"}},  # ignored
        {"_id": docs[0]["_id"], "claim_id": docs[0]["claim_id"], "verdict": None},  # not judged yet
    ]
    m = by_insurer(compute_metrics(docs, verdicts, now=NOW))
    assert m["payer_a"]["judge_precision"] == 0.5
    assert m["payer_a"]["judge_recall"] == 0.5
    assert m["payer_a"]["judged_denials"] == 4
    assert m["payer_b"]["judge_precision"] is None


def test_appeals_and_recovered_dollars():
    docs = [
        truth(1, "payer_c", status="denied", kind="wrongful",
              appeals=[{"outcome": "upheld", "paid_amount": 0.0}, {"outcome": "overturned", "paid_amount": 533.0}]),
        truth(2, "payer_c", status="denied", kind="legit", appeals=[{"outcome": "upheld", "paid_amount": 0.0}]),
    ]
    m = by_insurer(compute_metrics(docs, [], now=NOW))
    assert m["payer_c"]["appeal_win_rate"] == round(1 / 3, 4)
    assert m["payer_c"]["recovered_usd"] == 533.0
    assert m["all"]["recovered_usd"] == 533.0


def test_phi_leaks_and_blocks():
    canary = {"_id": "canary_x", "type": "phi_canary", "claim_id": "clm_x", "insurer": "payer_b",
              "value_hashes": [value_hash("ssn", "999-12-3456"), value_hash("name", "Jane Doe"),
                               value_hash("dob", "04121961"), value_hash("dob", "19610412")]}
    outputs = [
        {"insurer": "payer_b", "text": "Denied; patient SSN 999123456 on file"},  # leak
        {"insurer": "payer_b", "text": "Dear Jane Doe, ...", "names_allowed": True},  # rendered letter: fine
        {"insurer": "payer_a", "text": "spoke to jane  doe"},  # leak (name)
        {"insurer": "payer_c", "text": "PATIENT_0042, age band 60-69, CO-50"},  # clean
        {"insurer": "payer_c", "text": "born 1961-04-12"},  # leak (ISO date)
    ]
    incidents = [{"insurer": "payer_b"}, {"insurer": "payer_b"}, {"insurer": "payer_a"}]
    m = by_insurer(compute_metrics([canary], [], incidents, outputs, now=NOW))
    assert (m["payer_a"]["phi_leaks_to_llm"], m["payer_b"]["phi_leaks_to_llm"], m["payer_c"]["phi_leaks_to_llm"]) == (1, 1, 1)
    assert m["all"]["phi_leaks_to_llm"] == 3
    assert m["payer_b"]["leaks_blocked"] == 2 and m["all"]["leaks_blocked"] == 3


def test_candidate_hashes_formats():
    hashes = candidate_hashes("call (508) 555-0142 or member PAM123456789, DOB 04/12/1961")
    assert value_hash("phone", "(508) 555-0142") in hashes
    assert value_hash("member_id", "PAM123456789") in hashes
    assert value_hash("dob", "04121961") in hashes


def test_document_shape():
    docs = compute_metrics([], [], now=NOW)
    assert [d["insurer"] for d in docs] == ["payer_a", "payer_b", "payer_c", "all"]
    for d in docs:
        assert d["ts"] == NOW
        assert set(d) >= {"acceptance_rate", "judge_precision", "judge_recall", "appeal_win_rate", "recovered_usd",
                          "phi_leaks_to_llm", "leaks_blocked", "cost_usd_per_claim"}
        assert d["phi_leaks_to_llm"] == 0 and d["cost_usd_per_claim"] is None
