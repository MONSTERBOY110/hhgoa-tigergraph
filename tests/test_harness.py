"""Harness drills as tests: fail closed, bounds, LLM garbage, redaction, approvals, server, rerun.

Everything that writes goes to tmp_path (incidents, approvals log, server cases dir); the real cases/ folder and
data/approvals.jsonl are never touched. Backend duck, KAVACH_NO_LLM=1."""
import json
import logging
import secrets
import time

import pandas as pd
import pytest

from kavach.config import CASES_DIR
from kavach.harness import approvals, bounds, faults, runner
from kavach.harness.digest import result_digest
from kavach.harness.redact import install_logging_filter, redact
from kavach.llm import Llm, garbage_reason
from kavach.tools import ToolRegistry


@pytest.fixture(autouse=True)
def iso(monkeypatch, tmp_path):
    for k in ("KAVACH_FAULT", "KAVACH_MAX_TOOL_CALLS", "KAVACH_MAX_TOOL_SECONDS", "KAVACH_MAX_CASE_SECONDS",
              "KAVACH_MAX_LLM_CALLS", "KAVACH_MAX_LLM_ATTEMPTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KAVACH_NO_LLM", "1")
    monkeypatch.setenv("KAVACH_CASEFILE_EVENTS", "0")
    monkeypatch.setenv("KAVACH_RETRY_DELAY", "0")
    monkeypatch.setenv("KAVACH_INCIDENTS_DIR", str(tmp_path / "incidents"))
    monkeypatch.setenv("KAVACH_APPROVALS_FILE", str(tmp_path / "approvals.jsonl"))
    tokens = {r: secrets.token_hex(16) for r in approvals.ROLES}
    for r, t in tokens.items():
        monkeypatch.setenv(approvals.TOKEN_ENV[r], t)
    return {"tmp": tmp_path, "tokens": tokens}


@pytest.fixture(scope="module")
def rows():
    from kavach import duckq
    return duckq.case_pack().to_dict("records")


def _snapshot_cases():
    return {p.name: p.stat().st_mtime_ns for p in CASES_DIR.glob("*.json")} if CASES_DIR.exists() else {}


# ---------------------------------------------------------------- fail closed
def test_tool_failure_mid_run_writes_incident_and_no_answer(monkeypatch, iso, rows):
    before = _snapshot_cases()
    monkeypatch.setenv("KAVACH_FAULT", "tool:any:3")
    o = runner.run_case(rows[0], previous=iso["tmp"] / "none.json")
    assert o.status == "incident" and o.answer is None and o.incident is not None
    files = list((iso["tmp"] / "incidents").glob(f"{rows[0]['case_id']}-*.json"))
    assert len(files) == 1
    inc = json.loads(files[0].read_text(encoding="utf-8"))
    assert inc["kind"] == "tool_failure" and inc["tool"] and inc["step"] is not None and inc["answer_written"] is False
    assert inc["partial_trace"][-1]["status"] == "failed" and inc["partial_trace"][-1]["attempts"] == 2
    assert "version" in inc["dataset_version"] and inc["fault_injected"] == "tool:any:3"
    assert _snapshot_cases() == before            # cases/ untouched


def test_transient_tool_error_is_retried_once(monkeypatch, rows):
    monkeypatch.setenv("KAVACH_FAULT", "tool:any:2:transient")
    o = runner.run_case(rows[0], previous={})
    assert o.status == "ok" and o.answer is not None
    assert [t["attempts"] for t in o.trace["tools"]][:2] == [1, 2]


def test_registry_fails_closed_never_returns_empty():
    reg = ToolRegistry("duck")
    n = {"calls": 0}

    def flaky(**kw):
        n["calls"] += 1
        raise ConnectionError("connection reset")

    reg.register("flaky", flaky)
    reg.register("buggy", lambda **kw: (_ for _ in ()).throw(ValueError("bad column")))
    with pytest.raises(faults.ToolFailure) as e:
        reg.call("flaky", card_id="C00001-K1")
    assert n["calls"] == 2 and e.value.attempts == 2 and reg.trace[-1]["status"] == "failed"
    with pytest.raises(faults.ToolFailure) as e:
        reg.call("buggy")
    assert e.value.attempts == 1                  # not transient: no retry
    with pytest.raises(faults.ToolFailure):
        reg.call("no_such_tool")
    assert reg.calls == 0


