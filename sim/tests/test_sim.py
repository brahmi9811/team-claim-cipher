from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from common.rules import apply_fix
from sim import policies as pol
from sim.app import create_app
from sim.engine import BadRequest, Conflict, NotFound, Simulator
from sim.report import first_match
from sim.rules import INSURERS, LATEST_VERSION, rules_for
from sim.stub import StubSimulator

NOW = datetime(2026, 9, 25, 15, 0, 1, tzinfo=timezone.utc)
PRICE = {"G0463": 140, "G0438": 290, "G0439": 175, "G0444": 25, "C8901": 2150, "C8908": 2450, "A0429": 650,
         "J1885": 12, "G0283": 40, "Q0084": 450, "J7030": 20, "G0108": 60, "J9355": 95, "G0378": 95,
         "G0383": 650, "G0279": 350}


def line(hcpcs: str, units: int = 1, modifiers: list[str] | None = None, n: int = 1) -> dict:
    return {"line_no": n, "hcpcs": hcpcs, "modifiers": modifiers or [], "units": units, "charge_usd": PRICE[hcpcs] * units}


def claim(insurer: str, *lines_: dict, cid: str = "clm_t0001", dx=("J06.9",), days_ago: int = 10,
          pa: str | None = None, ref: str | None = "PRV-100200") -> dict:
    lines_ = [dict(ln, line_no=i + 1) for i, ln in enumerate(lines_)]
    return {
        "_id": cid,
        "insurer": insurer,
        "service_day": -days_ago,
        "encounter_type": "ambulatory",
        "diagnosis_codes": list(dx),
        "lines": lines_,
        "total_charge_usd": float(sum(ln["charge_usd"] for ln in lines_)),
        "prior_auth_id": pa,
        "referring_provider_id": ref,
        "records": {"prior_auth_id": "PA-ZX9K2M", "referring_provider_id": "PRV-100200"},
        "holdout": False,
    }


# (insurer, policy version, rule id, claim that should trip exactly that rule first)
CASES = [
    ("payer_a", 1, "a_legit_01", claim("payer_a", line("G0463"), days_ago=120)),
    ("payer_a", 1, "a_legit_02", claim("payer_a", line("C8901"))),
    ("payer_a", 1, "a_legit_03", claim("payer_a", line("C8901"), pa="PA-ZX9K2M", ref=None)),
    ("payer_a", 1, "a_legit_04", claim("payer_a", line("G0438"), line("G0463"), dx=("Z00.00",))),
    ("payer_a", 1, "a_legit_05", claim("payer_a", line("A0429"), dx=("R07.9",))),
    ("payer_a", 1, "a_legit_06", claim("payer_a", line("G0438"), line("G0444"), dx=("Z00.00",))),
    ("payer_a", 1, "a_legit_07", claim("payer_a", line("J1885", units=6))),
    ("payer_a", 1, "a_legit_08", claim("payer_a", line("G0283", units=6))),
    ("payer_a", 1, "a_wrong_01", claim("payer_a", line("G0463"), dx=("I10",))),
    ("payer_b", 1, "b_legit_06", claim("payer_b", line("G0463"), days_ago=130)),
    ("payer_b", 1, "b_legit_01", claim("payer_b", line("C8901"))),
    ("payer_b", 1, "b_legit_02", claim("payer_b", line("G0463"), ref=None)),
    ("payer_b", 1, "b_legit_03", claim("payer_b", line("A0429"), dx=("R07.9",))),
    ("payer_b", 1, "b_legit_04", claim("payer_b", line("Q0084"), line("J7030"), dx=("C50.911",))),
    ("payer_b", 1, "b_legit_05", claim("payer_b", line("G0108", units=6), dx=("E11.9",))),
    ("payer_b", 1, "b_wrong_01", claim("payer_b", line("C8908"), pa="PA-ZX9K2M", dx=("R10.9",))),
    ("payer_b", 1, "b_wrong_02", claim("payer_b", line("G0463"), dx=("Z09",))),
    ("payer_b", 1, "b_wrong_03", claim("payer_b", line("G0463"), pa="PA-ZX9K2M")),
    ("payer_c", 1, "c_legit_10", claim("payer_c", line("G0463"), days_ago=130)),
    ("payer_c", 1, "c_legit_01", claim("payer_c", line("J9355", units=4), dx=("C50.911",))),
    ("payer_c", 1, "c_legit_02", claim("payer_c", line("C8901"), ref=None)),
    ("payer_c", 1, "c_legit_03", claim("payer_c", line("J1885", units=6))),
    ("payer_c", 1, "c_legit_04", claim("payer_c", line("G0438"), line("G0444"), dx=("Z00.00",))),
    ("payer_c", 1, "c_legit_05", claim("payer_c", line("G0438"), line("G0463"), dx=("Z00.00",))),
    ("payer_c", 1, "c_legit_06", claim("payer_c", line("G0378", units=30), dx=("R07.9",))),
    ("payer_c", 1, "c_wrong_01", claim("payer_c", line("G0383"), dx=("R07.9",))),
    ("payer_c", 2, "c_legit_07", claim("payer_c", line("G0279"), dx=("Z12.31",))),
    ("payer_c", 2, "c_legit_08", claim("payer_c", line("G0463"), ref=None)),
    ("payer_c", 2, "c_legit_09", claim("payer_c", line("G0108", units=6), dx=("E11.9",))),
]


