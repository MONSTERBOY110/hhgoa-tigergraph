"""Write the life of a case into TigerGraph, then read it back and verify it.

    sync_case(conn, case_id)        one CaseEvent per line of data/casefiles/<case>.jsonl, CASE_EVENT from the case's
                                    InvestigationCase (written by kavach.persist.write_case)
    sync_approvals(conn)            one Approval per line of data/approvals.jsonl, CASE_APPROVAL from the case it decides
    sync_detector(conn, name)       an approved grown detector as a Detector vertex: APPROVED_DETECTOR from its L2 Approval,
                                    DETECTOR_HIT to every transaction it fires on (closed-case transactions = its backtest,
                                    January = its forward run), CASE_DETECTOR from each investigation it fired in
    verify_case / verify_approvals / verify_detector   read back through the installed queries case_life and detector_life
                                    (queries_fusion.gsql) and REST, compare counts and hashes

Rules: insert-only. CaseEvent and Approval vertices are written with the REST upsert flag new_vertex_only, so an existing
vertex is never updated; a vertex already on the graph with a different hash is a conflict and stops the sync (fail
closed, nothing is overwritten). Edges are written with vertex_must_exist, so an edge never creates a stub vertex: a case
whose InvestigationCase is not on the graph yet is reported as skipped, not invented. Every write is batched.

CLI: python -m kavach.graph.writeback all [--cases HHF-001,HHF-006] | verify | plan     (plan = what would be written)
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BATCH = 400
DETAIL_MAX = 1500
# TigerGraph RESTPP answers HTTP 400 (with a complete, error-free body) when a query result contains its own runtime-error
# marker, e.g. a failed event that quotes "Runtime Error: Parameter devices is NULL." (docs/tigergraph-feedback.md). The
# graph copy of free text writes the marker with a dash; the chain hash is the local file's, so verification is unaffected.
_TG_MARKERS = (("Runtime Error:", "Runtime Error -"),)


def graph_text(s: str) -> str:
    for a, b in _TG_MARKERS:
        s = s.replace(a, b)
    return s


class WritebackError(RuntimeError):
    """A write-back step failed or found a conflict; nothing after it ran."""


# ---------------------------------------------------------------------------------------------------------------
# pure mapping (unit-tested offline)

def graph_ts(ts: str) -> str:
    """ISO 8601 (with Z, an offset or fractional seconds) -> 'YYYY-MM-DD HH:MM:SS' in UTC for a DATETIME attribute."""
    s = str(ts).strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def event_id(ev: dict) -> str:
    return f"{ev['case_id']}-E{int(ev['seq']):04d}"


def event_vertex(ev: dict) -> tuple[str, dict]:
    """A casefile event -> (CaseEvent id, attributes). The detail attribute is JSON carrying the event's chain hash, its
    predecessor's hash, the sequence number, the evidence ids and the (truncated) text, so the chain can be checked on
    the graph and against the local file."""
    detail = {"seq": int(ev["seq"]), "hash": ev.get("hash", ""), "prev_hash": ev.get("prev_hash", ""),
              "detail": graph_text(str(ev.get("detail", "")))[:DETAIL_MAX], "evidence_ids": [str(x) for x in ev.get("evidence_ids") or []][:40],
              "approval_id": ev.get("approval_id")}
    return event_id(ev), {"case_id": ev["case_id"], "kind": ev["kind"], "ts": graph_ts(ev["ts"]),
                          "actor": str(ev.get("actor") or "agent"), "detail": json.dumps(detail, ensure_ascii=False, sort_keys=True)}


def approval_vertex(rec: dict) -> tuple[str, dict]:
    return str(rec["id"]), {"case_id": str(rec.get("case_id", "")), "action": str(rec.get("action", "")),
                            "route": str(rec.get("route", "")), "role": str(rec.get("role", "")),
                            "status": str(rec.get("status", "")), "ts": graph_ts(rec["ts"]),
                            "prev_hash": str(rec.get("prev_hash", "")), "hash": str(rec.get("hash", ""))}


def detector_vertex(spec: dict) -> tuple[str, dict]:
    bt = spec.get("backtest") or {}
    if isinstance(bt, str):
        bt = {}
    ap = spec.get("approval") or {}
    params = {"params": spec.get("params"), "rule": spec.get("rule", ""), "spec_sha": spec.get("spec_sha", ""),
              "approval_id": ap.get("id", "")}
    return str(spec["name"]), {"family": str(spec.get("family", "")), "status": str(spec.get("status", "")),
                               "params": json.dumps(params, sort_keys=True, default=str),
                               "lift": float(bt.get("lift") or 0.0), "support": int(bt.get("support") or 0),
                               "description": graph_text(str(spec.get("description", "")))[:2000],
                               "approved_ts": graph_ts(ap["ts"]) if ap.get("ts") else "1970-01-01 00:00:00"}


def upsert_payload(vtype: str, rows: list[tuple[str, dict]]) -> dict:
    return {"vertices": {vtype: {vid: {k: {"value": v} for k, v in attrs.items()} for vid, attrs in rows}}}


def digest(rows) -> str:
    """Order-free sha256 over (id, attributes) pairs: the same set of vertices hashes the same on both sides."""
    items = sorted(json.dumps([vid, attrs], sort_keys=True, default=str) for vid, attrs in rows)
    return hashlib.sha256("\n".join(items).encode()).hexdigest()


def chain_ok(hashes: list[tuple[str, str]], genesis: str = "") -> bool:
    """[(prev_hash, hash)] in order: each prev_hash is the previous hash (the first one is the genesis)."""
    prev = genesis
    for p, h in hashes:
        if p != prev:
            return False
        prev = h
    return True


def _chunks(xs: list, n: int = BATCH):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# ---------------------------------------------------------------------------------------------------------------
# graph access

def _graph_case_id(case_id: str) -> str:
    from kavach.persist import graph_case_id
    return graph_case_id(case_id)


def _q(conn, name: str, **params):
    return conn.runInstalledQuery(name, params=params, timeout=120_000, usePost=True)


def _part(res: list, key: str, default=None):
    return next((r[key] for r in res if key in r), default)


def case_life(conn, case_ids: list[str]) -> dict:
    """Read back the life of cases from the graph: {case_id: {"exists", "events", "approvals", "detectors"}}."""
    gids = {_graph_case_id(c): c for c in case_ids}
    res = _q(conn, "case_life", gids=sorted(gids) or [""])
    present = {v["v_id"] for v in _part(res, "cases", [])}
    out = {c: {"exists": g in present, "events": [], "approvals": [], "detectors": []} for g, c in gids.items()}
    for v in _part(res, "events", []):
        a = v["attributes"]
        if a.get("case_id") in out:
            out[a["case_id"]]["events"].append({"id": v["v_id"], **a})
    for v in _part(res, "approvals", []):
        a = v["attributes"]
        if a.get("case_id") in out:
            out[a["case_id"]]["approvals"].append({"id": v["v_id"], **a})
    for v in _part(res, "detectors", []):
        for c in out:
            out[c]["detectors"].append(v["v_id"])
    return out


def _existing_vertices(conn, vtype: str, ids: list[str]) -> dict:
    """{id: attributes} for the ids already on the graph (missing ids are simply absent)."""
    out = {}
    for vid in ids:
        try:
            got = conn.getVerticesById(vtype, [vid])
        except Exception as e:  # noqa: BLE001  (REST 601: not a valid vertex id -> absent; anything else fails closed)
            if "601" in str(e) or "not a valid vertex" in str(e).lower() or "not found" in str(e).lower():
                continue
            raise WritebackError(f"reading {vtype} {vid} failed: {str(e)[:200]}") from e
        for v in got or []:
            out[v["v_id"]] = v.get("attributes", {})
    return out


def _insert_vertices(conn, vtype: str, rows: list[tuple[str, dict]]) -> int:
    n = 0
    for part in _chunks(rows):
        res = conn.upsertData(upsert_payload(vtype, part), newVertexOnly=True)
        n += int((res or {}).get("accepted_vertices", len(part))) if isinstance(res, dict) else len(part)
    return n


def _insert_edges(conn, stype: str, etype: str, ttype: str, edges: list[tuple[str, str]]) -> int:
    n = 0
    for part in _chunks(edges):
        n += int(conn.upsertEdges(stype, etype, ttype, [(s, t, {}) for s, t in part], vertexMustExist=True) or 0)
    return n


# ---------------------------------------------------------------------------------------------------------------
# case events

def sync_case(conn, case_id: str, events: list[dict] | None = None) -> dict:
    """CaseEvents of one case (insert-only) and their CASE_EVENT edges. Skips (status "skipped") when the case's
    InvestigationCase is not on the graph; raises WritebackError on a hash conflict or a failed write."""
    from kavach import casefile
    evs = events if events is not None else casefile.events(case_id)
    life = case_life(conn, [case_id])[case_id]
    gid = _graph_case_id(case_id)
    if not life["exists"]:
        return {"case_id": case_id, "status": "skipped", "reason": f"InvestigationCase {gid} not on the graph (run with --persist)",
                "local_events": len(evs)}
    on_graph = {e["id"]: e for e in life["events"]}
    rows = [event_vertex(e) for e in evs]
    new, conflicts = [], []
    for vid, attrs in rows:
        g = on_graph.get(vid)
        if g is None:
            new.append((vid, attrs))
        elif json.loads(g.get("detail") or "{}").get("hash") != json.loads(attrs["detail"])["hash"]:
            conflicts.append(vid)
    if conflicts:
        raise WritebackError(f"{case_id}: {len(conflicts)} CaseEvent vertices on the graph carry a different hash than the "
                             f"local case file ({conflicts[:3]}); nothing was overwritten")
    nv = _insert_vertices(conn, "CaseEvent", new) if new else 0
    missing_edges = [vid for vid, _ in rows if vid not in on_graph]
    ne = _insert_edges(conn, "InvestigationCase", "CASE_EVENT", "CaseEvent", [(gid, vid) for vid in missing_edges]) if missing_edges else 0
    return {"case_id": case_id, "status": "written", "graph_case_id": gid, "local_events": len(evs), "already": len(on_graph),
            "inserted_vertices": nv, "inserted_edges": ne}


def verify_case(conn, case_id: str, events: list[dict] | None = None) -> dict:
    """Read the case's events back: same count, same ids, same chain hashes, the chain intact on the graph."""
    from kavach import casefile
    evs = events if events is not None else casefile.events(case_id)
    life = case_life(conn, [case_id])[case_id]
    local = dict(event_vertex(e) for e in evs)
    graph = {e["id"]: e for e in life["events"]}
    ghash = sorted(((json.loads(g["detail"])["seq"], json.loads(g["detail"])["prev_hash"], json.loads(g["detail"])["hash"])
                    for g in graph.values()))
    same_hash = all(vid in graph and json.loads(graph[vid]["detail"])["hash"] == json.loads(a["detail"])["hash"]
                    for vid, a in local.items())
    local_digest = digest([(vid, {k: a[k] for k in ("case_id", "kind", "ts", "actor", "detail")}) for vid, a in local.items()])
    graph_digest = digest([(vid, {k: g.get(k) for k in ("case_id", "kind", "ts", "actor", "detail")}) for vid, g in graph.items()
                           if vid in local])
    ok = life["exists"] and len(graph) == len(local) and set(graph) == set(local) and same_hash and \
        chain_ok([(p, h) for _, p, h in ghash]) and local_digest == graph_digest
    return {"case_id": case_id, "ok": bool(ok), "exists": life["exists"], "local": len(local), "graph": len(graph),
            "hashes_equal": same_hash, "chain_ok_on_graph": chain_ok([(p, h) for _, p, h in ghash]),
            "digest_local": local_digest[:16], "digest_graph": graph_digest[:16]}


