"""DuckDB lane tools used only by grown detectors at runtime.

They are registered on a ToolRegistry on demand (kavach.grow.templates._call), so every call is still
counted and traced like any other tool. Names carry a `grow_` prefix so they never shadow a tool another
stream adds. The TigerGraph lane needs installed-query mirrors with the same names and shapes; until then a
grown detector that needs one of these on another backend is skipped and the skip is recorded.
"""
from datetime import timedelta

import pandas as pd

from kavach.duckq import TXN_COLS, _ts, q

ENTITIES = ("device_key", "R_emaildomain", "P_emaildomain", "addr1", "holder_key")


def _col(entity: str) -> str:
    assert entity in ENTITIES, entity
    return "cast(addr1 as integer)" if entity == "addr1" else entity


def _val(entity: str, value):
    return int(value) if entity == "addr1" else str(value)


def grow_entity_txns(entity: str, value, t0, days: float = 7) -> pd.DataFrame:
    """Every transaction on any card that carries the same entity value within +-days of t0."""
    t0 = _ts(t0)
    return q(f"select {TXN_COLS} from txn where {_col(entity)} = ? and ts between ? and ? order by ts, TransactionID",
             _val(entity, value), t0 - timedelta(days=days), t0 + timedelta(days=days))


def grow_entity_popularity(entity: str, value) -> int:
    """Distinct cards ever seen with this entity value (rare values link cards, common ones do not)."""
    return int(q(f"select count(distinct card_id) n from txn where {_col(entity)} = ?", _val(entity, value)).n.iloc[0])


def grow_entity_daily_cards(entity: str, value, d_from, d_to) -> pd.DataFrame:
    """Distinct (day, card) pairs paying with this entity value between two calendar days (inclusive)."""
    return q(f"""select distinct cast(ts as date) d, card_id from txn where {_col(entity)} = ?
                 and cast(ts as date) between ? and ? order by 1, 2""",
             _val(entity, value), pd.Timestamp(d_from).date(), pd.Timestamp(d_to).date())


def grow_date_range() -> dict:
    """First and last calendar day in the index (a fan-in baseline needs a full look-back window)."""
    r = q("select cast(min(ts) as date) d0, cast(max(ts) as date) d1 from txn").iloc[0]
    return {"d0": str(r.d0), "d1": str(r.d1)}


GROW_TOOLS = ("grow_entity_txns", "grow_entity_popularity", "grow_entity_daily_cards", "grow_date_range")
