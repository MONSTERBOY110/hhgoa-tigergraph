"""DuckDB implementations of the agent's graph tools (lane A).

Every function mirrors an installed GSQL query of the same name (kavach/graph/queries).
Returned frames use the same column names on both backends.
"""
from datetime import datetime, timedelta

import pandas as pd

from kavach.data.duck import connect

TXN_COLS = """TransactionID, card_id, customer_id, ts, TransactionAmt amt, ProductCD, channel, risk_score,
cast(addr1 as integer) addr1, cast(addr2 as integer) addr2, dist1, dist2, P_emaildomain, R_emaildomain, device_key, DeviceType,
id_15, id_23, id_31, M1, M2, M3, M4, M5, M6, M7, M8, M9, C1, C2, C13, C14, D1, D2, D3, D4, D10, D15, holder_key, card1, card4, card6"""

_con = None


def con():
    global _con
    if _con is None:
        _con = connect()
    return _con


def q(sql: str, *params) -> pd.DataFrame:
    return con().execute(sql, list(params)).df()


def _ts(t) -> datetime:
    return pd.Timestamp(t).to_pydatetime()


def txn_detail(txn_id) -> dict:
    df = q(f"select {TXN_COLS}, id_01, id_02, id_05, id_06, id_11, id_12, id_16, id_19, id_20, id_28, id_29, id_30, id_33, id_34, id_35, id_36, id_37, id_38, DeviceInfo from txn where TransactionID = ?", int(txn_id))
    return df.iloc[0].to_dict() if len(df) else {}


def card_history(card_id: str) -> pd.DataFrame:
    return q(f"select {TXN_COLS} from txn where card_id = ? order by ts", card_id)


def card_window(card_id: str, t0, hours_before: float = 48, hours_after: float = 48) -> pd.DataFrame:
    t0 = _ts(t0)
    return q(f"select {TXN_COLS} from txn where card_id = ? and ts between ? and ? order by ts",
             card_id, t0 - timedelta(hours=hours_before), t0 + timedelta(hours=hours_after))


def customer_cards(customer_id: str) -> pd.DataFrame:
    return q("""select card_id, count(*) n, min(ts) first_ts, max(ts) last_ts, any_value(card4) card4, any_value(card6) card6
                from txn where customer_id = ? group by 1 order by 1""", customer_id)


def card_profile(card_id: str, before=None) -> dict:
    """Baseline behaviour of a card, optionally only before a timestamp."""
    return profile_from_history(card_id, card_history(card_id), before)


def profile_from_history(card_id: str, h: pd.DataFrame, before=None) -> dict:
    if before is not None:
        h = h[h.ts < _ts(before)]
    if h.empty:
        return {"card_id": card_id, "n": 0}
    return {
        "card_id": card_id,
        "n": len(h),
        "first_ts": h.ts.min(), "last_ts": h.ts.max(),
        "products": h.ProductCD.value_counts().to_dict(),
        "channels": h.channel.value_counts().to_dict(),
        "regions": h.addr1.dropna().astype(int).value_counts().head(10).to_dict(),
        "amt_median": float(h.amt.median()), "amt_p90": float(h.amt.quantile(0.9)), "amt_max": float(h.amt.max()),
        "devices": h.device_key.dropna().value_counts().head(10).to_dict(),
        "p_emails": h.P_emaildomain.dropna().value_counts().head(5).to_dict(),
        "r_emails": h.R_emaildomain.dropna().value_counts().head(5).to_dict(),
        "mean_risk": float(h.risk_score.mean()),
    }


def device_neighbors(device_key: str, t0=None, days: float = 30) -> pd.DataFrame:
    """Cards seen on a device profile (optionally within +-days of t0)."""
    if t0 is None:
        return q("""select card_id, customer_id, count(*) n, min(ts) first_ts, max(ts) last_ts, sum(TransactionAmt) amt,
                    avg(risk_score) mean_risk, sum(case when id_15='New' then 1 else 0 end) n_new
                    from txn where device_key = ? group by 1,2 order by first_ts""", device_key)
    t0 = _ts(t0)
    return q("""select card_id, customer_id, count(*) n, min(ts) first_ts, max(ts) last_ts, sum(TransactionAmt) amt,
                avg(risk_score) mean_risk, sum(case when id_15='New' then 1 else 0 end) n_new
                from txn where device_key = ? and ts between ? and ? group by 1,2 order by first_ts""",
             device_key, t0 - timedelta(days=days), t0 + timedelta(days=days))