# ---------------------------------------------------------------------------------------------------------------
# approvals

def sync_approvals(conn, records: list[dict] | None = None) -> dict:
    """Approval vertices (insert-only) for every approval record, CASE_APPROVAL from the InvestigationCase it decides
    when that case is on the graph. A record already on the graph with another hash stops the sync."""
    from kavach.harness import approvals
    recs = records if records is not None else approvals.records()
    rows = [approval_vertex(r) for r in recs]
    have = _existing_vertices(conn, "Approval", [vid for vid, _ in rows])
    conflicts = [vid for vid, a in rows if vid in have and have[vid].get("hash") != a["hash"]]
    if conflicts:
        raise WritebackError(f"{len(conflicts)} Approval vertices on the graph differ from data/approvals.jsonl ({conflicts[:3]}); "
                             "the approvals log is append-only and nothing was overwritten")
    new = [(vid, a) for vid, a in rows if vid not in have]
    nv = _insert_vertices(conn, "Approval", new) if new else 0
    cases = sorted({a["case_id"] for _, a in rows if not a["case_id"].startswith("DETECTOR-")})
    present = {c for c, life in (case_life(conn, cases).items() if cases else []) if life["exists"]}
    edges = [(_graph_case_id(a["case_id"]), vid) for vid, a in rows if a["case_id"] in present]
    ne = _insert_edges(conn, "InvestigationCase", "CASE_APPROVAL", "Approval", edges) if edges else 0
    return {"status": "written", "records": len(rows), "already": len(have), "inserted_vertices": nv, "case_edges": ne,
            "cases_not_on_graph": sorted(set(cases) - present)}


