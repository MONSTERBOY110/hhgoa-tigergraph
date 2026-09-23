"""TigerVector: embeddings of closed-case notes, policy/pattern sections and investigation summaries inside the graph.

Adds 384-d cosine vector attributes, upserts the vectors built by kavach.vectors, installs vector-search
queries, and exposes the same search functions as the local lane. If the server rejects vectors, the
agent keeps the local index (same embeddings) and still links results via CASE_SIMILAR edges.
"""
import numpy as np

from kavach.config import env
from kavach.vectors import embed, index

SCHEMA = """CREATE GLOBAL SCHEMA_CHANGE JOB kavach_vectors {
  ALTER VERTEX ClosedCase ADD VECTOR ATTRIBUTE emb(DIMENSION=384, METRIC="COSINE");
  ALTER VERTEX PolicyDoc ADD VECTOR ATTRIBUTE emb(DIMENSION=384, METRIC="COSINE");
  ALTER VERTEX InvestigationCase ADD VECTOR ATTRIBUTE emb(DIMENSION=384, METRIC="COSINE");
}
RUN GLOBAL SCHEMA_CHANGE JOB kavach_vectors"""

QUERIES = """CREATE OR REPLACE QUERY similar_closed(LIST<FLOAT> qv, INT k) FOR GRAPH {g} SYNTAX v3 {{
  MapAccum<VERTEX, FLOAT> @@dist;
  R = vectorSearch({{ClosedCase.emb}}, qv, k, {{distance_map: @@dist}});
  PRINT R[R.outcome, R.pattern] AS hits;
  PRINT @@dist AS dist;
}}
CREATE OR REPLACE QUERY similar_docs(LIST<FLOAT> qv, INT k) FOR GRAPH {g} SYNTAX v3 {{
  MapAccum<VERTEX, FLOAT> @@dist;
  R = vectorSearch({{PolicyDoc.emb}}, qv, k, {{distance_map: @@dist}});
  PRINT R[R.section, R.text] AS hits;
  PRINT @@dist AS dist;
}}
CREATE OR REPLACE QUERY similar_investigations(LIST<FLOAT> qv, INT k) FOR GRAPH {g} SYNTAX v3 {{
  MapAccum<VERTEX, FLOAT> @@dist;
  R = vectorSearch({{InvestigationCase.emb}}, qv, k, {{distance_map: @@dist}});
  PRINT R[R.case_id, R.verdict, R.pattern] AS hits;
  PRINT @@dist AS dist;
}}
INSTALL QUERY similar_closed, similar_docs, similar_investigations"""


def setup(conn) -> None:
    g = env("TG_GRAPH", "Fraud")
    # the vertex types are global (created before CREATE GRAPH), so the change is a global schema change job
    print(conn.gsql("USE GLOBAL\n" + SCHEMA)[-800:])
    print(conn.gsql(f"USE GRAPH {g}\n" + QUERIES.format(g=g))[-800:])


def upload(conn, batch: int = 500) -> dict:
    """Upsert ClosedCase and PolicyDoc embeddings (PolicyDoc vertices are created here with their text)."""
    ix = index()
    n = 0
    ids, vecs = ix["case_ids"], ix["case_vecs"]
    for i in range(0, len(ids), batch):
        rows = [(str(c), {"emb": [round(float(x), 6) for x in v]}) for c, v in zip(ids[i:i + batch], vecs[i:i + batch])]
        n += conn.upsertVertices("ClosedCase", rows)
    docs = [(str(d), {"source": "organizer README", "section": str(s), "text": str(t)[:4000],
                      "emb": [round(float(x), 6) for x in v]})
            for d, s, t, v in zip(ix["doc_ids"], ix["doc_sections"], ix["doc_texts"], ix["doc_vecs"])]
    m = conn.upsertVertices("PolicyDoc", docs)
    print(f"upserted {n} ClosedCase vectors and {m} PolicyDoc vertices with vectors")
    return {"closed": n, "docs": m}


def upsert_investigation_vector(conn, gid: str, summary: str) -> None:
    v = embed([summary])[0]
    conn.upsertVertex("InvestigationCase", gid, {"emb": [round(float(x), 6) for x in v]})


def _hits(res, attrs) -> list:
    hits = next((r["hits"] for r in res if "hits" in r), [])
    dist = next((r["dist"] for r in res if "dist" in r), {})
    out = []
    for h in hits:
        d = dist.get(h["v_id"], dist.get(str(h["v_id"]), None))
        out.append({"id": h["v_id"], "score": round(1 - float(d), 4) if d is not None else None,
                    **{a: h["attributes"].get(f"R.{a}", h["attributes"].get(a)) for a in attrs}})
    return sorted(out, key=lambda x: -(x["score"] or 0))


def search_closed(conn, text: str, k: int = 8) -> list[tuple[str, float]]:
    q = [round(float(x), 6) for x in embed([text])[0]]
    return [(h["id"], h["score"]) for h in _hits(conn.runInstalledQuery("similar_closed", params={"qv": q, "k": k}), ["outcome"])]


def search_docs(conn, text: str, k: int = 2) -> list[dict]:
    q = [round(float(x), 6) for x in embed([text])[0]]
    return _hits(conn.runInstalledQuery("similar_docs", params={"qv": q, "k": k}), ["section", "text"])


def search_investigations(conn, text: str, k: int = 3) -> list[dict]:
    q = [round(float(x), 6) for x in embed([text])[0]]
    return _hits(conn.runInstalledQuery("similar_investigations", params={"qv": q, "k": k}), ["case_id", "verdict", "pattern"])


def self_check(conn) -> bool:
    """A known closed case's own note must come back as its nearest neighbour."""
    ix = index()
    cid = str(ix["case_ids"][0])
    q = [round(float(x), 6) for x in np.asarray(ix["case_vecs"][0])]
    top = _hits(conn.runInstalledQuery("similar_closed", params={"qv": q, "k": 3}), ["outcome"])
    ok = bool(top) and top[0]["id"] == cid
    print(f"TigerVector self-check: nearest to {cid} is {top[0]['id'] if top else None} -> {'ok' if ok else 'FAIL'}")
    return ok
