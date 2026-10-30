"""Case memory: write each investigation into TigerGraph and read it back.

InvestigationCase vertex (stable id per case) + edges to the card, the episode transactions,
the device profiles and the similar closed cases. `written_to_graph` is true only when the
vertex re-reads with the same verdict after the upsert and, on a real connection, every link
re-reads with the count the answer lists.

A re-run replaces the decision's links (CASE_OF, CASE_CARD, CASE_TXN, CASE_DEVICE, CASE_SIMILAR are deleted
before the new ones are written), so the graph never carries links of an earlier, different decision. The
case's history (CASE_EVENT, CASE_APPROVAL, CASE_DETECTOR, written by kavach.graph.writeback) is append-only
and never deleted here. Links are written with vertex_must_exist: a link to an id that is not on the graph
is refused instead of creating an empty vertex, and the read-back then reports the shortfall.
"""
import zlib
from datetime import datetime

DECISION_LINKS = ("CASE_OF", "CASE_CARD", "CASE_TXN", "CASE_DEVICE", "CASE_SIMILAR")


def graph_case_id(case_id: str) -> str:
    n = zlib.crc32(case_id.encode()) % 9000 + 1000
    return f"CASE-2016-{n:04d}"


def links_for(answer: dict, card_id: str) -> dict:
    """The decision's links: {edge type: (target vertex type, [(target id, attrs)])}, deduplicated, in answer order."""
    c = answer["case"]

    def uniq(xs):
        return list(dict.fromkeys(str(x) for x in xs))
    return {
        "CASE_OF": ("Card", [(card_id, {})]),
        "CASE_CARD": ("Card", [(x, {}) for x in uniq([card_id] + list(c["connected_card_ids"]))]),
        "CASE_TXN": ("Transaction", [(x, {}) for x in uniq(c["affected_txn_ids"])]),
        "CASE_DEVICE": ("DeviceProfile", [(x, {}) for x in uniq(c["connected_device_profiles"])]),
        "CASE_SIMILAR": ("ClosedCase", [(x, {"score": 1.0}) for x in uniq(c["similar_prior_cases"])]),
    }


def _write_links(conn, gid: str, links: dict) -> None:
    batched = hasattr(conn, "upsertEdges")
    for etype, (ttype, targets) in links.items():
        if not targets:
            continue
        if batched:
            conn.upsertEdges("InvestigationCase", etype, ttype, [(gid, t, a) for t, a in targets], vertexMustExist=True)
        else:
            for t, a in targets:
                conn.upsertEdge("InvestigationCase", gid, etype, ttype, t, a or None)


def _link_counts(conn, gid: str, links: dict) -> dict | None:
    """{edge type: (on graph, expected)} on a connection that can count edges from a vertex; None otherwise."""
    if not hasattr(conn, "getEdgeCountFrom"):
        return None
    out = {}
    for etype, (_, targets) in links.items():
        got = conn.getEdgeCountFrom("InvestigationCase", gid, etype)
        n = got.get(etype, 0) if isinstance(got, dict) else int(got or 0)
        out[etype] = (int(n), len(targets))
    return out


def write_case(conn, answer: dict, card_id: str) -> tuple[bool, str]:
    c = answer["case"]
    gid = graph_case_id(answer["case_id"])
    conn.upsertVertex("InvestigationCase", gid, {
        "case_id": answer["case_id"], "status": c["status"], "verdict": c["verdict"], "p": c["fraud_probability"],
        "pattern": c["pattern"], "exposure_usd": c["exposure_usd"], "summary": c["summary"][:4000],
        "sar_filed": answer["sar"]["file"], "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    if hasattr(conn, "delEdges"):
        for etype in DECISION_LINKS:
            conn.delEdges("InvestigationCase", gid, etype)
    links = links_for(answer, card_id)
    _write_links(conn, gid, links)
    try:
        from kavach.graph.vectors_tg import upsert_investigation_vector
        upsert_investigation_vector(conn, gid, c["summary"])
    except Exception as e:  # the case vertex still counts as written; the embedding is a bonus for recall
        print(f"  embedding upsert failed for {gid}: {str(e)[:120]}")
    back = conn.getVerticesById("InvestigationCase", gid)
    ok = bool(back) and back[0]["attributes"].get("verdict") == c["verdict"] and back[0]["attributes"].get("case_id") == answer["case_id"]
    counts = _link_counts(conn, gid, links) if ok else None
    if counts is not None:
        short = {e: v for e, v in counts.items() if v[0] != v[1]}
        if short:
            print(f"  {gid}: links on the graph differ from the answer {short}; not marked as written")
            ok = False
    return ok, gid if ok else ""


def prior_investigations(conn, card_ids: list[str]) -> list[dict]:
    """Earlier InvestigationCases touching these cards (the case memory the next investigation retrieves)."""
    out = []
    for cid in card_ids:
        try:
            r = conn.runInstalledQuery("case_memory", params={"c": cid})
            out += [{"id": v["v_id"], **v["attributes"]} for v in r[0]["I"]]
        except Exception:
            pass
    return out