def verify_approvals(conn, records: list[dict] | None = None) -> dict:
    from kavach.harness import approvals
    recs = records if records is not None else approvals.records()
    local = dict(approval_vertex(r) for r in recs)
    graph = _existing_vertices(conn, "Approval", list(local))
    same = all(vid in graph and str(graph[vid].get("hash")) == a["hash"] and str(graph[vid].get("status")) == a["status"]
               and str(graph[vid].get("role")) == a["role"] for vid, a in local.items())
    order = list(local)                      # the log's own order
    linked = chain_ok([(str(graph[v].get("prev_hash")), str(graph[v].get("hash"))) for v in order if v in graph],
                      genesis=str(recs[0].get("prev_hash", "")) if recs else "")
    local_chain = approvals.verify_detail() if records is None else {"ok": True}
    ok = len(graph) == len(local) and same and linked and local_chain["ok"]
    return {"ok": bool(ok), "local": len(local), "graph": len(graph), "hashes_equal": same, "chain_ok_on_graph": linked,
            "local_chain_ok": local_chain["ok"]}


# ---------------------------------------------------------------------------------------------------------------
# approved grown detectors

def detector_hits(spec: dict) -> dict:
    """Transactions the detector fires on, from the same SQL its backtest used (kavach.grow.templates.fire):
    {"backtest": ids among closed-case transactions, "forward": ids among the January (grader scope) transactions}."""
    import duckdb
    from kavach.grow import templates
    con = duckdb.connect()
    try:
        templates.clear_cache()
        templates.prepare(con)
        closed = [int(x) for x in con.execute("select distinct TransactionID from src.closed_txn").df().TransactionID]
        fwd = [int(x) for x in con.execute("select TransactionID from src.grader_scope").df().TransactionID]
        fam = templates.FAMILIES[spec["family"]] if hasattr(templates, "FAMILIES") else None
        if fam is None:
            from kavach.grow.miner import FAMILIES
            fam = FAMILIES[spec["family"]]
        out = {}
        for name, ids in (("backtest", closed), ("forward", fwd)):
            f = fam.fire(con, spec["params"], ids)
            out[name] = sorted(str(int(t)) for t in f.TransactionID[f.fired])
        return out
    finally:
        templates.clear_cache()
        con.close()


