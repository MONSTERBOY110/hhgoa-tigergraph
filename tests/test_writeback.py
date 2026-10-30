"""Case-life write-back (kavach/graph/writeback.py) and the decision links of kavach/persist.py, against an in-memory
stand-in for the pyTigerGraph connection that honours the REST semantics the code relies on: new_vertex_only (an
existing vertex is never updated), vertex_must_exist (an edge never creates a vertex), and the installed read-back
queries case_life / detector_life."""
import json

import pytest

from kavach import casefile, persist
from kavach.graph import writeback as wb


class FakeTG:
    def __init__(self):
        self.v = {}          # (type, id) -> attrs
        self.e = set()       # (stype, sid, etype, ttype, tid)
        self.calls = []

    # ---- REST
    def upsertData(self, data, newVertexOnly=False, **kw):
        n = 0
        for vtype, rows in data["vertices"].items():
            for vid, attrs in rows.items():
                if newVertexOnly and (vtype, vid) in self.v:
                    continue
                self.v.setdefault((vtype, vid), {}).update({k: x["value"] for k, x in attrs.items()})
                n += 1
        return {"accepted_vertices": n, "accepted_edges": 0}

    def upsertVertex(self, vtype, vid, attrs):
        self.v.setdefault((vtype, vid), {}).update(attrs)

    def upsertEdges(self, st, et, tt, edges, vertexMustExist=False, **kw):
        n = 0
        for s, t, _ in edges:
            if vertexMustExist and ((st, s) not in self.v or (tt, t) not in self.v):
                continue
            self.e.add((st, s, et, tt, t))
            n += 1
        return n

    def delEdges(self, st, sid, et, **kw):
        gone = {x for x in self.e if x[0] == st and x[1] == sid and x[2] == et}
        self.e -= gone
        return {et: len(gone)}

    def getEdgeCountFrom(self, st, sid, et, **kw):
        return {et: sum(1 for x in self.e if x[0] == st and x[1] == sid and x[2] == et)}

    def getVerticesById(self, vtype, ids):
        ids = ids if isinstance(ids, list) else [ids]
        out = []
        for i in ids:
            if (vtype, i) not in self.v:
                raise RuntimeError("REST-30000 601 The input vertex id is not a valid vertex id")
            out.append({"v_id": i, "attributes": dict(self.v[(vtype, i)])})
        return out

    # ---- installed queries
    def _out(self, st, sid, et):
        return [x[4] for x in self.e if x[0] == st and x[1] == sid and x[2] == et]

    def runInstalledQuery(self, name, params=None, **kw):
        self.calls.append(name)
        if name == "case_life":
            gids = [g for g in params["gids"] if ("InvestigationCase", g) in self.v]
            ev = [{"v_id": t, "attributes": dict(self.v[("CaseEvent", t)])} for g in gids for t in self._out("InvestigationCase", g, "CASE_EVENT")]
            ap = [{"v_id": t, "attributes": dict(self.v[("Approval", t)])} for g in gids for t in self._out("InvestigationCase", g, "CASE_APPROVAL")]
            dt = [{"v_id": t, "attributes": {}} for g in gids for t in self._out("InvestigationCase", g, "CASE_DETECTOR")]
            cs = [{"v_id": g, "attributes": {"S.case_id": self.v[("InvestigationCase", g)].get("case_id")}} for g in gids]
            return [{"cases": cs}, {"events": ev}, {"approvals": ap}, {"detectors": dt}]
        if name == "detector_life":
            d = params["names"][0]
            det = [{"v_id": d, "attributes": dict(self.v[("Detector", d)])}] if ("Detector", d) in self.v else []
            aps = [{"v_id": x[1]} for x in self.e if x[2] == "APPROVED_DETECTOR" and x[4] == d]
            cs = [{"v_id": x[1], "attributes": {"CS.case_id": self.v[("InvestigationCase", x[1])]["case_id"]}}
                  for x in self.e if x[2] == "CASE_DETECTOR" and x[4] == d]
            hits = [{"v_id": t} for t in self._out("Detector", d, "DETECTOR_HIT")]
            return [{"detectors": det}, {"approvals": aps}, {"cases": cs}, {"hits": hits}]
        raise KeyError(name)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("KAVACH_CASEFILE_DIR", str(tmp_path / "casefiles"))
    monkeypatch.setenv("KAVACH_APPROVALS_FILE", str(tmp_path / "approvals.jsonl"))
    g = FakeTG()
    for i in range(1, 6):
        g.v[("Transaction", str(i))] = {}
    g.v[("Card", "C00001-K1")] = {}
    g.v[("ClosedCase", "CC-0001")] = {}
    return g