def device_txns(device_key: str, t0=None, days: float = 30) -> pd.DataFrame:
    if t0 is None:
        return q(f"select {TXN_COLS} from txn where device_key = ? order by ts", device_key)
    t0 = _ts(t0)
    return q(f"select {TXN_COLS} from txn where device_key = ? and ts between ? and ? order by ts",
             device_key, t0 - timedelta(days=days), t0 + timedelta(days=days))


def device_popularity(device_key: str) -> int:
    """Distinct cards ever seen on a device profile (common profiles like 'iOS Device' are weak links)."""
    return int(q("select count(distinct card_id) n from txn where device_key = ?", device_key).n.iloc[0])


def region_activity(addr1: int, t0, days: float = 7) -> pd.DataFrame:
    """Cards transacting in a billing region around t0, flagging cards with no earlier history there."""
    t0 = _ts(t0)
    return q("""with w as (select * from txn where addr1 = ? and ts between ? and ?),
                prior as (select distinct card_id from txn where addr1 = ? and ts < ?)
                select w.card_id, w.customer_id, count(*) n, min(w.ts) first_ts, max(w.ts) last_ts, sum(TransactionAmt) amt,
                       avg(risk_score) mean_risk, (w.card_id not in (select card_id from prior)) first_time
                from w group by 1,2 order by first_ts""",
             int(addr1), t0 - timedelta(days=days), t0 + timedelta(days=days), int(addr1), t0 - timedelta(days=days))


def email_neighbors(domain: str, t0, days: float = 7, field: str = "R_emaildomain") -> pd.DataFrame:
    assert field in ("R_emaildomain", "P_emaildomain")
    t0 = _ts(t0)
    return q(f"""select card_id, customer_id, count(*) n, min(ts) first_ts, max(ts) last_ts, sum(TransactionAmt) amt, avg(risk_score) mean_risk
                 from txn where {field} = ? and ts between ? and ? group by 1,2 order by first_ts""",
             domain, t0 - timedelta(days=days), t0 + timedelta(days=days))


def holder_cards(card_id: str) -> pd.DataFrame:
    """Cards sharing a holder_key (card1|addr1|account start day) with this card."""
    return q("""with hk as (select distinct holder_key from txn where card_id = ? and D1 is not null and addr1 is not null)
                select card_id, customer_id, count(*) n from txn where holder_key in (select holder_key from hk)
                group by 1,2 order by n desc""", card_id)


def closed_cases_for(card_ids=(), customer_ids=(), txn_ids=(), device_keys=()) -> pd.DataFrame:
    """Closed cases touching any of the given cards, customers, transactions or device profiles."""
    card_ids, customer_ids = list(card_ids) or [""], list(customer_ids) or [""]
    txn_ids = [int(t) for t in txn_ids] or [-1]
    device_keys = list(device_keys) or [""]
    return q("""with hit as (
                  select case_id, 'card' via from closed where card_id in (select unnest(?))
                  union select case_id, 'customer' from closed where customer_id in (select unnest(?))
                  union select case_id, 'connected_card' from closed_conn where card_id in (select unnest(?))
                  union select case_id, 'txn' from closed_txn where TransactionID in (select unnest(?))
                  union select ct.case_id, 'device' from closed_txn ct join txn t using (TransactionID) where t.device_key in (select unnest(?)))
                select c.case_id, string_agg(distinct hit.via, ',') via, c.card_id, c.outcome, c.pattern, c.opened_at, c.n_txns,
                       c.exposure_usd, c.connected_card_ids, c.report_filed, c.analyst_notes
                from hit join closed c using (case_id) group by all order by c.opened_at""",
             card_ids, customer_ids, card_ids, txn_ids, device_keys)