def detector_cases(name: str) -> dict:
    """Investigations the detector fired in: {case_id: [evidence ids]} from the attestation sidecars (a claim whose
    signal is grown:<name>)."""
    from kavach.config import ROOT
    out = {}
    for p in sorted((ROOT / "attestations").glob("*.json")):
        try:
            side = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for c in side.get("claims", []):
            if c.get("signal") == f"grown:{name}":
                ev = c.get("evidence") or {}
                out[side.get("case_id", p.stem)] = sorted({str(x) for k in ("txn_ids", "card_ids") for x in ev.get(k, [])})
    return out


def sync_detector(conn, spec: dict, hits: dict | None = None, cases: dict | None = None) -> dict:
    """Detector vertex (insert-only: an approved spec never changes; a vertex with another spec_sha is a conflict),
    APPROVED_DETECTOR from its Approval, DETECTOR_HIT edges, CASE_DETECTOR from the cases on the graph it fired in."""
    ap = spec.get("approval") or {}
    if spec.get("status") != "approved" or ap.get("role") != "L2" or not ap.get("id"):
        raise WritebackError(f"detector {spec.get('name')} has no L2 approval; only approved detectors are written to the graph")
    vid, attrs = detector_vertex(spec)
    have = _existing_vertices(conn, "Detector", [vid])
    if vid in have and json.loads(have[vid].get("params") or "{}").get("spec_sha") != spec.get("spec_sha"):
        raise WritebackError(f"Detector {vid} on the graph has another spec hash; nothing was overwritten")
    nv = 0 if vid in have else _insert_vertices(conn, "Detector", [(vid, attrs)])
    if not _existing_vertices(conn, "Approval", [ap["id"]]):
        raise WritebackError(f"approval {ap['id']} of detector {vid} is not on the graph: run sync_approvals first")
    na = _insert_edges(conn, "Approval", "APPROVED_DETECTOR", "Detector", [(ap["id"], vid)])
    hits = hits if hits is not None else detector_hits(spec)
    ids = sorted(set(hits.get("backtest", [])) | set(hits.get("forward", [])))
    nh = _insert_edges(conn, "Detector", "DETECTOR_HIT", "Transaction", [(vid, t) for t in ids]) if ids else 0
    cases = cases if cases is not None else detector_cases(vid)
    present = {c for c, life in (case_life(conn, sorted(cases)).items() if cases else []) if life["exists"]}
    nc = _insert_edges(conn, "InvestigationCase", "CASE_DETECTOR", "Detector",
                       [(_graph_case_id(c), vid) for c in sorted(present)]) if present else 0
    return {"detector": vid, "status": "written", "inserted_vertex": nv, "approval_edge": na, "hits": len(ids),
            "hits_backtest": len(hits.get("backtest", [])), "hits_forward": len(hits.get("forward", [])), "hit_edges": nh,
            "cases": sorted(present), "case_edges": nc, "cases_not_on_graph": sorted(set(cases) - present)}


