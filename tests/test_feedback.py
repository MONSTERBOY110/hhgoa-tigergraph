"""Analyst in the loop: accept or reject findings, role checks, the hash chain, and how adjust() changes weights."""
import json
from types import SimpleNamespace

import pytest

from kavach import feedback as fb
from kavach.assess import Signal
from kavach.harness.faults import Forbidden

TOK = {"analyst": "analyst-token-0123456789", "L1": "l1-token-0123456789abc", "L2": "l2-token-0123456789abc"}
CTX = {"trigger": "risk_score", "product": "R", "channel": "online"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KAVACH_FEEDBACK_FILE", str(tmp_path / "feedback.jsonl"))
    monkeypatch.setenv("KAVACH_TOKEN_ANALYST", TOK["analyst"])
    monkeypatch.setenv("KAVACH_TOKEN_L1", TOK["L1"])
    monkeypatch.setenv("KAVACH_TOKEN_L2", TOK["L2"])
    monkeypatch.delenv("KAVACH_FEEDBACK", raising=False)
    monkeypatch.setattr(fb, "_dataset_version", lambda: "test")
    fb._cache.clear()
    yield
    fb._cache.clear()


def sigs():
    return [Signal("device_ring", "network", 2.0, "Rare device profile used on 4 other cards", ["C00001-K1"], "", "graph",
                   {"tool": "device_txns", "params": {"device_key": "Windows | other | chrome 61.0 | 1280x720", "days": 30}}),
            Signal("device_new_for_account", "device", -0.4, "Device New for this account", [], "", "graph",
                   {"tool": "card_history", "params": {"card_id": "C00001-K1"}}),
            Signal("dq_score_mismatch", "score", 0.0, "zero weight note")]


def rec(decision="reject", role="analyst", scope="context", finding="device_ring", context=CTX, signals=None, case="CASE-1"):
    return fb.record(case, finding, decision, TOK[role], "reviewed against the closed record", scope=scope,
                     signals=signals or sigs(), context=context, flagged_txn_id=3500001)


def test_record_reads_role_from_token_and_chains():
    r1 = rec()
    r2 = rec("accept", role="L2")
    assert r1["status"] == "recorded" and r1["role"] == "analyst" and r2["role"] == "L2"
    assert r1["key"] == {"detector": "device_ring", "trigger": "risk_score", "channel": "online", "product": "R"}
    assert r1["signal"]["llr"] == 2.0 and r1["signal"]["how"]["tool"] == "device_txns"
    assert r2["prev_hash"] == r1["hash"] and r1["id"].startswith("FB-")
    assert TOK["analyst"] not in fb.path().read_text()          # the token itself is never stored
    assert fb.verify() == (True, 2)


def test_unknown_token_is_denied_and_logged():
    with pytest.raises(Forbidden):
        fb.record("CASE-1", "device_ring", "reject", "not-a-real-token-123456", "x", signals=sigs(), context=CTX)
    rows = fb.records()
    assert len(rows) == 1 and rows[0]["status"] == "denied" and rows[0]["role"] == "none"
    assert fb.verify()[0] and fb.active_records() == []


def test_channel_scope_needs_l1():
    with pytest.raises(Forbidden):
        rec(scope="channel", role="analyst")
    r = rec(scope="channel", role="L1")
    assert "product" not in r["key"] and r["scope"] == "channel"


def test_bad_inputs():
    with pytest.raises(ValueError):
        rec(finding="not_on_this_case")
    with pytest.raises(ValueError):
        rec(decision="maybe")
    with pytest.raises(ValueError):
        fb.record("CASE-1", "device_ring", "reject", TOK["analyst"], "  ", signals=sigs(), context=CTX)
    with pytest.raises(ValueError):          # a finding with no shared element cannot be scoped to one
        rec(finding="device_new_for_account", scope="element")
    assert fb.records() == []


def test_reject_down_weights_only_the_matching_context():
    r = rec()
    out = fb.adjust(sigs(), None, context=CTX)
    ring = out[0]
    assert ring.llr == pytest.approx(0.5) and ring.feedback["records"] == [r["id"]] and ring.feedback["factor"] == 0.25
    assert r["id"] in ring.claim and "down-weighted" in ring.claim
    assert out[1].llr == -0.4 and not hasattr(out[1], "feedback")
    for other in ({**CTX, "product": "C"}, {**CTX, "trigger": "customer_report"}, {**CTX, "channel": "in_person"}):
        assert fb.adjust(sigs(), None, context=other)[0].llr == 2.0


def test_inputs_are_not_mutated_and_sign_never_flips():
    rec()
    rec(finding="device_new_for_account")
    s = sigs()
    out = fb.adjust(s, None, context=CTX)
    assert s[0].llr == 2.0 and not hasattr(s[0], "feedback")
    assert out[1].llr == pytest.approx(-0.1)          # a rejected legit-leaning finding loses weight, stays legit-leaning
    assert out[2].llr == 0.0 and not hasattr(out[2], "feedback")


def test_channel_and_element_scopes():
    rec(scope="channel", role="L1")
    assert fb.adjust(sigs(), None, context={**CTX, "product": "C"})[0].llr == pytest.approx(0.5)
    fb.path().unlink()
    fb._cache.clear()
    rec(scope="element")
    assert fb.adjust(sigs(), None, context=CTX)[0].llr == pytest.approx(0.5)
    other = sigs()
    other[0].how = {"tool": "device_txns", "params": {"device_key": "iOS Device | iOS 11 | safari | 375x667"}}
    assert fb.adjust(other, None, context=CTX)[0].llr == 2.0


def test_accept_raises_weight_capped():
    rec("accept")
    assert fb.adjust(sigs(), None, context=CTX)[0].llr == pytest.approx(2.2)
    rec("accept", role="L1")
    rec("accept", role="L2")
    assert fb.adjust(sigs(), None, context=CTX)[0].llr == pytest.approx(2.4)     # x1.2 cap, not x1.3


def test_latest_decision_per_approver_and_ties():
    rec("reject")
    rec("accept")                       # same analyst changes their mind: the later record wins
    assert fb.adjust(sigs(), None, context=CTX)[0].llr == pytest.approx(2.2)
    rec("reject", role="L1")            # one accept, one reject from different people: unchanged
    assert fb.adjust(sigs(), None, context=CTX)[0].llr == 2.0


def test_switch_off_and_force(monkeypatch):
    rec()
    monkeypatch.setenv("KAVACH_FEEDBACK", "0")
    assert fb.adjust(sigs(), None, context=CTX)[0].llr == 2.0
    assert fb.adjust(sigs(), None, context=CTX, force=True)[0].llr == pytest.approx(0.5)


def test_tampered_log_fails_closed():
    rec()
    rec(finding="device_new_for_account")
    lines = fb.path().read_text().splitlines()
    first = json.loads(lines[0])
    first["decision"] = "accept"
    fb.path().write_text("\n".join([json.dumps(first, sort_keys=True)] + lines[1:]) + "\n")
    fb._cache.clear()
    assert not fb.verify()[0]
    with pytest.raises(fb.FeedbackIntegrityError):
        fb.adjust(sigs(), None, context=CTX)


def test_context_from_a_case_context_and_dict_signals():
    rec()
    ctx = SimpleNamespace(case={"trigger_type": "risk_score"}, f={"ProductCD": "R", "channel": "online"})
    assert fb.adjust(sigs(), ctx)[0].llr == pytest.approx(0.5)
    as_dicts = [dict(s.__dict__) for s in sigs()]
    out = fb.adjust(as_dicts, ctx)
    assert out[0]["llr"] == pytest.approx(0.5) and out[0]["feedback"]["records"] and "feedback" not in as_dicts[0]
    assert fb.changed(out)[0]["signal"] == "device_ring"


def test_no_log_is_a_no_op():
    s = sigs()
    assert [x.llr for x in fb.adjust(s, None, context=CTX)] == [2.0, -0.4, 0.0]
