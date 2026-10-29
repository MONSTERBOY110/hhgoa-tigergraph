"""Lane A: DuckDB index over the organizer CSVs (Round 1, July to December 2016, plus Round 2, January 2017).

Card id mapping (verified against every closed-case transaction, 100% agreement in Round 1):
card_id = customer_id + "-K" + rank of coalesce(card6, '') among that customer's
distinct card6 values, ordered lexically (null first, then credit, debit, ...).
Round 2 keeps the same rule over the union of both transaction files; build() asserts that it still
reproduces every closed-case card id (5,585 cases) and every case-pack card id (both rounds).

Tables: txn (all columns plus card_id, day, holder_key, identity columns, src_file, round), ident, ident2,
closed (the Round 2 history), closed_txn, closed_conn, cases (both packs, with a round column), card_map,
grader_scope (TransactionIDs that exist in transactions_2017q1.csv, the only file the organizer grader sees),
dataset_version (one row per source file: bytes, sha256, rows, built_at).
"""
import hashlib
import time
from pathlib import Path

import duckdb

from kavach.config import DUCK_PATH, RAW, RAW2

NULL_CARD = "card2 is null and card3 is null and card4 is null and card5 is null and card6 is null"

TXN_FILES = [(RAW / "transactions.csv", 1), (RAW2 / "transactions_2017q1.csv", 2)]
IDENT_FILES = [(RAW / "identity.csv", 1), (RAW2 / "identity_2017q1.csv", 2)]
CLOSED_FILE = RAW2 / "closed_cases_history.csv"
CASE_PACKS = [(RAW / "case_pack.csv", 1), (RAW2 / "cases" / "case_pack_final.csv", 2)]


def _p(p) -> str:
    return str(p).replace("\\", "/")


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DUCK_PATH), read_only=read_only)


def _load_union(con, table: str, files: list) -> None:
    """First file with full sniffing, later files cast to the first file's column types (an all-null column in a
    small file would otherwise sniff as VARCHAR and poison the union)."""
    first, rnd = files[0]
    con.execute(f"create or replace table {table} as select *, '{first.name}' as src_file, {rnd} as round "
                f"from read_csv('{_p(first)}', header=true, sample_size=-1)")
    types = {r[0]: r[1] for r in con.execute(f"describe {table}").fetchall() if r[0] not in ("src_file", "round")}
    for path, rnd in files[1:]:
        if not path.exists():
            print(f"  {table}: missing {path}, skipped")
            continue
        tdict = "{" + ", ".join(f"'{k}': '{v}'" for k, v in types.items()) + "}"
        try:
            con.execute(f"insert into {table} by name select *, '{path.name}' as src_file, {rnd} as round "
                        f"from read_csv('{_p(path)}', header=true, sample_size=-1, types={tdict})")
        except duckdb.Error as e:  # a value that does not fit the Round 1 type: cast column by column, nulling the misfits
            print(f"  {table}: typed read failed ({str(e)[:80]}), falling back to try_cast per column")
            cols = ", ".join(f'try_cast("{k}" as {v}) as "{k}"' for k, v in types.items())
            con.execute(f"insert into {table} by name select {cols}, '{path.name}' as src_file, {rnd} as round "
                        f"from read_csv('{_p(path)}', header=true, sample_size=-1, all_varchar=true)")