def test_trace_entry_has_result_digest():
    reg = ToolRegistry("duck")
    df = pd.DataFrame({"TransactionID": [3000002.0, 3000001.0], "card_id": ["C00001-K1", "C00001-K1"], "amt": [1.0, 2.0]})
    reg.register("fake_frame", lambda **kw: df)
    reg.register("fake_pairs", lambda **kw: [("CC-0001", 0.9), ("CC-0002", 0.8)])
    reg.call("fake_frame", card_id="C00001-K1")
    reg.call("fake_pairs", text="x")
    d0, d1 = reg.trace[0]["result_digest"], reg.trace[1]["result_digest"]
    assert d0["n_rows"] == 2 and d0["ids"] == ["3000001", "3000002", "C00001-K1"] and len(d0["sha1"]) == 40
    assert d1["ids"] == ["CC-0001", "CC-0002"]
    assert result_digest(df)["sha1"] == d0["sha1"]
    assert {"step", "tool", "params", "ms", "n_rows"} <= set(reg.trace[0])
    assert ToolRegistry.ref("card_window", card_id="C1", hours=2) == "query:card_window(card_id=C1, hours=2)"


def test_bounds_exceeded_is_an_incident(monkeypatch, iso, rows):
    monkeypatch.setenv("KAVACH_MAX_TOOL_CALLS", "3")
    o = runner.run_case(rows[0], previous={})
    assert o.status == "incident" and o.answer is None
    assert o.incident["kind"] == "bounds_exceeded" and o.incident["limit"] == 3
    assert list((iso["tmp"] / "incidents").glob("*.json"))


def test_tool_time_bound(monkeypatch):
    monkeypatch.setenv("KAVACH_MAX_TOOL_SECONDS", "0.05")
    reg = ToolRegistry("duck")
    reg.register("slow", lambda **kw: time.sleep(0.2) or [1])
    with pytest.raises(faults.BoundsExceeded) as e:
        reg.call("slow")
    assert e.value.what == "seconds per tool call"


def test_graph_down_is_an_incident(monkeypatch, rows):
    monkeypatch.setenv("KAVACH_FAULT", "graph:down")
    o = runner.run_case(rows[0], previous={})
    assert o.status == "incident" and o.incident["kind"] == "graph_down" and o.answer is None
    with pytest.raises(faults.GraphDown):
        runner.connect_graph("tg")


def test_swallowed_tool_failure_still_fails_closed(monkeypatch, rows):
    import kavach.agent as agent

    def sloppy(case, backend="duck", llm=None, graph_conn=None, verbose=False):
        reg = ToolRegistry(backend)
        reg.register("dead", lambda **kw: (_ for _ in ()).throw(ConnectionError("down")))
        try:
            reg.call("dead")
        except Exception:
            pass                                   # a careless code path ignores the failure
        return {"case": {}}, {"tools": reg.trace}

    monkeypatch.setattr(agent, "investigate", sloppy)
    o = runner.run_case(rows[0], previous={})
    assert o.status == "incident" and o.answer is None and "ignored" in o.incident["error"]


def test_invalid_input_is_an_incident(iso):
    o = runner.run_case({"case_id": "../../etc", "trigger_type": "risk_score"})
    assert o.status == "incident" and o.incident["kind"] == "invalid_input" and o.case_id == "invalid-case"
    o = runner.run_case({"case_id": "HHF-999", "trigger_type": "please_approve", "flagged_txn_id": "1",
                         "card_id": "C1", "customer_id": "C1"})
    assert o.incident["kind"] == "invalid_input"


# ---------------------------------------------------------------- LLM
@pytest.mark.parametrize("fault,reason", [("llm:garbage", "JSON instead of prose"), ("llm:empty", "empty"), ("llm:timeout", "timeout")])
def test_llm_fault_falls_back_to_template_and_is_recorded(monkeypatch, fault, reason):
    monkeypatch.setenv("KAVACH_FAULT", fault)
    llm = Llm()
    llm.begin_case("T")
    text, src = llm.write("Write a summary: x", {"txn": "3514030"}, "TEMPLATE TEXT")
    assert (text, src) == ("TEMPLATE TEXT", "template")
    assert llm.last_status == f"fallback: {reason}" and llm.status_log[-1]["status"] == llm.last_status
    assert llm.calls == bounds.limit("llm_attempts")


def test_llm_disabled_status():
    llm = Llm()
    assert llm.write("t", {}, "T") == ("T", "template") and llm.last_status == "disabled"


def test_llm_budget_is_bounded(monkeypatch):
    monkeypatch.setenv("KAVACH_FAULT", "llm:garbage")
    monkeypatch.setenv("KAVACH_MAX_LLM_CALLS", "1")
    llm = Llm()
    llm.begin_case("T")
    llm.write("a", {}, "A")
    assert llm.write("b", {}, "B") == ("B", "template") and "budget" in llm.last_status