def with_id_that_fires(insurer: str, version: int, rule_id: str, base: dict) -> dict:
    """Probabilistic rules fire for some claim ids only; find one that does."""
    for i in range(500):
        c = dict(base, _id=f"clm_t{i:04d}")
        hit = first_match(c, rules_for(insurer, version))
        if hit and hit.id == rule_id:
            return c
    raise AssertionError(f"{rule_id} never fired")


def all_rule_ids() -> set[str]:
    return {r.id for i in INSURERS for v in range(1, LATEST_VERSION[i] + 1) for r in rules_for(i, v)}


def test_every_hidden_rule_has_a_case():
    assert {case[2] for case in CASES} == all_rule_ids()


@pytest.mark.parametrize("insurer,version,rule_id,base", CASES, ids=[c[2] for c in CASES])
def test_rule_fires(insurer, version, rule_id, base):
    with_id_that_fires(insurer, version, rule_id, base)


@pytest.mark.parametrize("insurer,version,rule_id,base", [c for c in CASES if c[2].split("_")[1] == "legit"],
                         ids=[c[2] for c in CASES if c[2].split("_")[1] == "legit"])
def test_legit_rules_are_learnable(insurer, version, rule_id, base):
    rule = next(r for r in rules_for(insurer, version) if r.id == rule_id)
    if not rule.fix:
        assert rule.carc == "CO-29"  # timely filing can't be fixed after the fact
        return
    fixed = apply_fix(rule.fix, with_id_that_fires(insurer, version, rule_id, base))
    after = first_match(fixed, rules_for(insurer, version))
    assert after is None or after.id != rule_id


def test_clean_claims_are_paid():
    sim = Simulator(clock=lambda: NOW)
    for insurer in INSURERS:
        out = sim.submit(claim(insurer, line("G0463"), cid=f"clm_ok_{insurer}", dx=("J06.9",)))
        assert out["status"] == "paid", out
        assert out["paid_amount"] > 0 and out["carc"] is None


def test_deterministic():
    c = claim("payer_a", line("G0463"), dx=("I10",), cid="clm_det")
    a = Simulator(clock=lambda: NOW).submit(c)
    b = Simulator(clock=lambda: NOW).submit(c)
    assert a == b


def test_response_shape_and_denial_text():
    sim = Simulator(clock=lambda: NOW)
    out = sim.submit(claim("payer_a", line("C8901"), cid="clm_shape"))
    assert out["status"] == "denied"
    assert (out["carc"], out["rarc"]) == ("CO-16", "M62")
    assert out["adjudication_id"] == out["_adjudication_id"] == "adj_clm_shape_1"
    assert out["denial_text"].startswith("CO-16 / M62") and "C8901" in out["denial_text"]
    assert "rule" not in " ".join(out).lower()  # hidden rule ids never leave the simulator


