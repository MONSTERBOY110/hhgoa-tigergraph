"""Round 2 DuckDB tools (lane A) that have no Round 1 equivalent yet.

Same conventions as kavach/duckq.py: one function per tool, pandas frames with the same column
names on both lanes, every call goes through kavach.tools.ToolRegistry (which falls back to this
module when a name is not in duckq). The TigerGraph mirrors live in kavach/graph/queries_r2.gsql.

Typology tools (S2, kavach/detectors/typologies.py):
  entity_neighbors(entity_field, value, t_from, t_to)   transactions on ANY card touching one entity in a window
  card_atypical_window(card_id, t_from, t_to)           a card's (or several cards') transactions with "atypical for this card" flags
  entity_baseline(entity_field, value, t0, days)        how many cards touched the entity in the window ending at t0, against
                                                        the cards per window over the previous 90 days and the active cards
Entity fields are a closed list (ENTITY_FIELDS); values are compared as strings so both lanes agree.
"""
from datetime import timedelta

import pandas as pd

from kavach.duckq import TXN_COLS, q, _ts  # noqa: F401  (shared helpers for the typology tools)

# Amount bands for the (ProductCD, amount band) entity: log-like edges, so a price template (for example
# "just under $500") falls in one band. band_label() and BAND_SQL must agree; tests check both.
BAND_EDGES = (5, 10, 20, 50, 100, 200, 350, 500, 750, 1000, 1500, 2500)


def band_label(amt: float) -> str:
    lo = 0
    for e in BAND_EDGES:
        if amt < e:
            return f"{lo}-{e}"
        lo = e
    return f"{lo}+"


def _band_sql(col: str = "TransactionAmt") -> str:
    parts, lo = [], 0
    for e in BAND_EDGES:
        parts.append(f"when {col} < {e} then '{lo}-{e}'")
        lo = e
    return "case " + " ".join(parts) + f" else '{lo}+' end"


# entity field -> SQL expression over txn (NULL when the row does not carry a usable value)
ENTITY_FIELDS = {
    "R_emaildomain": "R_emaildomain",
    "P_emaildomain": "P_emaildomain",
    # billing region only identifies a place of purchase for card-present rows
    "addr1": "case when channel = 'in_person' and addr1 is not null then cast(cast(addr1 as integer) as varchar) end",
    "product_band": "ProductCD || '|' || " + _band_sql(),
    # anonymised access signature: only rows behind a proxy carry one (a bare 'Found|desktop' links nothing)
    "access_signature": "case when id_23 is not null then ProductCD || '|' || id_23 || '|' || coalesce(id_15, '') || '|' "
                        "|| coalesce(DeviceType, '') end",
    "holder_key": "case when holder_key not like '%||%' and holder_key not like '%|' then holder_key end",
}


def entity_value(field: str, row) -> str | None:
    """The entity value of one transaction row (dict or namedtuple-like), computed in Python exactly like ENTITY_FIELDS."""
    g = (lambda k: row.get(k)) if isinstance(row, dict) else (lambda k: getattr(row, k, None))
    def ok(v):
        try:
            if pd.isna(v):          # None, NaN, NaT and pd.NA (pd.NA != pd.NA raises, so test with isna first)
                return False
        except (TypeError, ValueError):
            pass
        return str(v) != ""
    if field in ("R_emaildomain", "P_emaildomain"):
        v = g(field)
        return str(v) if ok(v) else None
    if field == "addr1":
        v = g("addr1")
        return str(int(float(v))) if g("channel") == "in_person" and ok(v) else None
    if field == "product_band":
        return f"{g('ProductCD')}|{band_label(float(g('amt')))}"
    if field == "access_signature":
        px = g("id_23")
        if not ok(px):
            return None
        i15, dt = g("id_15"), g("DeviceType")
        return f"{g('ProductCD')}|{px}|{i15 if ok(i15) else ''}|{dt if ok(dt) else ''}"
    if field == "holder_key":
        v = g("holder_key")
        return v if isinstance(v, str) and "||" not in v and not v.endswith("|") else None
    raise ValueError(field)


