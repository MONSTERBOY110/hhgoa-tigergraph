"""Export slim CSVs from DuckDB and load them into TigerGraph with GSQL loading jobs."""
import time
from pathlib import Path

import duckdb

from kavach.config import DATA, DUCK_PATH, ROOT

TG_DIR = DATA / "tg"


def _p(p) -> str:
    return str(p).replace("\\", "/")


EXPORTS = {
    "customer": "select distinct customer_id from txn",
    "card": """select card_id, any_value(card1), any_value(cast(card2 as varchar)), any_value(cast(card3 as varchar)),
                      any_value(card4), any_value(cast(card5 as varchar)), any_value(card6),
                      mode(cast(addr1 as integer)), count(*) from txn group by card_id""",
    "owns": "select distinct customer_id, card_id from txn",
    "holder": """select distinct holder_key, card_id from txn where D1 is not null and addr1 is not null""",
    "txn": """select TransactionID, strftime(ts, '%Y-%m-%d %H:%M:%S'), TransactionDT, TransactionAmt, ProductCD, channel, risk_score,
                     cast(addr1 as integer), cast(addr2 as integer), dist1, P_emaildomain, R_emaildomain,
                     M1, M2, M3, M4, M5, M6, M7, M8, M9, C1, C2, C13, C14, D1, D2, D3, D4, D10, D15,
                     case when id_15 = 'New' then 'true' else 'false' end, id_23, DeviceType, card_id, customer_id,
                     case when replace(replace(coalesce(device_key, ''), '|', ''), ' ', '') = '' then null else device_key end,
                     id_15, id_31, holder_key, card1, card4, card6 from txn""",
    "made": "select card_id, TransactionID from txn",
    "device": """select device_key, any_value(DeviceInfo), any_value(id_30), any_value(id_31), any_value(id_33), count(distinct card_id)
                 from txn where device_key is not null and replace(replace(device_key, '|', ''), ' ', '') <> '' group by 1""",
    "from_device": """select TransactionID, device_key from txn
                      where device_key is not null and replace(replace(device_key, '|', ''), ' ', '') <> ''""",
    "p_email": "select TransactionID, P_emaildomain from txn where P_emaildomain is not null",
    "r_email": "select TransactionID, R_emaildomain from txn where R_emaildomain is not null",
    "billed_in": "select TransactionID, cast(cast(addr1 as integer) as varchar) from txn where addr1 is not null",
    "next_txn": """select TransactionID, nxt, gap from (select TransactionID, lead(TransactionID) over w nxt,
                   cast(lead(TransactionDT) over w - TransactionDT as integer) gap from txn window w as (partition by card_id order by ts, TransactionID))
                   where nxt is not null""",
    "closed_case": """select case_id, outcome, pattern, opened_at, closed_at, exposure_usd, n_txns, report_filed, actions_taken,
                             replace(replace(analyst_notes, chr(10), ' '), '"', '''') from closed""",
    "involves": "select case_id, TransactionID from closed_txn",
    "on_card": "select case_id, card_id from closed",
    "connected_to": "select case_id, card_id from closed_conn",
}


def export() -> None:
    TG_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DUCK_PATH), read_only=True)
    for name, sql in EXPORTS.items():
        out = TG_DIR / f"{name}.csv"
        con.execute(f"copy ({sql}) to '{_p(out)}' (header false, delimiter ',', quote '\"')")
        n = con.execute(f"select count(*) from ({sql})").fetchone()[0]
        print(f"{name:12s} {n:>10,}")
    con.close()


# loading job: file name -> (target statement)
JOB_LINES = {
    "customer": "TO VERTEX Customer VALUES($0)",
    "card": "TO VERTEX Card VALUES($0, $1, $2, $3, $4, $5, $6, $7, $8)",
    "owns": "TO EDGE OWNS VALUES($0, $1)",
    "holder": "TO VERTEX Holder VALUES($0), TO EDGE HOLDER_CARD VALUES($0, $1)",
    "txn": ("TO VERTEX Transaction VALUES($0, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, "
            "$18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31, $32, $33, $34, $35, $36, $37, $38, $39, $40, $41, $42)"),
    "made": "TO EDGE MADE VALUES($0, $1)",
    "device": "TO VERTEX DeviceProfile VALUES($0, $1, $2, $3, $4, $5)",
    "from_device": "TO EDGE FROM_DEVICE VALUES($0, $1)",
    "p_email": "TO VERTEX EmailDomain VALUES($1), TO EDGE PURCHASER_EMAIL VALUES($0, $1)",
    "r_email": "TO VERTEX EmailDomain VALUES($1), TO EDGE RECIPIENT_EMAIL VALUES($0, $1)",
    "billed_in": "TO VERTEX BillingRegion VALUES($1), TO EDGE BILLED_IN VALUES($0, $1)",
    "next_txn": "TO EDGE NEXT_TXN VALUES($0, $1, $2)",
    "closed_case": "TO VERTEX ClosedCase VALUES($0, $1, $2, $3, $4, $5, $6, $7, $8, $9)",
    "involves": "TO EDGE INVOLVES VALUES($0, $1)",
    "on_card": "TO EDGE ON_CARD VALUES($0, $1)",
    "connected_to": "TO EDGE CONNECTED_TO VALUES($0, $1)",
}


def job_gsql(graph: str) -> str:
    lines = [f"CREATE LOADING JOB load_kavach FOR GRAPH {graph} {{"]
    for name in JOB_LINES:
        lines.append(f'  DEFINE FILENAME f_{name};')
    for name, target in JOB_LINES.items():
        stmts = [s.strip() for s in target.split(", TO ")]
        stmts = [stmts[0]] + ["TO " + s for s in stmts[1:]]
        lines.append(f"  LOAD f_{name} " + ", ".join(stmts) + ' USING SEPARATOR=",", QUOTE="double", HEADER="false";')
    lines.append("}")
    return "\n".join(lines)


def load(conn, graph: str, only: list[str] | None = None, chunk_lines: int = 50_000) -> None:
    """Post each CSV to the loading job in chunks (keeps every request well under upload limits)."""
    import tempfile
    for name in JOB_LINES:
        if only and name not in only:
            continue
        path = TG_DIR / f"{name}.csv"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        t0, ok = time.time(), 0
        for i in range(0, len(lines), chunk_lines):
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".csv", encoding="utf-8") as tmp:
                tmp.writelines(lines[i:i + chunk_lines])
            res = conn.runLoadingJobWithFile(tmp.name, f"f_{name}", "load_kavach", sep=",", timeout=3_600_000)
            Path(tmp.name).unlink(missing_ok=True)
            stats = res[0]["statistics"] if isinstance(res, list) and res and "statistics" in res[0] else res
            ok += min(chunk_lines, len(lines) - i)
            bad = str(stats).count("invalid") if stats else 0
            print(f"  {name}: {ok:,}/{len(lines):,} lines posted", flush=True)
        print(f"loaded {name} ({len(lines):,} lines) in {time.time() - t0:.0f}s; last stats: {str(stats)[:400]}", flush=True)