def test_fix_and_resubmit_then_duplicate():
    sim = Simulator(clock=lambda: NOW)
    bad = claim("payer_a", line("C8901"), cid="clm_resub")
    assert sim.submit(bad)["status"] == "denied"
    fixed = dict(bad, prior_auth_id="PA-ZX9K2M")
    second = sim.submit(fixed)
    assert (second["status"], second["attempt"], second["adjudication_id"]) == ("paid", 2, "adj_clm_resub_2")
    third = sim.submit(fixed)
    assert (third["status"], third["carc"]) == ("denied", "CO-18")


def test_concurrent_submits_of_one_claim_get_distinct_attempts():
    from concurrent.futures import ThreadPoolExecutor

    sim = Simulator(clock=lambda: NOW)
    bad = claim("payer_a", line("C8901"), cid="clm_race")
    with ThreadPoolExecutor(8) as pool:
        outs = list(pool.map(lambda _i: sim.submit(bad), range(8)))
    assert sorted(o["attempt"] for o in outs) == list(range(1, 9))


def test_filing_limits_match_each_policy():
    for insurer, limit in (("payer_a", 90), ("payer_b", 120), ("payer_c", 120)):
        inside = first_match(claim(insurer, line("G0463"), days_ago=limit), rules_for(insurer, 1))
        late = first_match(claim(insurer, line("G0463"), days_ago=limit + 1), rules_for(insurer, 1))
        assert inside is None or inside.carc != "CO-29"
        assert late is not None and late.carc == "CO-29"
    assert first_match(claim("payer_c", line("G0463"), days_ago=121), rules_for("payer_c", 2)).carc == "CO-29"


def test_payer_b_bulk_denials_are_fast_and_batched():
    sim = Simulator(clock=lambda: NOW)
    base = claim("payer_b", line("C8908"), pa="PA-ZX9K2M", dx=("R10.9",))
    outs = [sim.submit(dict(base, _id=f"clm_bulk{i}")) for i in range(5)]
    assert all(o["carc"] == "CO-50" and o["latency_ms"] < 2000 for o in outs)
    assert len({o["batch_id"] for o in outs}) == 1
    assert len({o["adjudicated_at"] for o in outs}) == 1
    assert datetime.fromisoformat(outs[0]["adjudicated_at"]).second % 2 == 0
    legit = sim.submit(claim("payer_b", line("C8901"), cid="clm_slow"))
    assert legit["latency_ms"] >= 2000 and "batch_id" not in legit


def test_ground_truth_recorded():
    sim = Simulator(clock=lambda: NOW)
    c = with_id_that_fires("payer_a", 1, "a_wrong_01", claim("payer_a", line("G0463"), dx=("I10",)))
    out = sim.submit(dict(c, holdout=True))
    truth = sim.ledger.get(out["adjudication_id"])
    assert truth["kind"] == "wrongful" and truth["rule_id"] == "a_wrong_01"
    assert truth["contradicts_clause_no"] == 7 and truth["holdout"] is True


# --- appeals ----------------------------------------------------------------


def denied(sim: Simulator, insurer: str, version: int, rule_id: str, base: dict) -> dict:
    out = sim.submit(with_id_that_fires(insurer, version, rule_id, base))
    assert out["status"] == "denied"
    return out


def test_legit_denials_are_upheld():
    sim = Simulator(clock=lambda: NOW)
    out = denied(sim, "payer_a", 1, "a_legit_02", claim("payer_a", line("C8901")))
    res = sim.appeal({"claim_id": out["claim_id"], "letter": "Per clause 4 ...", "cited_clause_ids": ["payer_a_v1_c4"]})
    assert res["outcome"] == "upheld" and res["paid_amount"] == 0