def _case_on_graph(g, case_id="HHF-901"):
    g.v[("InvestigationCase", persist.graph_case_id(case_id))] = {"case_id": case_id, "verdict": "fraud"}


def _events(case_id="HHF-901"):
    casefile.append(case_id, "opened", "alert landed")
    casefile.append(case_id, "failed", "tool_failure: MCP failed: ('Runtime Error: Parameter devices is NULL.', None)")
    casefile.append(case_id, "actions_final", "BLOCK_CARD (L1)", evidence_ids=["1", "2"])
    return casefile.events(case_id)


# ------------------------------------------------------------------------------------------------------------- pure
def test_graph_ts_and_event_vertex():
    assert wb.graph_ts("2026-10-29T12:19:33.302387Z") == "2026-10-29 12:19:33"
    assert wb.graph_ts("2026-10-29T17:40:00+05:30") == "2026-10-29 12:10:00"
    ev = {"seq": 7, "case_id": "HHF-901", "kind": "failed", "ts": "2026-10-29T12:00:00Z", "actor": "agent",
          "detail": "x Runtime Error: Parameter devices is NULL.", "evidence_ids": [1, 2], "approval_id": None,
          "hash": "h7", "prev_hash": "h6"}
    vid, a = wb.event_vertex(ev)
    d = json.loads(a["detail"])
    assert vid == "HHF-901-E0007" and a["ts"] == "2026-10-29 12:00:00" and d["hash"] == "h7" and d["prev_hash"] == "h6"
    assert "Runtime Error:" not in a["detail"] and "Runtime Error -" in d["detail"] and d["evidence_ids"] == ["1", "2"]


def test_chain_and_digest():
    assert wb.chain_ok([("", "a"), ("a", "b"), ("b", "c")]) and not wb.chain_ok([("", "a"), ("x", "b")])
    assert wb.digest([("1", {"a": 1}), ("2", {"b": 2})]) == wb.digest([("2", {"b": 2}), ("1", {"a": 1})])


# ------------------------------------------------------------------------------------------------------------- events
def test_sync_case_insert_only_idempotent_and_verified(iso):
    g = iso
    evs = _events()
    assert wb.sync_case(g, "HHF-901")["status"] == "skipped"            # no InvestigationCase yet: nothing invented
    assert not any(k[0] == "CaseEvent" for k in g.v)
    _case_on_graph(g)
    r = wb.sync_case(g, "HHF-901")
    assert (r["inserted_vertices"], r["inserted_edges"]) == (3, 3)
    assert wb.verify_case(g, "HHF-901")["ok"]
    casefile.append("HHF-901", "closed", "closed_fraud")                  # the case moves on: only the new event goes in
    r2 = wb.sync_case(g, "HHF-901")
    assert (r2["already"], r2["inserted_vertices"], r2["inserted_edges"]) == (3, 1, 1)
    v = wb.verify_case(g, "HHF-901")
    assert v["ok"] and v["local"] == v["graph"] == 4 and v["chain_ok_on_graph"] and v["digest_local"] == v["digest_graph"]
    assert len(evs) == 3


def test_sync_case_refuses_to_overwrite_a_different_event(iso):
    g = iso
    _events()
    _case_on_graph(g)
    wb.sync_case(g, "HHF-901")
    key = ("CaseEvent", "HHF-901-E0002")
    d = json.loads(g.v[key]["detail"])
    d["hash"] = "tampered"
    g.v[key]["detail"] = json.dumps(d)
    with pytest.raises(wb.WritebackError, match="different hash"):
        wb.sync_case(g, "HHF-901")
    assert wb.verify_case(g, "HHF-901")["ok"] is False


# ------------------------------------------------------------------------------------------------------------- approvals
def test_sync_approvals_insert_only_with_case_edges(iso, monkeypatch):
    from kavach.harness import approvals
    g = iso
    _case_on_graph(g)
    monkeypatch.setattr(approvals, "role_for", lambda tok: {"t-analyst": "analyst", "t-l2": "L2"}.get(tok))
    approvals.approve("HHF-901", "CREATE_CASE", "t-analyst")
    try:
        approvals.approve("HHF-901", "FILE_REPORT", "t-analyst")          # denied, still recorded
    except Exception:
        pass
    approvals.approve("HHF-901", "FILE_REPORT", "t-l2")
    recs = approvals.records()
    r = wb.sync_approvals(g)
    assert r["records"] == len(recs) and r["inserted_vertices"] == len(recs) and r["case_edges"] == len(recs)
    v = wb.verify_approvals(g)
    assert v["ok"] and v["chain_ok_on_graph"] and v["local_chain_ok"]
    assert wb.sync_approvals(g)["inserted_vertices"] == 0                # idempotent
    g.v[("Approval", recs[0]["id"])]["hash"] = "rewritten"
    with pytest.raises(wb.WritebackError, match="append-only"):
        wb.sync_approvals(g)


