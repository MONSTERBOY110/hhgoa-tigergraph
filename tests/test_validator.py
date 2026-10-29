import copy
import csv
import dataclasses
import json
from pathlib import Path

import pytest

from kavach.config import DATA, RAW2
from kavach.validate import ROUNDS, IdIndex, load_index, load_pack, review, run_grader, validate_answer

EX = json.loads((Path(__file__).parent / "fixtures" / "readme_example.json").read_text(encoding="utf-8"))


def test_readme_example_structurally_valid():
    # the README example omits a rule citation on one action; our own files are held to strict mode
    assert validate_answer(EX, None, strict=False) == []


def test_bad_route_fails():
    a = copy.deepcopy(EX)
    a["next_best_actions"]["final"][2]["route"] = "L1"
    assert any("FILE_REPORT route" in e for e in validate_answer(a, None))


def test_sar_mismatch_fails():
    a = copy.deepcopy(EX)
    a["sar"]["file"] = False
    assert any("sar.file disagrees" in e for e in validate_answer(a, None))


def test_legit_with_episode_fails():
    a = copy.deepcopy(EX)
    a["case"]["verdict"] = "legitimate"
    assert any("legitimate but" in e for e in validate_answer(a, None))


def test_enum_violation_fails():
    a = copy.deepcopy(EX)
    a["case"]["pattern"] = "card testing"
    assert validate_answer(a, None)


def test_dash_fails():
    a = copy.deepcopy(EX)
    a["case"]["summary"] += " " + chr(0x2014) + " bad"
    assert any("dash" in e for e in validate_answer(a, None))


def test_no_request_final_must_equal_initial():
    a = copy.deepcopy(EX)
    a["evidence_requests"] = []
    assert any("final != initial" in e for e in validate_answer(a, None))


# ---------------------------------------------------------------- Round 2: grader rules and grader port

JAN = RAW2 / "transactions_2017q1.csv"
needs_r2 = pytest.mark.skipif(
    not (JAN.exists() and ROUNDS[2]["pack"].exists() and (DATA / "kavach_r2.duckdb").exists()),
    reason="Round 2 raw files or data/kavach_r2.duckdb not present")
NO_SAR = {"file": False, "reason": "3a: no report owed", "narrative": "", "subjects": [], "total_amount_usd": 0,
          "activity_dates": []}