def verify_detector(conn, spec: dict, hits: dict, cases: list[str]) -> dict:
    res = _q(conn, "detector_life", names=[spec["name"]])
    det = _part(res, "detectors", [])
    aps = [v["v_id"] for v in _part(res, "approvals", [])]
    got_cases = sorted(v["attributes"].get("CS.case_id", v["attributes"].get("case_id")) for v in _part(res, "cases", []))
    got_hits = sorted(v["v_id"] for v in _part(res, "hits", []))
    want_hits = sorted(set(hits.get("backtest", [])) | set(hits.get("forward", [])))
    a = det[0]["attributes"] if det else {}
    sha_ok = bool(det) and json.loads(a.get("params") or "{}").get("spec_sha") == spec.get("spec_sha")
    ok = sha_ok and aps == [spec["approval"]["id"]] and got_hits == want_hits and got_cases == sorted(cases)
    return {"detector": spec["name"], "ok": bool(ok), "spec_sha_equal": sha_ok, "approvals": aps, "hits_graph": len(got_hits),
            "hits_local": len(want_hits), "cases_graph": got_cases, "cases_local": sorted(cases)}


# ---------------------------------------------------------------------------------------------------------------
# everything

def sync_all(conn, case_ids: list[str] | None = None, verify: bool = True) -> dict:
    """Approvals first (detectors point at them), then each case's events, then the approved detectors; then read all of
    it back. Raises WritebackError on any conflict or failed write (fail closed); `ok` is true only if every read-back
    matches."""
    from kavach import casefile
    from kavach.grow import registry
    case_ids = case_ids or sorted(p.stem for p in casefile.casefile_dir().glob("*.jsonl"))
    rep = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "approvals": sync_approvals(conn), "cases": [],
           "detectors": [], "verify": {}}
    for c in case_ids:
        rep["cases"].append(sync_case(conn, c))
    specs = registry.load_approved(verbose=False)
    det_inputs = {}
    for s in specs:
        hits, cases = detector_hits(s), detector_cases(s["name"])
        det_inputs[s["name"]] = (hits, cases)
        rep["detectors"].append(sync_detector(conn, s, hits, cases))
    if verify:
        v_cases = [verify_case(conn, c) for c in case_ids if any(x["case_id"] == c and x["status"] == "written" for x in rep["cases"])]
        v_det = []
        for s in specs:
            hits, cases = det_inputs[s["name"]]
            on = next(d for d in rep["detectors"] if d["detector"] == s["name"])["cases"]
            v_det.append(verify_detector(conn, s, hits, on))
        v_ap = verify_approvals(conn)
        rep["verify"] = {"cases": v_cases, "approvals": v_ap, "detectors": v_det,
                         "ok": all(x["ok"] for x in v_cases) and v_ap["ok"] and all(x["ok"] for x in v_det)}
    return rep