# ------------------------------------------------------------------------------------------------------------- detector
def _spec():
    return {"name": "fanin_test", "family": "fanin", "status": "approved", "params": {"entity": "R_emaildomain"},
            "rule": "r", "description": "d", "spec_sha": "abc", "backtest": {"lift": 18.0, "support": 8},
            "approval": {"id": "ap-1", "role": "L2", "ts": "2026-10-29T12:30:09+00:00"}}


def test_sync_detector_hits_cases_and_verify(iso):
    g = iso
    _case_on_graph(g)
    g.v[("Approval", "ap-1")] = {"hash": "x"}
    hits = {"backtest": ["1", "2"], "forward": ["3", "99"]}               # 99 is not on the graph: refused, not invented
    r = wb.sync_detector(g, _spec(), hits=hits, cases={"HHF-901": ["1"], "HHF-999": []})
    assert r["approval_edge"] == 1 and r["hit_edges"] == 3 and r["cases"] == ["HHF-901"] and r["cases_not_on_graph"] == ["HHF-999"]
    assert ("Transaction", "99") not in g.v
    v = wb.verify_detector(g, _spec(), {"backtest": ["1", "2"], "forward": ["3"]}, ["HHF-901"])
    assert v["ok"] and v["spec_sha_equal"] and v["approvals"] == ["ap-1"]
    other = dict(_spec(), spec_sha="changed")
    with pytest.raises(wb.WritebackError, match="another spec hash"):
        wb.sync_detector(g, other, hits=hits, cases={})


def test_unapproved_detector_is_never_written(iso):
    s = dict(_spec(), status="proposed", approval={})
    with pytest.raises(wb.WritebackError, match="no L2 approval"):
        wb.sync_detector(iso, s, hits={}, cases={})
    s = dict(_spec(), approval={"id": "ap-1", "role": "analyst", "ts": "2026-10-29T12:30:09+00:00"})
    with pytest.raises(wb.WritebackError, match="no L2 approval"):
        wb.sync_detector(iso, s, hits={}, cases={})
    assert not any(k[0] == "Detector" for k in iso.v)


# ------------------------------------------------------------------------------------------------------------- persist links
def _answer(txns, similar=("CC-0001",)):
    return {"case_id": "HHF-902", "case": {"status": "escalated", "verdict": "fraud", "fraud_probability": 0.9,
                                           "pattern": "undocumented", "exposure_usd": 10.0, "summary": "s",
                                           "connected_card_ids": [], "affected_txn_ids": list(txns),
                                           "connected_device_profiles": [], "similar_prior_cases": list(similar)},
            "sar": {"file": True}}


def test_persist_replaces_stale_links_and_refuses_unknown_ids(iso, monkeypatch):
    import kavach.graph.vectors_tg as vtg
    monkeypatch.setattr(vtg, "upsert_investigation_vector", lambda conn, gid, summary: None)
    g = iso
    gid = persist.graph_case_id("HHF-902")
    ok, got = persist.write_case(g, _answer(["1", "2", "3"]), "C00001-K1")
    assert ok and got == gid and persist._link_counts(g, gid, persist.links_for(_answer(["1", "2", "3"]), "C00001-K1"))["CASE_TXN"] == (3, 3)
    ok, _ = persist.write_case(g, _answer(["4"]), "C00001-K1")            # a re-run with another episode
    assert ok and sorted(x[4] for x in g.e if x[2] == "CASE_TXN") == ["4"]
    g.e.add(("InvestigationCase", gid, "CASE_EVENT", "CaseEvent", "keep-me"))
    ok, got = persist.write_case(g, _answer(["4", "77"]), "C00001-K1")    # 77 is not on the graph
    assert not ok and got == ""
    assert ("Transaction", "77") not in g.v
    assert ("InvestigationCase", gid, "CASE_EVENT", "CaseEvent", "keep-me") in g.e   # history is never deleted