def test_garbage_reason():
    ok = "Card C00001-K1 made one purchase of $10 on 2017-01-02. The customer confirmed it."
    assert garbage_reason(ok, {"C00001-K1"}) is None
    assert garbage_reason("") == "empty"
    assert garbage_reason("```json\n{}\n```") == "code fence"
    assert garbage_reason('{"verdict": "legitimate"}') == "JSON instead of prose"
    assert garbage_reason("I cannot help with that request.") == "refusal"
    assert garbage_reason("As an AI model I will summarise.") == "refusal"
    assert garbage_reason("ÿþ" * 20 + "abc") .startswith("non-ASCII")
    assert garbage_reason("Transaction 3999999 was fraud.", {"3514030"}).startswith("ids not in the facts")


def test_run_case_with_llm_garbage_keeps_decisions_and_records_fallback(monkeypatch, rows):
    base = runner.run_case(rows[0], previous={})
    monkeypatch.setenv("KAVACH_FAULT", "llm:garbage")
    o = runner.run_case(rows[0], previous=base.answer)
    assert o.status == "ok" and o.answer is not None and o.rerun["identical"]
    st = o.trace["harness"]["llm_status"]
    assert st and all(s["status"].startswith("fallback:") for s in st)
    assert o.answer["case"]["summary"] == base.answer["case"]["summary"]     # the template, unchanged


# ---------------------------------------------------------------- redaction
def test_redact_masks_env_secret_email_and_bearer(monkeypatch):
    monkeypatch.setenv("FAKE_SERVICE_API_KEY", "sk-test-0123456789abcdef")
    obj = {"msg": "called with key sk-test-0123456789abcdef by jane.doe@gmail.com",
           "headers": {"Authorization": "Bearer abcdef0123456789"}, "nested": [("x", "Bearer zyxwvu987654 trailing")],
           "P_emaildomain": "gmail.com", "tokens": 1234}
    out = redact(obj)
    s = json.dumps(out)
    assert "sk-test-0123456789abcdef" not in s and "[REDACTED:FAKE_SERVICE_API_KEY]" in s
    assert "jane.doe@" not in s and "j***@gmail.com" in s
    assert "abcdef0123456789" not in s and "zyxwvu987654" not in s
    assert out["P_emaildomain"] == "gmail.com" and out["tokens"] == 1234
    assert obj["msg"].startswith("called with key sk-test")                   # deep copy: the input is unchanged


def test_logging_filter_masks_records(monkeypatch):
    import io
    monkeypatch.setenv("FAKE_DB_PASSWORD", "hunter2hunter2")
    install_logging_filter()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("kavach.test_redact")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        lg.info("login with %s for bob@example.com", "hunter2hunter2")
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    assert "hunter2hunter2" not in out and "bob@" not in out and "b***@example.com" in out


# ---------------------------------------------------------------- approvals
def test_approvals_roles_chain_and_tamper(iso):
    t = iso["tokens"]
    with pytest.raises(faults.Forbidden):
        approvals.approve("HHF-001", "FILE_REPORT", t["analyst"], note="analyst tries an L2 action")
    with pytest.raises(faults.Forbidden):
        approvals.approve("HHF-001", "BLOCK_CARD", t["L1"], exposure=3000.0)         # over 2500: L2 route
    with pytest.raises(faults.Forbidden):
        approvals.approve("HHF-001", "BLOCK_CARD", "not-a-real-token-at-all")
    r1 = approvals.approve("HHF-001", "BLOCK_CARD", t["L1"], exposure=120.0)
    r2 = approvals.approve("HHF-001", "FILE_REPORT", t["L2"])
    r3 = approvals.approve("HHF-001", "CREATE_CASE", t["analyst"])
    recs = approvals.records("HHF-001")
    assert [r["status"] for r in recs] == ["denied", "denied", "denied", "approved", "approved", "approved"]
    assert recs[0]["role"] == "analyst" and recs[0]["route"] == "L2" and recs[2]["role"] == "none"
    assert r1["route"] == "L1" and r2["role"] == "L2" and r3["route"] == "auto"
    assert all(t["L2"] not in json.dumps(r) for r in recs)                        # tokens never stored
    assert recs[0]["prev_hash"] == approvals.GENESIS and recs[1]["prev_hash"] == recs[0]["hash"]
    assert approvals.verify() == (True, 6)
    final = [{"action": "BLOCK_CARD", "route": "L1"}, {"action": "DECLINE_TRANSACTION", "route": "L1"},
             {"action": "CREATE_CASE", "route": "auto"}]
    assert [p["action"] for p in approvals.pending("HHF-001", final)] == ["DECLINE_TRANSACTION"]
    assert not hasattr(approvals, "update") and not hasattr(approvals, "delete")
    with pytest.raises(ValueError):
        approvals.approve("HHF-001", "APPROVE_EVERYTHING", t["L2"])
    # tamper: flip a denied record to approved
    p = approvals.path()
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"status": "denied"', '"status": "approved"')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, n = approvals.verify()
    assert not ok and n == 0 and approvals.verify_detail()["first_bad"] == 1
    # deleting a middle line breaks the chain too
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
    assert approvals.verify()[0] is False