def test_payer_a_needs_the_clause_number():
    sim = Simulator(clock=lambda: NOW)
    out = denied(sim, "payer_a", 1, "a_wrong_01", claim("payer_a", line("G0463"), dx=("I10",)))
    cid = out["claim_id"]
    assert sim.appeal({"claim_id": cid, "letter": "This visit is covered."})["outcome"] == "upheld"
    assert sim.appeal({"claim_id": cid, "cited_clause_ids": ["payer_a_v1_c9"]})["outcome"] == "upheld"
    won = sim.appeal({"claim_id": cid, "cited_clause_ids": ["payer_a_v1_c7"]})
    assert won["outcome"] == "overturned" and won["paid_amount"] > 0
    again = sim.appeal({"claim_id": cid, "letter": "clause 7"})
    assert again["appeal_id"] == won["appeal_id"]
    assert len(sim.ledger.get(out["adjudication_id"])["appeals"]) == 3


def test_payer_a_clause_in_letter_text():
    sim = Simulator(clock=lambda: NOW)
    out = denied(sim, "payer_a", 1, "a_wrong_01", claim("payer_a", line("G0463"), dx=("I10",)))
    res = sim.appeal({"claim_id": out["claim_id"], "letter": "Your policy, Clause 7, covers chronic care visits."})
    assert res["outcome"] == "overturned"


def test_payer_b_needs_paid_comparables_and_pattern_stats():
    sim = Simulator(clock=lambda: NOW)
    ok1 = sim.submit(claim("payer_b", line("G0463"), cid="clm_bpaid1"))
    ok2 = sim.submit(claim("payer_b", line("G0463"), cid="clm_bpaid2"))
    assert ok1["status"] == ok2["status"] == "paid"
    out = denied(sim, "payer_b", 1, "b_wrong_01", claim("payer_b", line("C8908"), pa="PA-ZX9K2M", dx=("R10.9",)))
    stats = {"identical_denials_60s": 14}
    cid = out["claim_id"]
    assert sim.appeal({"claim_id": cid, "comparable_claim_ids": ["clm_bpaid1"], "pattern_stats": stats})["outcome"] == "upheld"
    assert sim.appeal({"claim_id": cid, "comparable_claim_ids": ["clm_bpaid1", "clm_bpaid2"]})["outcome"] == "upheld"
    assert sim.appeal({"claim_id": cid, "comparable_claim_ids": ["clm_bpaid1", "nope"], "pattern_stats": stats})["outcome"] == "upheld"
    res = sim.appeal({"claim_id": cid, "comparable_claim_ids": [ok1["adjudication_id"], "clm_bpaid2"], "pattern_stats": stats})
    assert res["outcome"] == "overturned"


def test_payer_c_needs_the_current_clause_verbatim():
    sim = Simulator(clock=lambda: NOW)
    out = denied(sim, "payer_c", 1, "c_wrong_01", claim("payer_c", line("G0383"), dx=("R07.9",)))
    v1 = pol.clause_text("payer_c", 1, 5)
    cid = out["claim_id"]
    assert sim.appeal({"claim_id": cid, "letter": "Clause 5 covers emergency visits for chest pain."})["outcome"] == "upheld"
    sim.policy_change("payer_c")
    assert sim.appeal({"claim_id": cid, "letter": f'Your policy states: "{v1}"'})["outcome"] == "upheld"
    v2 = pol.clause_text("payer_c", 2, 5)
    assert v1 != v2
    res = sim.appeal({"claim_id": cid, "letter": f"Your current policy states: \u201c{v2.upper()}\u201d  Please reprocess."})
    assert res["outcome"] == "overturned"


def test_appeal_errors():
    sim = Simulator(clock=lambda: NOW)
    with pytest.raises(NotFound):
        sim.appeal({"claim_id": "clm_none"})
    with pytest.raises(BadRequest):
        sim.appeal({})
    paid = sim.submit(claim("payer_a", line("G0463"), cid="clm_paid"))
    with pytest.raises(NotFound):
        sim.appeal({"claim_id": "clm_paid", "adjudication_id": paid["adjudication_id"]})


def test_submit_rejects_bad_claims():
    sim = Simulator()
    with pytest.raises(BadRequest):
        sim.submit({"insurer": "payer_a"})
    with pytest.raises(BadRequest):
        sim.submit({"_id": "x", "insurer": "payer_z"})


