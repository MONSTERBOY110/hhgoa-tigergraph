"""Case lifecycle: append-only events with a hash chain, state, pending approvals, handover."""
import json
import sys
import types

import pytest

from kavach import casefile

CASE = "TEST-CASEFILE"


@pytest.fixture(autouse=True)
def tmp_casefiles(tmp_path, monkeypatch):
    monkeypatch.setenv("KAVACH_CASEFILE_DIR", str(tmp_path))
    return tmp_path


def _answer(status="escalated", verdict="fraud", requests=True):
    final = [{"action": "BLOCK_CARD", "route": "L1", "reason": "R2: block and reissue"},
             {"action": "CREATE_CASE", "route": "auto", "reason": "R2 and §3a"},
             {"action": "FILE_REPORT", "route": "L2", "reason": "§3a: exposure over $1,000"},
             {"action": "ESCALATE_TO_ANALYST", "route": "auto", "reason": "R9: hand to an analyst"}]
    initial = [{"action": "VERIFY_WITH_CUSTOMER", "route": "auto", "reason": "R1: confirm before blocking"},
               {"action": "CREATE_CASE", "route": "auto", "reason": "R2"}]
    if verdict == "legitimate":
        final = [{"action": "CLOSE_NO_FRAUD", "route": "auto", "reason": "R3"}]
    return {"case_id": CASE,
            "case": {"status": status, "verdict": verdict, "fraud_probability": 0.91 if verdict == "fraud" else 0.08,
                     "affected_txn_ids": ["3476602", "3476633"] if verdict == "fraud" else [], "connected_card_ids": [],
                     "exposure_usd": 1200.0 if verdict == "fraud" else 0},
            "evidence_requests": [{"type": "customer_validation", "asked_after_step": 6,
                                   "assumed_response": "Customer states they did not make these purchases"}] if requests else [],
            "next_best_actions": {"initial": initial if requests else final, "final": final,
                                  "what_changed": "The denial raised p from 0.62 to 0.91." if requests else "nothing"}}


def _trace():
    return {"assess0": {"p": 0.62, "verdict": "uncertain", "fraud_groups": ["sequence"], "legit_groups": [], "by_group": {"sequence": 0.5}},
            "assess1": {"p": 0.91, "verdict": "fraud", "fraud_groups": ["sequence", "customer"], "legit_groups": [],
                        "by_group": {"sequence": 0.5, "customer": 2.2}}}


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        casefile.append(CASE, "deleted", "nope")


def test_append_events_and_chain(tmp_casefiles):
    e1 = casefile.append(CASE, "opened", "alert received", evidence_ids=["3476682"])
    e2 = casefile.append(CASE, "gathered", "261 card txns", extra={"n": 261})
    evs = casefile.events(CASE)
    assert [e["kind"] for e in evs] == ["opened", "gathered"] and [e["seq"] for e in evs] == [1, 2]
    assert evs[0]["ts"].endswith("Z") and evs[0]["evidence_ids"] == ["3476682"] and evs[0]["actor"] == "agent"
    assert e2["prev_hash"] == e1["hash"] and casefile.verify_chain(CASE)
    # edit a past line: the chain breaks
    p = tmp_casefiles / f"{CASE}.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[0])
    ev["detail"] = "rewritten"
    lines[0] = json.dumps(ev)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not casefile.verify_chain(CASE)
    assert "BROKEN" in casefile.handover(CASE)


def test_state_pending_approvals_and_handover():
    casefile.append(CASE, "opened", "alert received")
    casefile.record_answer(_answer(), _trace())
    s = casefile.state(CASE)
    assert s["status"] == "escalated" and s["verdict"] == "fraud" and s["p"] == 0.91
    assert sorted(x["action"] for x in s["pending_approvals"]) == ["BLOCK_CARD", "FILE_REPORT"]
    assert s["last_decision"]["kind"] == "escalated"
    assert any("did not make" in w["detail"] for w in s["what_changed"])
    # an L1 approver signs off the block in a later sitting
    casefile.append(CASE, "approved", "block confirmed", actor="L1", approval_id="A-0001",
                    extra={"action": "BLOCK_CARD", "route": "L1"})
    s = casefile.state(CASE)
    assert [x["action"] for x in s["pending_approvals"]] == ["FILE_REPORT"]
    h = casefile.handover(CASE)
    for needle in ("Case TEST-CASEFILE: escalated, waiting on 1 approval(s)", "FILE_REPORT needs L2 approval",
                   "Who decided what", "[approval A-0001]", "What changed", "Timeline", "Audit chain: intact"):
        assert needle in h, needle
    assert casefile.verify_chain(CASE)


def test_rerun_keeps_prior_approval_and_counts_sittings():
    casefile.append(CASE, "opened", "first sitting")
    casefile.record_answer(_answer(), _trace())
    casefile.append(CASE, "approved", "ok", actor="L2", approval_id="A-2", extra={"action": "FILE_REPORT", "route": "L2"})
    casefile.append(CASE, "rerun", "same case run again")
    casefile.record_answer(_answer(), _trace())
    s = casefile.state(CASE)
    assert s["sittings"] == 2
    assert [x["action"] for x in s["pending_approvals"]] == ["BLOCK_CARD"]


def test_closed_legitimate_waits_on_nothing():
    casefile.append(CASE, "opened", "alert received")
    casefile.record_answer(_answer(status="closed_legitimate", verdict="legitimate", requests=False))
    s = casefile.state(CASE)
    assert s["status"] == "closed_legitimate" and s["pending_approvals"] == []
    assert "nothing: no L1 or L2 action is waiting" in casefile.handover(CASE)


def test_failed_run_is_visible():
    casefile.append(CASE, "opened", "alert received")
    casefile.append(CASE, "failed", "ToolFailure at step 4: device_neighbors", extra={"incident": "incidents/x.json"})
    s = casefile.state(CASE)
    assert s["status"] == "failed"
    assert "the last run failed" in casefile.handover(CASE)


def test_harness_approvals_resolve_pending(monkeypatch):
    """An approval recorded only in the harness approvals log (server side) also resolves the action."""
    pkg = types.ModuleType("kavach.harness")
    appr = types.ModuleType("kavach.harness.approvals")
    approved = {"BLOCK_CARD"}
    appr.pending = lambda case_id, actions: [{**a, "needs_role": a["route"]} for a in actions
                                             if a["route"] in ("L1", "L2") and a["action"] not in approved]
    pkg.approvals = appr
    monkeypatch.setitem(sys.modules, "kavach.harness", pkg)
    monkeypatch.setitem(sys.modules, "kavach.harness.approvals", appr)
    casefile.append(CASE, "opened", "alert received")
    casefile.record_answer(_answer(), _trace())
    s = casefile.state(CASE)
    assert [(x["action"], x["needs_role"]) for x in s["pending_approvals"]] == [("FILE_REPORT", "L2")]


def test_cli(capsys):
    assert casefile.main(["NOPE-000"]) == 1
    casefile.append(CASE, "opened", "alert received")
    assert casefile.main([CASE]) == 0
    assert "Case TEST-CASEFILE: open" in capsys.readouterr().out
    assert casefile.main([CASE, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "open"