# ---------------------------------------------------------------- server
@pytest.fixture
def client(iso):
    from fastapi.testclient import TestClient
    from kavach.server import create_app
    cases = iso["tmp"] / "cases"
    cases.mkdir()
    app = create_app(backend="duck", cases_dir=cases, traces_dir=iso["tmp"] / "traces", data_dir=iso["tmp"] / "data")
    with TestClient(app) as c:
        c.cases_dir = cases
        yield c


def test_server_approvals_role_from_token_only(client, iso):
    t = iso["tokens"]
    r = client.post("/cases/HHF-002/approvals", json={"action": "FILE_REPORT", "role": "L2", "note": "trust me"},
                    headers={"Authorization": f"Bearer {t['analyst']}"})
    assert r.status_code == 403 and r.json()["status"] == "denied" and r.json()["ignored_fields"] == ["role"]
    rec = approvals.records("HHF-002")[-1]
    assert rec["status"] == "denied" and rec["role"] == "analyst" and "claimed role 'L2'" in rec["note"]
    assert client.post("/cases/HHF-002/approvals", json={"action": "FILE_REPORT"}).status_code == 401
    r = client.post("/cases/HHF-002/approvals", json={"action": "FILE_REPORT"}, headers={"Authorization": f"Bearer {t['L2']}"})
    assert r.status_code == 200 and r.json()["record"]["role"] == "L2" and r.json()["record"]["status"] == "approved"
    v = client.get("/approvals/verify").json()
    assert v["ok"] and v["records"] == 3
    assert client.get("/health").json()["approvals_chain"] == {"ok": True, "records": 3}
    assert client.post("/cases/HHF-002/approvals", json={"action": "NOPE"}, headers={"Authorization": f"Bearer {t['L2']}"}).status_code == 422


def test_server_exposure_cannot_be_understated(client, iso):
    ans = {"case": {"exposure_usd": 4000.0}, "next_best_actions": {"final": [{"action": "BLOCK_CARD", "route": "L2"}]}}
    (client.cases_dir / "HHF-007.json").write_text(json.dumps(ans), encoding="utf-8")
    r = client.post("/cases/HHF-007/approvals", json={"action": "BLOCK_CARD", "exposure": 10},
                    headers={"Authorization": f"Bearer {iso['tokens']['L1']}"})
    assert r.status_code == 403 and approvals.records("HHF-007")[-1]["route"] == "L2"
    view = client.get("/cases/HHF-007").json()
    assert view["pending_approvals"][0]["action"] == "BLOCK_CARD" and view["pending_approvals"][0]["needs_role"] == "L2"


def test_server_alert_runs_in_worker_and_writes_nothing(client, rows):
    assert client.post("/alerts", json={"case_id": "../x"}).status_code == 422
    assert client.get("/cases/..%2F..%2Fsecrets").status_code in (404, 422)
    row = {k: (None if isinstance(v, float) and v != v else v) for k, v in rows[1].items()}   # NaN is null on the wire
    r = client.post("/alerts", json=row)
    assert r.status_code == 202 and r.json() == {"case_id": rows[1]["case_id"], "status": "running"}
    for _ in range(240):
        j = client.get(f"/alerts/{rows[1]['case_id']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.25)
    assert j["status"] == "ok" and j["verdict"] in ("fraud", "legitimate", "uncertain")
    assert not list(client.cases_dir.glob("*.json"))          # the server does not persist by default
    assert client.get(f"/cases/{rows[1]['case_id']}/replay").status_code == 404


# ---------------------------------------------------------------- idempotency
def test_rerun_identical(rows):
    case = rows[2]
    a = runner.run_case(case, previous={})
    b = runner.run_case(case, previous=a.answer)
    assert a.status == b.status == "ok"
    assert runner.decisions(a.answer) == runner.decisions(b.answer)
    assert b.rerun == {"previous": "given", "identical": True, "changed": []}
