"""`python -m kavach graph schema|load|queries|counts|all`."""
import re
import time

from kavach.config import ROOT, env
from kavach.graph import loading
from kavach.graph.client import connect

GDIR = ROOT / "kavach" / "graph"


def _gsql(conn, text: str) -> str:
    out = conn.gsql(text)
    print(str(out)[-1500:])
    return str(out)


def schema(conn) -> None:
    graph = env("TG_GRAPH", "Fraud")
    body = (GDIR / "schema.gsql").read_text(encoding="utf-8")
    body = "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))
    body = body.replace("CREATE GRAPH Fraud (*)", f"CREATE GRAPH {graph} (*)")
    _gsql(conn, "USE GLOBAL\n" + body)


def load_job(conn) -> None:
    graph = env("TG_GRAPH", "Fraud")
    _gsql(conn, f"USE GRAPH {graph}\nDROP JOB load_kavach\n")
    _gsql(conn, f"USE GRAPH {graph}\n" + loading.job_gsql(graph))


def queries(conn) -> None:
    graph = env("TG_GRAPH", "Fraud")
    text = (GDIR / "queries.gsql").read_text(encoding="utf-8")
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))
    blocks = re.split(r"(?=CREATE OR REPLACE QUERY)", text)
    names = []
    for b in blocks:
        if not b.strip():
            continue
        names.append(re.search(r"QUERY (\w+)", b).group(1))
        _gsql(conn, f"USE GRAPH {graph}\n{b.replace('FOR GRAPH Fraud', f'FOR GRAPH {graph}')}")
    t0 = time.time()
    _gsql(conn, f"USE GRAPH {graph}\nINSTALL QUERY {', '.join(names)}")
    print(f"installed {len(names)} queries in {time.time() - t0:.0f}s")


def counts(conn) -> dict:
    out = {}
    for v in ("Customer", "Card", "Holder", "Transaction", "DeviceProfile", "EmailDomain", "BillingRegion", "ClosedCase",
              "InvestigationCase", "PolicyDoc"):
        out[v] = conn.getVertexCount(v)
    for e in ("OWNS", "MADE", "FROM_DEVICE", "PURCHASER_EMAIL", "RECIPIENT_EMAIL", "BILLED_IN", "NEXT_TXN", "HOLDER_CARD",
              "INVOLVES", "ON_CARD", "CONNECTED_TO"):
        out[e] = conn.getEdgeCount(e)
    for k, v in out.items():
        print(f"{k:18s} {v:>10,}")
    return out


def run(step: str) -> None:
    conn = connect()
    if step in ("schema", "all"):
        schema(conn)
        conn = connect()
    if step in ("load", "all"):
        if not (loading.TG_DIR / "txn.csv").exists():
            loading.export()
        load_job(conn)
        loading.load(conn, env("TG_GRAPH", "Fraud"))
    if step in ("queries", "all"):
        queries(conn)
    if step in ("counts", "all"):
        counts(conn)
