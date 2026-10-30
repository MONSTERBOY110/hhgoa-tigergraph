"""Drill: the same case twice on the TigerGraph lane with write-back (--persist).

Usage: python logs/drills/tg_twice.py <scratch_dir> [case_id]
Runs `kavach investigate <case> --backend tg --persist --no-llm` twice through logs/drills/kv_scratch.py (answer,
trace, sidecar and case file go to <scratch_dir>), counts InvestigationCase vertices and the case vertex's edges before
and after, then restores the vertex from the production answer in cases/<case>.json (so the graph keeps the submitted
summary, not the drill's template prose)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import kavach.config  # noqa: E402,F401
from kavach import duckq  # noqa: E402
from kavach.graph.client import connect  # noqa: E402
from kavach.persist import graph_case_id, write_case  # noqa: E402

scratch = Path(sys.argv[1]).resolve()
cid = sys.argv[2] if len(sys.argv) > 2 else "HHF-007"
gid = graph_case_id(cid)
conn = connect()
EDGES = ("CASE_OF", "CASE_CARD", "CASE_TXN", "CASE_DEVICE", "CASE_SIMILAR")


def snapshot(label):
    n = conn.getVertexCount("InvestigationCase")
    v = conn.getVerticesById("InvestigationCase", gid)
    a = v[0]["attributes"] if v else {}
    e = {t: conn.getEdgeCountFrom("InvestigationCase", gid, t) for t in EDGES} if v else {}
    print(f"{label}: InvestigationCase vertices {n}; {gid} exists {bool(v)} verdict {a.get('verdict')} created_at "
          f"{a.get('created_at')} edges {e}", flush=True)
    return n, e, a


n0, e0, a0 = snapshot("before")
outs = []
for i in (1, 2):
    r = subprocess.run([sys.executable, str(ROOT / "logs/drills/kv_scratch.py"), str(scratch), "investigate", cid, "--backend", "tg",
                        "--persist", "--no-llm"], capture_output=True, text=True, encoding="utf-8", timeout=300)
    print(f"run {i} (exit {r.returncode}): {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-400:]}", flush=True)
    outs.append(r.returncode)
    snapshot(f"after run {i}")
ans = json.loads((scratch / "cases" / f"{cid}.json").read_text(encoding="utf-8"))
print("scratch answer: written_to_graph", ans["case"]["written_to_graph"], "graph_case_id", ans["case"]["graph_case_id"])
ev = [json.loads(ln) for ln in (scratch / "casefiles" / f"{cid}.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
print("case file events:", [e["kind"] for e in ev if e["kind"] in ("opened", "rerun", "written_to_graph")])
print("rerun comparison:", [e["detail"] for e in ev if e["kind"] == "rerun" and e["detail"].startswith("same case")])
# restore the production vertex (the submitted answer's summary and fields)
prod = json.loads((ROOT / "cases" / f"{cid}.json").read_text(encoding="utf-8"))
row = next(r for r in duckq.case_pack().to_dict("records") if r["case_id"] == cid)
ok, g = write_case(conn, prod, str(row["card_id"]))
back = conn.getVerticesById("InvestigationCase", gid)[0]["attributes"]
print(f"restored {g} from cases/{cid}.json: read back ok {ok}, summary matches the submitted answer "
      f"{back.get('summary') == prod['case']['summary'][:4000]}")
snapshot("after restore")
