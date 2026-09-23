"""Lane A: DuckDB index over the organizer CSVs.

Card id mapping (verified against every closed-case transaction, 100% agreement):
card_id = customer_id + "-K" + rank of coalesce(card6, '') among that customer's
distinct card6 values, ordered lexically (null first, then credit, debit, ...).
"""
import duckdb

from kavach.config import DUCK_PATH, RAW

NULL_CARD = "card2 is null and card3 is null and card4 is null and card5 is null and card6 is null"


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(DUCK_PATH), read_only=read_only)


def build() -> None:
    DUCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DUCK_PATH))
    csv = lambda name: str(RAW / name).replace("\\", "/")
    print("loading raw tables")
    con.execute(f"create or replace table txn_raw as select * from read_csv('{csv('transactions.csv')}', header=true, sample_size=-1)")
    con.execute(f"create or replace table ident as select * from read_csv('{csv('identity.csv')}', header=true, sample_size=-1)")
    con.execute(f"create or replace table closed as select * from read_csv('{csv('closed_cases_history.csv')}', header=true, all_varchar=true)")
    con.execute(f"create or replace table cases as select * from read_csv('{csv('case_pack.csv')}', header=true, all_varchar=true)")

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
        from closed""")
    con.execute("""
        create or replace table closed_conn as
        select case_id, unnest(string_split(connected_card_ids, '|')) card_id from closed where connected_card_ids is not null""")
    con.execute("checkpoint")
    report(con)
    con.close()


def report(con) -> None:
    for t in ("txn", "ident", "closed", "cases", "card_map", "closed_txn"):
        print(f"{t:12s} {con.execute(f'select count(*) from {t}').fetchone()[0]:>9,}")
    agree = con.execute("""
        select avg(case when t.card_id = c.card_id then 1.0 else 0 end), count(*)
        from closed_txn c join txn t using (TransactionID)""").fetchone()
    print(f"closed-case card_id agreement: {agree[0]:.4%} over {agree[1]:,} txns")
    pack = con.execute("""
        select count(*), sum(case when t.card_id = c.card_id and t.customer_id = c.customer_id then 1 else 0 end)
        from cases c join txn t on t.TransactionID = cast(c.flagged_txn_id as bigint)""").fetchone()
    print(f"case pack flagged txns found: {pack[0]}/20, card_id agrees: {pack[1]}/20")
    assert agree[0] >= 0.99, "card_id mapping below 99%"