def _jan_rows() -> dict:
    """TransactionID -> (amount, TransactionDT) read straight from the organizer January file."""
    out = {}
    with open(JAN, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out[r["TransactionID"]] = (float(r["TransactionAmt"]), int(r["TransactionDT"]))
    return out


@pytest.fixture(scope="module")
def r2():
    jan = _jan_rows()
    pack = load_pack(2)
    # base case chosen by rule from the pack, never by id: first non-dispute case whose flagged January txn
    # is over 500 (so R8 applies) and at most 2500 (so BLOCK_CARD routes L1)
    base = next(r for r in pack.values() if r["trigger_type"] != "customer_report"
                and r["flagged_txn_id"] in jan and 500 < jan[r["flagged_txn_id"]][0] <= 2500)
    other = next(r for r in pack.values() if r["card_id"] != base["card_id"])
    return {"jan": jan, "pack": pack, "base": base, "other": other, "idx": load_index(2)}


def _act(action, exposure, reason):
    from kavach.answer import route_for

    return {"action": action, "route": route_for(action, exposure), "reason": reason}


def _valid(r2) -> dict:
    """Minimal valid HHF-style fraud answer on the base case's flagged January txn (amount from the file)."""
    b, other = r2["base"], r2["other"]
    tid = b["flagged_txn_id"]
    exp = round(r2["jan"][tid][0], 2)
    narrative = (
        f"Card {b['card_id']} of customer {b['customer_id']} made online transaction {tid} for ${exp:.2f} in January 2017. "
        f"The cardholder did not recognise the purchase when asked by the bank. "
        f"The same purchase pattern appears on card {other['card_id']} in the same month. "
        f"Both cards share an origin element that the analyst confirmed in the graph. "
        f"The activity is consistent with stolen card details being used online. "
        f"Total suspicious amount is ${exp:.2f} on one transaction.")
    final = [_act("BLOCK_CARD", exp, "R2: customer denied"), _act("CREATE_CASE", exp, "R2 and 3a"),
             _act("FILE_REPORT", exp, "3a: connected to another card"), _act("MONITOR_CONNECTED_CARDS", exp, "R6")]
    return {
        "case_id": b["case_id"],
        "case": {
            "status": "closed_fraud", "verdict": "fraud", "fraud_probability": 0.9, "pattern": "card_not_present_fraud",
            "pattern_description": "", "affected_txn_ids": [tid], "first_suspicious_txn_id": tid,
            "connected_card_ids": [other["card_id"]], "connected_device_profiles": [], "exposure_usd": exp,
            "evidence": [{"claim": "Online purchase the cardholder did not recognise", "source": "graph",
                          "ref": f"query:card_window(card_id={b['card_id']})", "entity_ids": [tid, b["card_id"]]},
                         {"claim": "Customer did not recognise the purchase", "source": "customer",
                          "ref": "evidence_request:1", "entity_ids": []}],
            "similar_prior_cases": [],
            "summary": "Online purchase not recognised by the cardholder, linked to another card.",
            "written_to_graph": False, "graph_case_id": ""},
        "evidence_requests": [{"type": "customer_validation", "asked_after_step": 3,
                               "assumed_response": "Customer says they did not make this purchase"}],
        "next_best_actions": {
            "initial": [_act("VERIFY_WITH_CUSTOMER", exp, "R1: confirm before blocking"), _act("CREATE_CASE", exp, "3a")],
            "final": final,
            "what_changed": "The customer denied the purchase, so the card is blocked under R2 and a report is filed under 3a."},
        "sar": {"file": True, "reason": "3a: connected to another card", "narrative": narrative,
                "subjects": [b["customer_id"], b["card_id"], other["card_id"]], "total_amount_usd": exp,
                "activity_dates": ["2017-01-01", "2017-01-31"]},
        "stop_reason": "Customer denial settled the question (section 6).", "tool_calls": 5, "tokens": 0, "latency_s": 1.0,
    }


def _review(r2, a, idx=None):
    return review(a, idx or r2["idx"], f"{a['case_id']}.json", round=2)


def _drop(lst, action):
    return [x for x in lst if x["action"] != action]


@needs_r2
def test_r2_valid_answer_has_no_errors_or_warnings(r2):
    assert _review(r2, _valid(r2)) == ([], [])


@needs_r2
def test_r2_affected_outside_grader_scope(r2):
    a = _valid(r2)
    r1_tid = next(iter(load_pack(1).values()))["flagged_txn_id"]  # a Round 1 (2016) transaction: exists, not January
    a["case"]["affected_txn_ids"].append(r1_tid)
    errs, _ = _review(r2, a)
    assert f"grader scope: affected txn {r1_tid} is not in transactions_2017q1.csv" in errs
    a["case"]["first_suspicious_txn_id"] = r1_tid
    errs, _ = _review(r2, a)
    assert f"grader scope: first_suspicious_txn_id {r1_tid} is not in transactions_2017q1.csv" in errs


@needs_r2
def test_r2_first_must_be_earliest_by_dt(r2):
    a = _valid(r2)
    tid = a["case"]["first_suspicious_txn_id"]
    earlier = min(r2["jan"], key=lambda t: r2["jan"][t][1])
    assert r2["jan"][earlier][1] < r2["jan"][tid][1]
    a["case"]["affected_txn_ids"].append(earlier)
    exp = round(r2["jan"][tid][0] + r2["jan"][earlier][0], 2)
    a["case"]["exposure_usd"] = a["sar"]["total_amount_usd"] = exp
    errs, _ = _review(r2, a)
    assert errs == [f"grader: first_suspicious_txn_id {tid} is not the earliest affected txn by TransactionDT ({earlier})"]


@needs_r2
def test_r2_exposure_tolerances(r2):
    a = _valid(r2)
    exp = a["case"]["exposure_usd"]
    near = round(exp + 0.015, 3)  # inside the grader tolerance, outside our own 0.011
    a["case"]["exposure_usd"] = a["sar"]["total_amount_usd"] = near
    errs, _ = _review(r2, a)
    assert errs == [f"exposure {near} != sum(abs(amt)) {exp}"]
    off = round(exp + 50, 2)
    a["case"]["exposure_usd"] = a["sar"]["total_amount_usd"] = off
    errs, _ = _review(r2, a)
    tol = max(0.02, exp * 0.005)
    assert f"grader: exposure_usd {off} != sum of affected amounts {exp} (tolerance {tol:.2f})" in errs


@needs_r2
def test_r2_fraud_connected_needs_report(r2):
    a = _valid(r2)
    a["next_best_actions"]["final"] = _drop(a["next_best_actions"]["final"], "FILE_REPORT")
    a["sar"] = dict(NO_SAR)
    errs, _ = _review(r2, a)
    assert errs == ["3a: fraud with connected cards or exposure over 1000 requires FILE_REPORT in final"]


@needs_r2
def test_r2_fraud_needs_case(r2):
    a = _valid(r2)
    a["next_best_actions"]["final"] = _drop(a["next_best_actions"]["final"], "CREATE_CASE")
    errs, _ = _review(r2, a)
    assert "3a: fraud requires CREATE_CASE in final" in errs


@needs_r2
def test_r2_undocumented_needs_r9_triple(r2):
    a = _valid(r2)
    a["case"]["pattern"] = "undocumented"
    a["case"]["pattern_description"] = "Several cards share an origin before the loss."
    errs, _ = _review(r2, a)
    assert errs == ["R9: undocumented pattern requires ['ESCALATE_TO_ANALYST'] in final"]


@needs_r2
def test_r2_uncertain_over_500_escalates(r2):
    a = _valid(r2)
    exp = a["case"]["exposure_usd"]
    a["case"].update(verdict="uncertain", status="open", fraud_probability=0.5, connected_card_ids=[])
    a["next_best_actions"]["final"] = [_act("CREATE_CASE", exp, "3a"), _act("MONITOR_CARD", exp, "R4")]
    a["sar"] = dict(NO_SAR)
    errs, _ = _review(r2, a)
    assert errs == [f"R8: uncertain with exposure {exp} over 500 requires ESCALATE_TO_ANALYST in final"]


@needs_r2
def test_r2_final_block_on_weak_signal(r2):
    a = _valid(r2)
    a["case"]["fraud_probability"] = 0.6
    a["next_best_actions"]["initial"] = [_act("CREATE_CASE", a["case"]["exposure_usd"], "3a")]
    errs, _ = _review(r2, a)
    assert errs == ["R1: ['BLOCK_CARD'] in final at probability 0.6 on a non-dispute trigger needs "
                    "VERIFY_WITH_CUSTOMER or STEP_UP_AUTH in initial"]
    # a customer dispute is R2 territory: no verification step needed
    idx = r2["idx"]
    disputed = dict(idx.cases[a["case_id"]], trigger_type="customer_report")
    idx2 = dataclasses.replace(idx, cases={**idx.cases, a["case_id"]: disputed})
    assert _review(r2, a, idx2)[0] == []


@needs_r2
@pytest.mark.parametrize("where", ["evidence_requests", "final WARN_CUSTOMER reason", "summary", "evidence claim 1"])
def test_r2_tipoff(r2, where):
    a = _valid(r2)
    bad = "we may file a report with the regulator"
    if where == "evidence_requests":
        a["evidence_requests"][0]["assumed_response"] = bad
    elif where == "summary":
        a["case"]["summary"] = bad
    elif where == "evidence claim 1":
        a["case"]["evidence"][0]["claim"] = bad
    else:
        a["next_best_actions"]["final"].append(_act("WARN_CUSTOMER", a["case"]["exposure_usd"], "R7: " + bad))
    errs, _ = _review(r2, a)
    assert errs == [f"tip-off: {where} mentions a report or regulator ('file a report')"]


@needs_r2
def test_r2_thin_sar_narrative(r2):
    a = _valid(r2)
    a["sar"]["narrative"] = "One. Two words. Three more words. Four. Five here. Six final words now."
    errs, _ = _review(r2, a)
    assert errs == ["sar narrative has 13 words (want at least 40)"]


@needs_r2
def test_r2_what_changed_required_when_sets_differ(r2):
    a = _valid(r2)
    a["next_best_actions"]["what_changed"] = " "
    errs, _ = _review(r2, a)
    assert errs == ["3b: what_changed is empty but the final action set differs from the initial one"]


@needs_r2
def test_r2_warnings_never_fail(r2):
    a = _valid(r2)
    flagged = a["case"]["affected_txn_ids"][0]
    other_jan = next(t for t in r2["jan"] if t != flagged)
    exp = round(r2["jan"][other_jan][0], 2)
    a["case"].update(fraud_probability=0.45, connected_card_ids=[], affected_txn_ids=[other_jan],
                     first_suspicious_txn_id=other_jan, exposure_usd=exp)
    a["sar"]["total_amount_usd"] = exp
    nba = a["next_best_actions"]
    for phase in ("initial", "final"):
        nba[phase] = [_act(x["action"], exp, x["reason"]) for x in nba[phase]]
    nba["final"] = [dict(x, route="L1") if x["action"] == "MONITOR_CONNECTED_CARDS" else x for x in nba["final"]]
    nba["final"].append(_act("BLOCK_ALL_CARDS", exp, "R10: two cards confirmed"))
    errs, warns = _review(r2, a)
    assert "final: MONITOR_CONNECTED_CARDS route L1 != auto" in errs  # the route itself is still an error
    assert "BLOCK_ALL_CARDS in final: the grader raises policy finding r10_block_all every time" in warns
    assert "verdict fraud with probability 0.45 below 0.5" in warns
    assert f"flagged txn {flagged} is not among affected_txn_ids" in warns
    assert "final: MONITOR_CONNECTED_CARDS is an auto action but is routed L1" in warns
    if exp <= 1000:
        assert f"FILE_REPORT with exposure {exp} (not over 1000) and no connected cards: 3a may not require it" in warns
    legit = _valid(r2)
    legit["case"].update(verdict="legitimate", fraud_probability=0.6)
    assert "verdict legitimate with probability 0.6 above 0.5" in _review(r2, legit)[1]


@needs_r2
def test_r2_round1_index_unions_january(r2):
    """With the Round 1 index (no January, no grader_scope table) the January ids come from the raw files."""
    r1db = DATA / "kavach.duckdb"
    if not r1db.exists():
        pytest.skip("Round 1 index not present")
    idx = IdIndex.from_duck(round=2, duck_path=r1db)
    tid = r2["base"]["flagged_txn_id"]
    assert idx.scope is not None and len(idx.scope) == len(r2["jan"])
    assert idx.scope[tid] == r2["jan"][tid] and idx.txn[tid] == r2["jan"][tid][0]
    assert _review(r2, _valid(r2), idx) == ([], [])


@needs_r2
def test_r2_expected_files_come_from_pack(r2):
    with open(ROUNDS[2]["pack"], newline="", encoding="utf-8") as fh:
        ids = [r["case_id"] for r in csv.DictReader(fh)]
    assert sorted(r2["idx"].cases) == sorted(ids) and len(ids) == len(set(ids))


@needs_r2
def test_run_grader_on_valid_file(r2, tmp_path):
    a = _valid(r2)
    f = tmp_path / f"{a['case_id']}.json"
    f.write_text(json.dumps(a, indent=1), encoding="utf-8")
    rep = run_grader(tmp_path, round=2, only_present=True, quiet=True)
    team = rep["team"]
    assert (team["cases_answered"], team["hard"], team["policy"]) == (1, 0, 0), rep["summary_line"]
    assert team["cases"][a["case_id"]]["findings"] == []
    # and the grader agrees with our own rule on a broken file
    a["case"]["exposure_usd"] = a["sar"]["total_amount_usd"] = round(a["case"]["exposure_usd"] + 50, 2)
    f.write_text(json.dumps(a, indent=1), encoding="utf-8")
    findings = run_grader(tmp_path, round=2, only_present=True, quiet=True)["team"]["cases"][a["case_id"]]["findings"]
    assert [x[1] for x in findings] == ["exposure_mismatch"]