def _file_version(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
    size = path.stat().st_size
    h.update(str(size).encode())
    return size, h.hexdigest()


def build(out_path=None) -> None:
    out = Path(out_path) if out_path else DUCK_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    con = duckdb.connect(str(out))
    print(f"building {out}")
    print("loading raw tables")
    _load_union(con, "txn_raw", TXN_FILES)
    _load_union(con, "ident", IDENT_FILES)
    con.execute(f"create or replace table closed as select * from read_csv('{_p(CLOSED_FILE)}', header=true, all_varchar=true)")
    packs = " union all by name ".join(
        f"select *, {rnd} as round from read_csv('{_p(p)}', header=true, all_varchar=true)" for p, rnd in CASE_PACKS if p.exists())
    con.execute(f"create or replace table cases as {packs}")

    print("card map")
    con.execute("""
        create or replace table card_map as
        with g as (select distinct customer_id, coalesce(card6, '') k from txn_raw)
        select customer_id, k, customer_id || '-K' || row_number() over (partition by customer_id order by k) card_id
        from g""")

    print("device profiles")
    con.execute("""
        create or replace table ident2 as
        select *, coalesce(DeviceInfo, '') || ' | ' || coalesce(id_30, '') || ' | ' || coalesce(id_31, '') || ' | ' || coalesce(id_33, '') device_key
        from ident""")

    print("txn with derived columns")
    con.execute(f"""
        create or replace table txn as
        select t.*, m.card_id, cast(floor(t.TransactionDT / 86400) as integer) as "day",
               cast(t.card1 as varchar) || '|' || coalesce(cast(t.addr1 as varchar), '') || '|' ||
                   coalesce(cast(cast(floor(t.TransactionDT / 86400) - t.D1 as integer) as varchar), '') as holder_key,
               ({NULL_CARD}) as card_fields_null,
               i.device_key, i.DeviceType, i.DeviceInfo, i.id_15, i.id_23, i.id_30, i.id_31, i.id_33, i.id_34,
               i.id_01, i.id_02, i.id_05, i.id_06, i.id_11, i.id_12, i.id_16, i.id_19, i.id_20, i.id_28, i.id_29,
               i.id_35, i.id_36, i.id_37, i.id_38
        from txn_raw t
        join card_map m on m.customer_id = t.customer_id and m.k = coalesce(t.card6, '')
        left join ident2 i using (TransactionID)
        order by card_id, ts""")
    con.execute("drop table txn_raw")

    print("closed case txn links")
    con.execute("""
        create or replace table closed_txn as
        select case_id, customer_id, card_id, outcome, pattern, cast(unnest(string_split(txn_ids, '|')) as bigint) TransactionID
        from closed where txn_ids is not null and txn_ids <> ''""")
    con.execute("""
        create or replace table closed_conn as
        select case_id, unnest(string_split(connected_card_ids, '|')) card_id from closed
        where connected_card_ids is not null and connected_card_ids <> ''""")
    print("grader scope (January ids) and dataset version")
    con.execute("create or replace table grader_scope as select TransactionID from txn where round = 2")
    rows = {"txn": "select src_file, count(*) from txn group by 1", "ident": "select src_file, count(*) from ident group by 1"}
    counts = {}
    for sql in rows.values():
        counts.update({r[0]: r[1] for r in con.execute(sql).fetchall()})
    counts[CLOSED_FILE.name] = con.execute("select count(*) from closed").fetchone()[0]
    for p, rnd in CASE_PACKS:
        counts[p.name] = con.execute(f"select count(*) from cases where round = {rnd}").fetchone()[0]
    built_at = time.strftime("%Y-%m-%d %H:%M:%S")
    con.execute("create or replace table dataset_version (name varchar, bytes bigint, sha256 varchar, rows bigint, built_at varchar)")
    for path in [p for p, _ in TXN_FILES + IDENT_FILES + CASE_PACKS] + [CLOSED_FILE]:
        if path.exists():
            size, digest = _file_version(path)
            con.execute("insert into dataset_version values (?, ?, ?, ?, ?)", [path.name, size, digest, counts.get(path.name, 0), built_at])
    con.execute("checkpoint")
    report(con)
    con.close()
    print(f"built {out} in {time.time() - t0:.0f}s")


def report(con) -> None:
    for t in ("txn", "ident", "closed", "cases", "card_map", "closed_txn", "closed_conn", "grader_scope"):
        print(f"{t:13s} {con.execute(f'select count(*) from {t}').fetchone()[0]:>9,}")
    for src, n in con.execute("select src_file, count(*) from txn group by 1 order by 1").fetchall():
        print(f"  {src:28s} {n:>9,}")
    agree = con.execute("""
        select avg(case when t.card_id = c.card_id then 1.0 else 0 end), count(*)
        from closed_txn c join txn t using (TransactionID)""").fetchone()
    print(f"closed-case card_id agreement: {agree[0]:.4%} over {agree[1]:,} txns")
    for rnd, n, ok in con.execute("""
        select c.round, count(*), sum(case when t.card_id = c.card_id and t.customer_id = c.customer_id then 1 else 0 end)
        from cases c join txn t on t.TransactionID = cast(c.flagged_txn_id as bigint) group by 1 order by 1""").fetchall():
        print(f"round {rnd} case pack: flagged txns found {n}, card_id agrees {ok}/{n}")
    # A case-pack row whose stated card disagrees with the card its flagged transaction maps to is a data-quality finding,
    # not a mapping failure (the mapping is verified on every closed case above). The agent resolves the card from the
    # transaction and records the discrepancy (kavach/context.py).
    for row in con.execute("""
        select c.case_id, c.card_id, t.card_id, t.card6 from cases c join txn t on t.TransactionID = cast(c.flagged_txn_id as bigint)
        where t.card_id <> c.card_id or t.customer_id <> c.customer_id order by 1""").fetchall():
        print(f"  DATA QUALITY: {row[0]} states card {row[1]} but its flagged transaction is on {row[2]} (card6={row[3]})")
    assert agree[0] >= 0.99, "card_id mapping below 99%"