def _expr(field: str) -> str:
    if field not in ENTITY_FIELDS:
        raise ValueError(f"unknown entity field {field!r}; allowed: {sorted(ENTITY_FIELDS)}")
    return ENTITY_FIELDS[field]


def entity_neighbors(entity_field: str, value: str, t_from, t_to) -> pd.DataFrame:
    """Every transaction (any card) whose entity value equals `value` in [t_from, t_to], with the value in column `entity`."""
    e = _expr(entity_field)
    return q(f"""select {TXN_COLS}, {e} entity from txn
                 where {e} = ? and ts between ? and ? order by ts, TransactionID""",
             str(value), _ts(t_from), _ts(t_to))


def card_atypical_window(card_id, t_from, t_to) -> pd.DataFrame:
    """The card's transactions in [t_from, t_to] with flags computed against the card's OWN earlier history:
    product_unseen (first use of this ProductCD, card has >= 5 earlier txns), amt_above_prior_max (above every earlier
    amount, >= 5 earlier txns), amt_above_product_max (above every earlier amount for the same product code, >= 3 earlier
    txns of that product), device_new (identity record says New), proxy_unseen (first proxied row on a card with >= 5
    earlier txns), hours_since_prev, prior_n, atypical (any flag).
    `card_id` may be one id or a list of ids (one query for a set of neighbour cards)."""
    cards = [card_id] if isinstance(card_id, str) else list(card_id)
    if not cards:
        return pd.DataFrame(columns=["TransactionID", "card_id", "ts", "amt", "ProductCD", "channel", "prior_n", "product_unseen",
                                     "amt_above_prior_max", "amt_above_product_max", "device_new", "proxy_unseen", "hours_since_prev",
                                     "atypical"])
    return q("""with h as (select TransactionID, card_id, ts, TransactionAmt amt, ProductCD, channel, cast(addr1 as integer) addr1,
                                  P_emaildomain, R_emaildomain, device_key, id_15, id_23, DeviceType, risk_score, holder_key
                           from txn where card_id in (select unnest(?)) and ts <= ?),
                f as (select *,
                        row_number() over (partition by card_id order by ts, TransactionID) - 1 prior_n,
                        row_number() over (partition by card_id, ProductCD order by ts, TransactionID) - 1 prior_same_product,
                        max(amt) over (partition by card_id order by ts, TransactionID rows between unbounded preceding and 1 preceding) prior_max,
                        max(amt) over (partition by card_id, ProductCD order by ts, TransactionID rows between unbounded preceding and 1 preceding) prior_max_product,
                        lag(ts) over (partition by card_id order by ts, TransactionID) prev_ts,
                        coalesce(sum(case when id_23 is not null then 1 else 0 end) over (partition by card_id order by ts, TransactionID
                                 rows between unbounded preceding and 1 preceding), 0) prior_proxy
                      from h)
                select TransactionID, card_id, ts, amt, ProductCD, channel, addr1, P_emaildomain, R_emaildomain, device_key, id_15, id_23,
                       DeviceType, risk_score, holder_key, prior_n, prior_max,
                       (prior_same_product = 0 and prior_n >= 5) product_unseen,
                       (prior_n >= 5 and amt > prior_max) amt_above_prior_max,
                       (prior_same_product >= 3 and amt > prior_max_product) amt_above_product_max,
                       coalesce(id_15 = 'New', false) device_new,
                       (id_23 is not null and prior_proxy = 0 and prior_n >= 5) proxy_unseen,
                       date_diff('second', prev_ts, ts) / 3600.0 hours_since_prev,
                       ((prior_same_product = 0 and prior_n >= 5) or (prior_n >= 5 and amt > prior_max) or coalesce(id_15 = 'New', false)
                        or (id_23 is not null and prior_proxy = 0 and prior_n >= 5)) atypical
                from f where ts >= ? order by card_id, ts, TransactionID""",
             cards, _ts(t_to), _ts(t_from))


