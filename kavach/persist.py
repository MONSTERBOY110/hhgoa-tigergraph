"""Case memory: write each investigation into TigerGraph and read it back.

InvestigationCase vertex (stable id per case) + edges to the card, the episode transactions,
the device profiles and the similar closed cases. `written_to_graph` is true only when the
vertex re-reads with the same verdict after the upsert.
"""
import zlib
from datetime import datetime


def graph_case_id(case_id: str) -> str:
    n = zlib.crc32(case_id.encode()) % 9000 + 1000
    return f"CASE-2016-{n:04d}"


def write_case(conn, answer: dict, card_id: str) -> tuple[bool, str]:
    c = answer["case"]
    gid = graph_case_id(answer["case_id"])
    conn.upsertVertex("InvestigationCase", gid, {
        "case_id": answer["case_id"], "status": c["status"], "verdict": c["verdict"], "p": c["fraud_probability"],
        "pattern": c["pattern"], "exposure_usd": c["exposure_usd"], "summary": c["summary"][:4000],
        "sar_filed": answer["sar"]["file"], "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    conn.upsertEdge("InvestigationCase", gid, "CASE_OF", "Card", card_id)
    conn.upsertEdge("InvestigationCase", gid, "CASE_CARD", "Card", card_id)
    for other in c["connected_card_ids"]:
        conn.upsertEdge("InvestigationCase", gid, "CASE_CARD", "Card", other)
    for t in c["affected_txn_ids"]:
        conn.upsertEdge("InvestigationCase", gid, "CASE_TXN", "Transaction", t)
    for d in c["connected_device_profiles"]:
        conn.upsertEdge("InvestigationCase", gid, "CASE_DEVICE", "DeviceProfile", d)
    for cc in c["similar_prior_cases"]:
        conn.upsertEdge("InvestigationCase", gid, "CASE_SIMILAR", "ClosedCase", cc, {"score": 1.0})
    back = conn.getVerticesById("InvestigationCase", gid)
    ok = bool(back) and back[0]["attributes"].get("verdict") == c["verdict"]
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