# --- policies and the policy-change button ----------------------------------


def test_policies_have_12_clauses():
    for insurer in INSURERS:
        for version in range(1, LATEST_VERSION[insurer] + 1):
            docs = pol.policy_docs(insurer, version, current=True)
            assert [d["clause_no"] for d in docs] == list(range(1, 13))
            assert all(d["_id"] == f"{insurer}_v{version}_c{d['clause_no']}" and d["clause_text"] for d in docs)


def test_wrongful_rules_contradict_a_published_clause():
    for insurer in INSURERS:
        for version in range(1, LATEST_VERSION[insurer] + 1):
            for rule in rules_for(insurer, version):
                if rule.kind == "wrongful":
                    assert pol.clause_text(insurer, version, rule.contradicts_clause)


def test_policy_change_swaps_rules_for_payer_c():
    sim = Simulator(clock=lambda: NOW)
    visit = claim("payer_c", line("G0463"), ref=None)
    assert sim.submit(dict(visit, _id="clm_v1"))["status"] == "paid"
    res = sim.policy_change("payer_c")
    assert (res["policy_version"], res["previous_version"], res["policy_version_id"]) == (2, 1, "payer_c_v2")
    assert res["changed_clauses"] == [4, 5, 6, 7]
    assert not any("rule" in key for key in res)
    after = sim.submit(dict(visit, _id="clm_v2"))
    assert (after["status"], after["rarc"]) == ("denied", "N286")
    with pytest.raises(Conflict):
        sim.policy_change("payer_c")
    with pytest.raises(Conflict):
        sim.policy_change("payer_a")
    assert sim.policy_reset("payer_c")["policy_version"] == 1
    assert sim.state()["policy_versions"]["payer_c"] == 1


# --- HTTP -------------------------------------------------------------------


def test_http_end_to_end(monkeypatch):
    monkeypatch.delenv("SIM_ADMIN_TOKEN", raising=False)
    client = TestClient(create_app(Simulator(clock=lambda: NOW)))
    assert client.get("/health").json()["mode"] == "real"
    out = client.post("/submit", json=claim("payer_a", line("C8901"), cid="clm_http")).json()
    assert out["status"] == "denied"
    assert client.post("/appeal", json={"claim_id": "clm_http", "letter": "x"}).json()["outcome"] == "upheld"
    assert client.post("/appeal", json={"claim_id": "clm_nope"}).status_code == 404
    assert client.post("/submit", json={"_id": "x", "insurer": "nobody"}).status_code == 422
    assert client.post("/admin/policy-change/payer_c").json()["policy_version"] == 2
    assert client.post("/admin/policy-change/payer_c").status_code == 409
    assert client.post("/admin/policy-reset/payer_c").json()["policy_version"] == 1
    assert client.post("/admin/policy-change/payer_x").status_code == 404


def test_admin_token(monkeypatch):
    monkeypatch.setenv("SIM_ADMIN_TOKEN", "s3cret")
    client = TestClient(create_app(Simulator()))
    assert client.post("/admin/policy-change/payer_c").status_code == 403
    ok = client.post("/admin/policy-change/payer_c", headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200


def test_stub_is_valid():
    stub = StubSimulator()
    for i in range(30):
        out = stub.submit(claim(INSURERS[i % 3], line("G0463"), cid=f"clm_s{i}"))
        assert out["status"] in {"paid", "denied"}
        assert out["adjudication_id"] == out["_adjudication_id"]
        if out["status"] == "denied":
            assert out["carc"] and out["denial_text"]
            assert stub.appeal({"claim_id": out["claim_id"]})["outcome"] in {"overturned", "upheld"}


def test_raw_claim_dates_work_like_service_day():
    created = NOW - timedelta(days=1)
    raw = claim("payer_a", line("G0463"), cid="clm_raw")
    raw.pop("service_day")
    raw["created_at"] = created.isoformat()
    raw["service_date"] = (created - timedelta(days=120)).date().isoformat()
    assert first_match(raw, rules_for("payer_a", 1)).id == "a_legit_01"