def plan(case_ids: list[str] | None = None) -> dict:
    """What a sync would write, without a graph connection."""
    from kavach import casefile
    from kavach.grow import registry
    from kavach.harness import approvals
    case_ids = case_ids or sorted(p.stem for p in casefile.casefile_dir().glob("*.jsonl"))
    return {"case_events": {c: len(casefile.events(c)) for c in case_ids}, "approvals": len(approvals.records()),
            "approvals_chain": approvals.verify_detail(), "detectors": [s["name"] for s in registry.load_approved(verbose=False)]}


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m kavach.graph.writeback")
    ap.add_argument("cmd", choices=["all", "verify", "plan"])
    ap.add_argument("--cases", default="", help="comma-separated case ids (default: every case file)")
    ns = ap.parse_args(argv)
    cases = [c.strip() for c in ns.cases.split(",") if c.strip()] or None
    if ns.cmd == "plan":
        print(json.dumps(plan(cases), indent=1, default=str))
        return 0
    from kavach.graph.client import connect
    conn = connect()
    if ns.cmd == "all":
        try:
            rep = sync_all(conn, cases)
        except WritebackError as e:
            print(f"WRITE-BACK STOPPED: {e}")
            return 2
        print(json.dumps(rep, indent=1, default=str))
        return 0 if rep["verify"].get("ok") else 1
    from kavach import casefile
    cases = cases or sorted(p.stem for p in casefile.casefile_dir().glob("*.jsonl"))
    out = {"cases": [verify_case(conn, c) for c in cases], "approvals": verify_approvals(conn)}
    print(json.dumps(out, indent=1, default=str))
    return 0 if all(x["ok"] for x in out["cases"]) and out["approvals"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(_main())