_DATA_START = None


def _data_start():
    global _DATA_START
    if _DATA_START is None:
        _DATA_START = pd.Timestamp(q("select min(ts) m from txn").m.iloc[0])
    return _DATA_START


def entity_baseline(entity_field: str, value: str, t0, days: float) -> dict:
    """Cards touching the entity in the window [t0 - days, t0] against its own history and the whole bank.

    cards_window      distinct cards with the entity in the window
    active_window     distinct cards with any transaction in the window (share_window = cards_window / active_window)
    baseline_per_window  mean distinct cards per `days`-long bucket over the 90 days before the window (complete buckets only)
    baseline_buckets  how many complete buckets inside the data the baseline rests on (0 = no baseline, the rule must not fire)
    """
    e = _expr(entity_field)
    t0 = pd.Timestamp(_ts(t0))
    w0 = t0 - timedelta(days=days)
    n_b = max(1, int(90 // days))
    b0 = w0 - timedelta(days=n_b * days)
    cur = q(f"""select count(distinct card_id) filter (where {e} = ?) k, count(distinct card_id) n
                from txn where ts between ? and ?""", str(value), w0.to_pydatetime(), t0.to_pydatetime())
    buckets = q(f"""select floor(date_diff('second', ?, ts) / ?) b, count(distinct card_id) k
                    from txn where {e} = ? and ts >= ? and ts < ? group by 1""",
                b0.to_pydatetime(), days * 86400.0, str(value), b0.to_pydatetime(), w0.to_pydatetime())
    start = _data_start()
    complete = [i for i in range(n_b) if b0 + timedelta(days=i * days) >= start]
    per = {int(r.b): int(r.k) for r in buckets.itertuples()}
    base = sum(per.get(i, 0) for i in complete) / len(complete) if complete else None
    k, n = int(cur.k.iloc[0]), int(cur.n.iloc[0])
    return {"entity_field": entity_field, "value": str(value), "t0": str(t0), "days": days,
            "cards_window": k, "active_window": n, "share_window": round(k / n, 6) if n else 0.0,
            "baseline_per_window": None if base is None else round(base, 3), "baseline_buckets": len(complete)}


def entity_shares(entity_field: str, values, t0, days: float) -> pd.DataFrame:
    """For several values of one entity field at once: distinct cards touching each value in [t0 - days, t0], the active
    cards in that window, and the share (one query instead of one entity_baseline per value; used for the rarity screen)."""
    e = _expr(entity_field)
    vals = [str(v) for v in values]
    t0 = pd.Timestamp(_ts(t0))
    w0 = t0 - timedelta(days=days)
    if not vals:
        return pd.DataFrame(columns=["value", "cards", "active", "share"])
    df = q(f"""with w as (select {e} v, card_id from txn where ts between ? and ?),
                    a as (select count(distinct card_id) n from w)
               select v as "value", count(distinct card_id) as cards, any_value(a.n) as active from w, a
               where v in (select unnest(?)) group by 1 order by 1""", w0.to_pydatetime(), t0.to_pydatetime(), vals)
    df["share"] = (df.cards / df.active).round(6) if len(df) else []
    return df


# grown-detector tools (S6), resolvable through ToolRegistry's duckq_ext fallback for attestation re-runs
from kavach.grow.gtools import grow_date_range, grow_entity_daily_cards, grow_entity_popularity, grow_entity_txns  # noqa: E402,F401


def small_online_txns(t_from, t_to, max_amt: float = 6.0) -> pd.DataFrame:
    """Every online transaction under max_amt on any card in [t_from, t_to] (input of the cross-customer micro-charge
    check). TigerGraph mirror: TgTools.small_online_txns over the installed band_txns query."""
    return q("""select TransactionID, card_id, ts, TransactionAmt amt from txn
                where channel = 'online' and TransactionAmt < ? and ts between ? and ? order by ts, TransactionID""",
             float(max_amt), _ts(t_from), _ts(t_to))
