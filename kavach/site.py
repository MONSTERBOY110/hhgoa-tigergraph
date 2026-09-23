"""Build the static case viewer's data file (site/cases.js) from the answer files.

Everything shown comes from cases/*.json and monitor/*.json; transaction amounts, times and products
for the graph panel come from the organizer dataset (DuckDB lane). Written as a script assignment so
the page also works from file:// without a server.
"""
import json

import pandas as pd

from kavach.config import CASES_DIR, ROOT

SITE = ROOT / "site"


def _txn_rows(ids: list[str]) -> dict:
    if not ids:
        return {}
    from kavach import duckq
    df = duckq.q("""select TransactionID, strftime(ts, '%Y-%m-%d %H:%M') ts, TransactionAmt amt, ProductCD, channel,
                           cast(addr1 as integer) addr1, card_id from txn where TransactionID in (select unnest(?))""",
                 [int(x) for x in ids])
    return {str(r.TransactionID): {"ts": r.ts, "amt": round(float(r.amt), 2), "product": r.ProductCD, "channel": r.channel,
                                   "region": None if pd.isna(r.addr1) else int(r.addr1), "card": r.card_id} for r in df.itertuples()}


def _closed(ids: list[str]) -> dict:
    if not ids:
        return {}
    from kavach import duckq
    df = duckq.q("select case_id, outcome, pattern, exposure_usd from closed where case_id in (select unnest(?))", ids)
    return {r.case_id: {"outcome": r.outcome, "pattern": r.pattern, "exposure": float(r.exposure_usd)} for r in df.itertuples()}


def build() -> None:
    from kavach import duckq
    pack = {r["case_id"]: r for r in duckq.case_pack().to_dict("records")}
    cases, monitor = [], []
    for f in sorted(CASES_DIR.glob("HHG-*.json")):
        a = json.loads(f.read_text(encoding="utf-8"))
        c = a["case"]
        row = pack[a["case_id"]]
        flagged = str(row["flagged_txn_id"])
        a["_pack"] = {"trigger_type": row["trigger_type"], "trigger_text": row["trigger_text"], "opened_at": row["opened_at"],
                      "card_id": row["card_id"], "customer_id": row["customer_id"], "flagged_txn_id": flagged}
        a["_txns"] = _txn_rows(sorted(set(c["affected_txn_ids"]) | {flagged}))
        a["_closed"] = _closed(c["similar_prior_cases"])
        cases.append(a)
    for f in sorted((ROOT / "monitor").glob("MON-*.json")):
        a = json.loads(f.read_text(encoding="utf-8"))
        monitor.append({"case_id": a["case_id"], "sweep": a["monitor"]["sweep"], "card": a["monitor"]["card_id"],
                        "why": a["monitor"]["trigger_text"], "verdict": a["case"]["verdict"], "p": a["case"]["fraud_probability"],
                        "pattern": a["case"]["pattern"], "exposure": a["case"]["exposure_usd"], "sar": a["sar"]["file"],
                        "summary": a["case"]["summary"]})
    SITE.mkdir(exist_ok=True)
    data = {"cases": cases, "monitor": monitor}
    (SITE / "cases.js").write_text("window.KAVACH = " + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")
    print(f"site/cases.js: {len(cases)} cases, {len(monitor)} monitor findings")