def recurring_check(card_id: str, amt: float, product_cd: str, before=None, tol: float = 0.02) -> pd.DataFrame:
    """Prior charges on the card with a similar amount and the same product code, with day gaps."""
    return recurring_from_history(card_history(card_id), amt, product_cd, before, tol)


def recurring_from_history(h: pd.DataFrame, amt: float, product_cd: str, before=None, tol: float = 0.02) -> pd.DataFrame:
    if before is not None:
        h = h[h.ts < _ts(before)]
    m = h[(h.ProductCD == product_cd) & ((h.amt - amt).abs() <= max(tol * amt, 0.5))].copy()
    m["gap_days"] = m.ts.diff().dt.total_seconds() / 86400
    return m[["TransactionID", "ts", "amt", "ProductCD", "addr1", "device_key", "P_emaildomain", "gap_days"]]


def case_pack() -> pd.DataFrame:
    return q("select * from cases order by case_id")


def closed_case(case_id: str) -> dict:
    df = q("select * from closed where case_id = ?", case_id)
    return df.iloc[0].to_dict() if len(df) else {}


def holder_fraud_history(holder_keys, before) -> pd.DataFrame:
    """Closed cases whose transactions share a holder_key (card1|addr1|account start) with the given keys."""
    keys = [k for k in holder_keys if k and not k.endswith("|")] or [""]
    return q("""select distinct ct.case_id, ct.outcome, ct.pattern, c.opened_at, t.holder_key
                from closed_txn ct join txn t using (TransactionID) join closed c using (case_id)
                where t.holder_key in (select unnest(?)) and cast(c.opened_at as timestamp) < ?
                order by c.opened_at""", keys, _ts(before))


def amount_peers(product_cd: str, amt: float, t0, days: float = 7, tol: float = 0.01,
                 email: str | None = None, email_field: str = "P_emaildomain") -> pd.DataFrame:
    """Online txns on ANY card with the same product and an amount within tol, around t0 (fixed-amount rings)."""
    assert email_field in ("P_emaildomain", "R_emaildomain")
    t0 = _ts(t0)
    lo, hi = amt * (1 - tol), amt * (1 + tol)
    sql = f"""select {TXN_COLS} from txn where ProductCD = ? and channel = 'online' and TransactionAmt between ? and ?
              and ts between ? and ?"""
    params = [product_cd, lo, hi, t0 - timedelta(days=days), t0 + timedelta(days=days)]
    if email:
        sql += f" and {email_field} = ?"
        params.append(email)
    return q(sql + " order by ts", *params)


def band_bursts(amt_lo: float, amt_hi: float, t0, days: float = 30, minutes: int = 40, min_n: int = 3) -> pd.DataFrame:
    """Cards with >= min_n online txns in [amt_lo, amt_hi) within `minutes` of each other (sub-threshold template)."""
    t0 = _ts(t0)
    return q("""with b as (select card_id, TransactionID, ts, TransactionAmt amt from txn
                           where channel = 'online' and TransactionAmt >= ? and TransactionAmt < ? and ts between ? and ?),
                w as (select *, count(*) over (partition by card_id order by ts range between interval (?) minute preceding and current row) k from b)
                select card_id, min(ts) first_ts, max(ts) last_ts, max(k) n, sum(amt) amt from w group by 1 having max(k) >= ?
                order by first_ts""",
             amt_lo, amt_hi, t0 - timedelta(days=days), t0 + timedelta(days=days), minutes, min_n)


def similar_cases(**kw) -> list:
    """Top-k closed cases by fingerprint similarity (see kavach.recall)."""
    from kavach import recall
    return recall.similar(**kw)


def vector_search_cases(text: str, k: int = 8) -> list:
    """Closed cases whose analyst notes are closest to the text (384-d embeddings, cosine)."""
    from kavach import vectors
    return vectors.search_cases(text, k)


def vector_search_docs(text: str, k: int = 2) -> list:
    """README pattern and Fraud Policy sections closest to the text."""
    from kavach import vectors
    return vectors.search_docs(text, k)
