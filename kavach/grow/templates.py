"""Pattern families for the self-growing detective.

Each family is a parametrised shape of fraud (a small DSL): a parameter grid the miner searches, a vectorised
`fire()` that evaluates one grid point over a set of transaction ids in SQL, and an `apply()` that evaluates the
same rule at runtime for one case using only ToolRegistry calls (so every grown-detector hit is traced and
re-runnable). The SQL in `fire()` and the pandas in `apply()` implement the same definition; tests check parity.

Nothing here knows about case ids, transaction ids or the hand-written typologies' thresholds. The grids are
generic (round amount limits, a few windows and counts); the miner decides which points matter from the labels.

Data facts honoured (docs/round2/january-data-profile.md):
  * holder_key is not usable from January 2017 (D1 was resampled), so it is treated as missing from then on;
  * generic device profiles (empty or seen on more than RARE cards) link nothing and are excluded.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from kavach.assess import Signal

RARE = 100                              # an entity value seen on more cards than this is generic (context.RARE_DEVICE)
HOLDER_KEY_UNTIL = "2017-01-01"         # D1 resampled in January: holder_key carries no account identity after this
BASELINE_DAYS = 56                      # fan-in baseline look-back (eight weeks)
NDP_DAYS = 30                           # device window for the new-device-proxy family (same as context.gather)

_CACHE: dict = {}


def _p(path) -> str:
    return str(path).replace("\\", "/")


# ----------------------------------------------------------------------------------------------------------------
# data preparation (miner side)

def prepare(con, duck_path=None, features_path=None) -> None:
    """Attach the index read-only and build the in-memory base table `t` every family's SQL reads."""
    from kavach.config import DUCK_PATH
    duck_path = Path(duck_path or DUCK_PATH)
    features_path = Path(features_path) if features_path else None
    con.execute(f"attach '{_p(duck_path)}' as src (read_only)")
    con.execute(f"""
        create or replace temp table t as
        with x as (
          select TransactionID, card_id, ts, epoch(ts) es, cast(ts as date) d, TransactionAmt amt, ProductCD, channel,
                 case when replace(replace(coalesce(device_key, ''), '|', ''), ' ', '') = '' then null else device_key end device_key,
                 cast(id_15 as varchar) id_15, cast(id_23 as varchar) id_23, cast(P_emaildomain as varchar) P_emaildomain,
                 cast(R_emaildomain as varchar) R_emaildomain, cast(addr1 as integer) addr1,
                 case when holder_key is null or holder_key like '%||%' or holder_key like '%|'
                      or ts >= TIMESTAMP '{HOLDER_KEY_UNTIL}' then null else holder_key end holder_key
          from src.txn)
        select *, lag(es) over (partition by card_id order by ts, TransactionID) prev_es from x""")
    # the features parquet carries the same gap definition; when present it is checked against the lag above
    if features_path is not None and features_path.exists():
        bad = con.execute(f"""select count(*) from t join '{_p(features_path)}' f using (TransactionID)
                               where f.prev_es is distinct from t.prev_es""").fetchone()[0]
        if bad:
            print(f"  note: {bad} rows differ in prev_es between the index and {features_path.name}; using the index")
    con.execute("""create or replace temp table devpop as
                   select device_key, count(distinct card_id) pop from t where device_key is not null group by 1""")


def set_ids(con, txn_ids, table: str = "_ev") -> str:
    """Register an id set as a temp table; return its name and a cache token."""
    if isinstance(txn_ids, str):
        return txn_ids
    ids = pd.DataFrame({"TransactionID": pd.Series(list(txn_ids), dtype="int64")}).drop_duplicates()
    con.register("_ids_df", ids)
    con.execute(f"create or replace temp table {table} as select * from _ids_df")
    con.unregister("_ids_df")
    return table


def _token(con, table: str) -> tuple:
    n, s = con.execute(f"select count(*), coalesce(sum(TransactionID), 0) from {table}").fetchone()
    return (id(con), table, int(n), int(s))


def clear_cache() -> None:
    _CACHE.clear()


# ----------------------------------------------------------------------------------------------------------------
# runtime helpers

class ToolUnavailable(RuntimeError):
    """A grown detector needs a tool the current backend does not serve."""


def _call(ctx, name: str, **kw):
    from kavach.grow import gtools
    T = ctx.tools
    if name in gtools.GROW_TOOLS and name not in getattr(T, "_extra", {}) and not hasattr(getattr(T, "_impl", None), name):
        if getattr(T, "backend", "duck") == "duck":
            T.register(name, getattr(gtools, name))
        else:
            raise ToolUnavailable(name)
    return T.call(name, **kw)


def _jsonable(kw: dict) -> dict:
    out = {}
    for k, v in kw.items():
        if isinstance(v, pd.Timestamp) or hasattr(v, "isoformat"):
            out[k] = str(pd.Timestamp(v))
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        elif isinstance(v, (np.floating,)):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _ref(name: str, kw: dict) -> str:
    from kavach.tools import ToolRegistry
    return ToolRegistry.ref(name, **_jsonable(kw))


def _ids(df) -> list[str]:
    return [str(int(x)) for x in df.TransactionID]


def _clean_device(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return None if str(v).replace("|", "").strip() == "" else v


def _llr(spec: dict) -> float:
    lift = float(spec.get("backtest", {}).get("lift") or 1.0)
    return round(max(0.0, min(2.0, 0.5 * math.log(max(lift, 1.0)))), 2)


def _claim(spec: dict, what: str) -> str:
    bt = spec.get("backtest", {})
    ap = spec.get("approval") or {}
    who = f"approved {ap['id']} by {ap.get('role')}" if ap.get("id") else "proposed, not approved"
    return (f"Grown detector {spec['name']} ({who}) fired: {what}. "
            f"Backtest on the closed cases: {bt.get('support', 0)} confirmed-fraud hits, {bt.get('cleared_hits', 0)} cleared hits, "
            f"population rate {100 * float(bt.get('population_rate', 0)):.2f}%, lift {float(bt.get('lift', 0)):.1f}")


def _signal(spec: dict, group: str, what: str, entity_ids: list, tool: str, kw: dict) -> Signal:
    kw = _jsonable(kw)
    return Signal(f"grown:{spec['name']}", group, _llr(spec), _claim(spec, what), entity_ids, _ref(tool, kw),
                  "graph", {"tool": tool, "params": kw})


# ----------------------------------------------------------------------------------------------------------------
# families

class Family:
    name = ""
    group = "sequence"
    keywords: tuple = ()

    def grid(self) -> list[dict]:
        raise NotImplementedError

    def builtin(self) -> list[tuple[str, dict]]:
        """Grid points that reproduce a hand-written detector of the same shape (novelty reference)."""
        return []

    def stat_key(self, params: dict) -> tuple:
        raise NotImplementedError

    def stats_sql(self, params: dict, ev: str) -> str:
        raise NotImplementedError

    def rule(self, s: pd.DataFrame, params: dict) -> pd.Series:
        raise NotImplementedError

    def text(self, params: dict) -> str:
        raise NotImplementedError

    def slug(self, params: dict) -> str:
        raise NotImplementedError

    def stats(self, con, params: dict, ev: str) -> pd.DataFrame:
        key = (_token(con, ev), self.name, self.stat_key(params))
        if key not in _CACHE:
            _CACHE[key] = con.execute(self.stats_sql(params, ev)).df()
        return _CACHE[key]

    def fire(self, con, params: dict, txn_ids) -> pd.DataFrame:
        """DataFrame[TransactionID, fired] for every id in txn_ids (a list or the name of a temp table)."""
        ev = set_ids(con, txn_ids)
        s = self.stats(con, params, ev)
        hit = set(s.TransactionID[self.rule(s, params)].astype("int64")) if len(s) else set()
        ids = con.execute(f"select TransactionID from {ev}").df()
        ids["fired"] = ids.TransactionID.isin(hit)
        return ids

    def apply(self, ctx, spec: dict) -> list[Signal]:
        raise NotImplementedError


def _num(x) -> str:
    return f"{x:g}".replace(".", "p").replace("-", "m")


class BandBurst(Family):
    """n or more online purchases with amounts in [lo, hi) on one card within +-minutes."""
    name = "band_burst"
    group = "sequence"
    keywords = ("just under", "threshold", "within forty minutes", "four online purchases", "burst", "authorization")
    LIMITS = (100, 200, 250, 500, 1000, 2000)

    def grid(self):
        return [{"lo": round(thr * (1 - w), 2), "hi": float(thr), "n": n, "minutes": m}
                for thr in self.LIMITS for w in (0.1, 0.2) for n in (3, 4) for m in (30, 60)]

    def builtin(self):
        return [("sub_threshold_burst", {"lo": round(0.88 * thr, 2), "hi": float(thr), "n": 3, "minutes": 60})
                for thr in (500.0, 1000.0, 2500.0)]

    def stat_key(self, p):
        return (p["lo"], p["hi"], p["minutes"])

    def stats_sql(self, p, ev):
        s = int(p["minutes"] * 60)
        return f"""with e as (select t.TransactionID, t.card_id, t.es from {ev} v join t using (TransactionID)
                              where t.channel = 'online' and t.amt >= {p['lo']} and t.amt < {p['hi']}),
                    b as (select card_id, es from t where channel = 'online' and amt >= {p['lo']} and amt < {p['hi']}
                          and card_id in (select card_id from e))
                    select e.TransactionID, count(*) k from e join b on b.card_id = e.card_id and b.es between e.es - {s} and e.es + {s}
                    group by 1"""

    def rule(self, s, p):
        return s.k >= p["n"]

    def text(self, p):
        return (f"{p['n']} or more online purchases between ${p['lo']:,.2f} and ${p['hi']:,.2f} on one card within "
                f"{p['minutes']} minutes of each other")

    def slug(self, p):
        return f"band_burst_{_num(p['lo'])}_{_num(p['hi'])}_n{p['n']}_{p['minutes']}m"

    def apply(self, ctx, spec):
        p, f = spec["params"], ctx.f
        if f.get("channel") != "online" or not (p["lo"] <= float(f["amt"]) < p["hi"]):
            return []
        kw = {"card_id": ctx.card_id, "t0": ctx.t0, "hours_before": p["minutes"] / 60, "hours_after": p["minutes"] / 60}
        w = _call(ctx, "card_window", **kw)
        b = w[(w.channel == "online") & (w.amt >= p["lo"]) & (w.amt < p["hi"])]
        if len(b) < p["n"]:
            return []
        what = (f"{len(b)} online purchases of ${b.amt.min():,.2f} to ${b.amt.max():,.2f} on card {ctx.card_id} within "
                f"{p['minutes']} minutes of the flagged transaction, all inside the band ${p['lo']:,.2f} to ${p['hi']:,.2f}")
        return [_signal(spec, self.group, what, _ids(b), "card_window", kw)]


class SharedEntity(Family):
    """The transaction's entity value was used by min_cards or more OTHER cards within +-days (amounts within tol)."""
    name = "shared_entity"
    group = "network"
    keywords = ("other cardholders", "same device profile", "shared", "same email", "same region")
    ENTITIES = ("device_key", "R_emaildomain", "P_emaildomain", "addr1", "holder_key")

    def grid(self):
        return [{"entity": e, "min_cards": k, "days": d, "amount_tol": tol}
                for e in self.ENTITIES for d in (1, 7) for tol in (0.02, None) for k in (2, 3, 5)]

    def builtin(self):
        # fixed_amount_ring also needs a shared D4/D15 fingerprint, which this family cannot express, so it is not listed
        return [("device_ring", {"entity": "device_key", "min_cards": 2, "days": 30, "amount_tol": 0.02})]

    def stat_key(self, p):
        return (p["entity"], p["days"], p["amount_tol"])

    def stats_sql(self, p, ev):
        e, s, tol = p["entity"], int(p["days"] * 86400), p["amount_tol"]
        rare = tol is None or e == "device_key"
        pop = f"(select {e} v, count(distinct card_id) n from t where {e} is not null group by 1)"
        base = (f"select t.TransactionID, t.card_id, t.es, t.amt, t.{e} v from {ev} x join t using (TransactionID) "
                + (f"join {pop} pp on pp.v = t.{e} where pp.n <= {RARE}" if rare else f"where t.{e} is not null"))
        if tol is None:
            return f"""with e as ({base}),
                        b as (select card_id, es, {e} v from t where {e} in (select v from e))
                        select e.TransactionID, count(distinct b.card_id) filter (where b.card_id <> e.card_id) k
                        from e join b on b.v = e.v and b.es between e.es - {s} and e.es + {s} group by 1"""
        lb = f"ln(1 + {tol})"
        return f"""with e as ({base}),
                    e5 as (select e.*, floor(ln(e.amt + 0.01) / {lb}) + o.o bk from e, (values (-2), (-1), (0), (1), (2)) o(o)),
                    b as (select card_id, es, amt, {e} v, floor(ln(amt + 0.01) / {lb}) bk from t where {e} in (select v from e))
                    select e5.TransactionID,
                           count(distinct b.card_id) filter (where b.card_id <> e5.card_id and abs(b.amt - e5.amt) <= {tol} * e5.amt) k
                    from e5 join b on b.v = e5.v and b.bk = e5.bk and b.es between e5.es - {s} and e5.es + {s} group by 1"""

    def rule(self, s, p):
        return s.k >= p["min_cards"]

    def text(self, p):
        amt = f" for amounts within {100 * p['amount_tol']:g}% of this one" if p["amount_tol"] is not None else ""
        rare = f" (only values seen on at most {RARE} cards)" if p["amount_tol"] is None or p["entity"] == "device_key" else ""
        return (f"the transaction's {p['entity']}{rare} was used by {p['min_cards']} or more other cards within "
                f"{p['days']} day(s){amt}")

    def slug(self, p):
        tol = "any" if p["amount_tol"] is None else f"tol{_num(100 * p['amount_tol'])}"
        return f"shared_{p['entity'].lower()}_k{p['min_cards']}_{p['days']}d_{tol}"

    def apply(self, ctx, spec):
        p, f = spec["params"], ctx.f
        e, tol = p["entity"], p["amount_tol"]
        if e == "device_key":
            v = _clean_device(ctx.device)
            if v is None or not (0 < int(ctx.device_pop or 0) <= RARE):
                return []
            tool, kw = "device_txns", {"device_key": v, "t0": ctx.t0, "days": p["days"]}
        else:
            v = f.get(e)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return []
            if e == "holder_key" and (pd.Timestamp(ctx.t0) >= pd.Timestamp(HOLDER_KEY_UNTIL) or "||" in str(v) or str(v).endswith("|")):
                return []
            if tol is None and _call(ctx, "grow_entity_popularity", entity=e, value=v) > RARE:
                return []
            tool, kw = "grow_entity_txns", {"entity": e, "value": int(v) if e == "addr1" else v, "t0": ctx.t0, "days": p["days"]}
        rows = _call(ctx, tool, **kw)
        others = rows[rows.card_id != ctx.card_id]
        if tol is not None:
            others = others[(others.amt - float(f["amt"])).abs() <= tol * float(f["amt"])]
        cards = sorted(others.card_id.unique())
        if len(cards) < p["min_cards"]:
            return []
        amt = f" for amounts within {100 * tol:g}% of ${float(f['amt']):,.2f}" if tol is not None else ""
        what = f"{e} {v} was used by {len(cards)} other cards within {p['days']} day(s){amt}"
        return [_signal(spec, self.group, what, cards[:10] + _ids(others.head(10)), tool, kw)]


class Dormancy(Family):
    """A card silent for gap_days or more wakes up and makes n or more transactions within `hours` of waking."""
    name = "dormancy"
    group = "sequence"
    keywords = ("dormant", "silent", "inactive", "weeks of silence", "wake", "woke")

    def grid(self):
        return [{"gap_days": g, "n": n, "hours": h} for g in (30, 45, 60, 90) for n in (1, 2, 3) for h in (24, 48, 72)]

    def stat_key(self, p):
        return (p["gap_days"], p["hours"])

    def stats_sql(self, p, ev):
        g, h = int(p["gap_days"] * 86400), int(p["hours"] * 3600)
        return f"""with e as (select t.TransactionID, t.card_id, t.es from {ev} x join t using (TransactionID)),
                    w as (select card_id, es wake from t where prev_es is not null and es - prev_es >= {g}
                          and card_id in (select card_id from e)),
                    ew as (select e.TransactionID, e.card_id, max(w.wake) wake from e join w on w.card_id = e.card_id
                           and w.wake <= e.es and e.es - w.wake <= {h} group by 1, 2)
                    select ew.TransactionID, count(*) k from ew join t on t.card_id = ew.card_id
                    and t.es between ew.wake and ew.wake + {h} group by 1"""

    def rule(self, s, p):
        return s.k >= p["n"]

    def text(self, p):
        return (f"a card silent for {p['gap_days']} days or more makes {p['n']} or more transactions within "
                f"{p['hours']} hours of waking up")

    def slug(self, p):
        return f"dormancy_{p['gap_days']}d_n{p['n']}_{p['hours']}h"

    def apply(self, ctx, spec):
        p = spec["params"]
        kw = {"card_id": ctx.card_id}
        h = _call(ctx, "card_history", **kw).sort_values(["ts", "TransactionID"]).reset_index(drop=True)
        if h.empty:
            return []
        es = h.ts.astype("int64") // 10**9
        h["gap"] = es.diff()
        t0 = int(pd.Timestamp(ctx.t0).value // 10**9)
        wakes = h[(h.gap >= p["gap_days"] * 86400) & (es <= t0) & (t0 - es <= p["hours"] * 3600)]
        if wakes.empty:
            return []
        w = wakes.iloc[-1]
        w_es = int(pd.Timestamp(w.ts).value // 10**9)
        run = h[(es >= w_es) & (es <= w_es + p["hours"] * 3600)]
        if len(run) < p["n"]:
            return []
        what = (f"card {ctx.card_id} was silent for {w.gap / 86400:.0f} days, then made {len(run)} transaction(s) within "
                f"{p['hours']} hours of waking on {pd.Timestamp(w.ts):%Y-%m-%d %H:%M}")
        return [_signal(spec, self.group, what, _ids(run), "card_history", kw)]


class Micro(Family):
    """n or more online charges under amount_lt on one card within +-days (the transaction itself is one)."""
    name = "micro"
    group = "sequence"
    keywords = ("micro", "too small", "small charges", "test charges", "authorizations under")

    def grid(self):
        return [{"amount_lt": a, "n": n, "days": d} for a in (1, 2, 5, 10) for n in (3, 5, 8) for d in (1, 7)]

    def builtin(self):
        return [("card_testing", {"amount_lt": 10, "n": 3, "days": 1 / 24})]

    def stat_key(self, p):
        return (p["amount_lt"], p["days"])

    def stats_sql(self, p, ev):
        s = int(p["days"] * 86400)
        return f"""with e as (select t.TransactionID, t.card_id, t.es from {ev} x join t using (TransactionID)
                              where t.channel = 'online' and t.amt < {p['amount_lt']}),
                    b as (select card_id, es from t where channel = 'online' and amt < {p['amount_lt']}
                          and card_id in (select card_id from e))
                    select e.TransactionID, count(*) k from e join b on b.card_id = e.card_id and b.es between e.es - {s} and e.es + {s}
                    group by 1"""

    def rule(self, s, p):
        return s.k >= p["n"]

    def text(self, p):
        return f"{p['n']} or more online charges under ${p['amount_lt']:g} on one card within {p['days']:g} day(s)"

    def slug(self, p):
        return f"micro_lt{_num(p['amount_lt'])}_n{p['n']}_{_num(p['days'])}d"

    def apply(self, ctx, spec):
        p, f = spec["params"], ctx.f
        if f.get("channel") != "online" or float(f["amt"]) >= p["amount_lt"]:
            return []
        kw = {"card_id": ctx.card_id, "t0": ctx.t0, "hours_before": p["days"] * 24, "hours_after": p["days"] * 24}
        w = _call(ctx, "card_window", **kw)
        b = w[(w.channel == "online") & (w.amt < p["amount_lt"])]
        if len(b) < p["n"]:
            return []
        what = f"{len(b)} online charges under ${p['amount_lt']:g} on card {ctx.card_id} within {p['days']:g} day(s)"
        return [_signal(spec, self.group, what, _ids(b), "card_window", kw)]


class FanIn(Family):
    """One recipient (email domain or region) paid by min_cards or more cards in `days` while its card-day rate over
    the previous eight weeks was below baseline_ratio times the current rate (a fan-in spike)."""
    name = "fanin"
    group = "network"
    keywords = ("recipient", "many cards", "paid", "same merchant", "one recipient", "fan-in")
    ENTITIES = ("R_emaildomain", "P_emaildomain", "addr1")

    def grid(self):
        return [{"entity": e, "min_cards": k, "days": d, "baseline_ratio": r}
                for e in self.ENTITIES for k in (3, 5, 10) for d in (1, 7) for r in (0.2, 0.5)]

    def stat_key(self, p):
        return (p["entity"], p["days"])

    def stats_sql(self, p, ev):
        e, d = p["entity"], int(p["days"])
        return f"""with e as (select x.TransactionID, t.{e} v, t.d from {ev} x join t using (TransactionID) where t.{e} is not null),
                    vd as (select distinct v, d from e),
                    dd as (select distinct {e} v, d, card_id from t where {e} in (select v from vd)),
                    dn as (select v, d, count(*) n from dd group by 1, 2),
                    cur as (select vd.v, vd.d, count(distinct dd.card_id) cards, count(*) cd from vd join dd
                            on dd.v = vd.v and dd.d between vd.d - {d - 1} and vd.d group by 1, 2),
                    pri as (select vd.v, vd.d, sum(dn.n) pcd from vd join dn
                            on dn.v = vd.v and dn.d between vd.d - {d + BASELINE_DAYS - 1} and vd.d - {d} group by 1, 2),
                    st as (select min(d) d0 from t)
                    select e.TransactionID, coalesce(cur.cards, 0) cards, coalesce(cur.cd, 0) cd, coalesce(pri.pcd, 0) pcd,
                           (e.d - st.d0) >= {d + BASELINE_DAYS} full_base
                    from e cross join st left join cur on cur.v = e.v and cur.d = e.d left join pri on pri.v = e.v and pri.d = e.d"""

    def rule(self, s, p):
        base = s.pcd * p["days"] / BASELINE_DAYS
        return s.full_base & (s.cards >= p["min_cards"]) & (base < p["baseline_ratio"] * s.cd)

    def text(self, p):
        return (f"the transaction's {p['entity']} was paid by {p['min_cards']} or more cards in {p['days']} day(s), while over the "
                f"previous {BASELINE_DAYS} days its rate was below {p['baseline_ratio']:g} times that (a fan-in spike)")

    def slug(self, p):
        return f"fanin_{p['entity'].lower()}_k{p['min_cards']}_{p['days']}d_r{_num(p['baseline_ratio'])}"

    def apply(self, ctx, spec):
        p, f = spec["params"], ctx.f
        e, d = p["entity"], int(p["days"])
        v = f.get(e)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return []
        v = int(v) if e == "addr1" else v
        day = pd.Timestamp(ctx.t0).normalize()
        rng = _call(ctx, "grow_date_range")
        if (day - pd.Timestamp(rng["d0"])).days < d + BASELINE_DAYS:
            return []
        kw = {"entity": e, "value": v, "d_from": str((day - pd.Timedelta(days=d + BASELINE_DAYS - 1)).date()), "d_to": str(day.date())}
        dd = _call(ctx, "grow_entity_daily_cards", **kw)
        dd = dd.assign(d=pd.to_datetime(dd.d))
        cur = dd[dd.d > day - pd.Timedelta(days=d)]
        pri = dd[dd.d <= day - pd.Timedelta(days=d)]
        cards, cd, pcd = cur.card_id.nunique(), len(cur), len(pri)
        if not (cards >= p["min_cards"] and pcd * d / BASELINE_DAYS < p["baseline_ratio"] * cd):
            return []
        what = (f"{e} {v} was paid by {cards} cards in the {d} day(s) up to {day:%Y-%m-%d} ({cd} card-days) against "
                f"{pcd} card-days in the previous {BASELINE_DAYS} days")
        return [_signal(spec, self.group, what, sorted(cur.card_id.unique())[:12], "grow_entity_daily_cards", kw)]


class RepeatAmount(Family):
    """n or more transactions of the same product and (nearly) the same amount on one card within +-minutes."""
    name = "repeat_amount"
    group = "sequence"
    keywords = ("same amount", "repeated", "identical", "duplicate")

    def grid(self):
        return [{"n": n, "minutes": m, "tol": tol} for n in (2, 3, 4) for m in (10, 60, 360) for tol in (0.0, 0.01, 0.05)]

    def builtin(self):
        return [("duplicate_amount_burst", {"n": 3, "minutes": 75, "tol": 0.01})]

    def stat_key(self, p):
        return (p["minutes"], p["tol"])

    def stats_sql(self, p, ev):
        s = int(p["minutes"] * 60)
        return f"""with e as (select t.TransactionID, t.card_id, t.es, t.amt, t.ProductCD from {ev} x join t using (TransactionID)),
                    b as (select card_id, es, amt, ProductCD from t where card_id in (select card_id from e))
                    select e.TransactionID, count(*) k from e join b on b.card_id = e.card_id and b.ProductCD = e.ProductCD
                    and b.es between e.es - {s} and e.es + {s} and abs(b.amt - e.amt) <= {p['tol']} * e.amt + 0.01 group by 1"""

    def rule(self, s, p):
        return s.k >= p["n"]

    def text(self, p):
        return (f"{p['n']} or more transactions of the same product within {100 * p['tol']:g}% (and one cent) of the same amount "
                f"on one card within {p['minutes']} minutes")

    def slug(self, p):
        return f"repeat_amount_n{p['n']}_{p['minutes']}m_tol{_num(100 * p['tol'])}"

    def apply(self, ctx, spec):
        p, f = spec["params"], ctx.f
        kw = {"card_id": ctx.card_id, "t0": ctx.t0, "hours_before": p["minutes"] / 60, "hours_after": p["minutes"] / 60}
        w = _call(ctx, "card_window", **kw)
        a = float(f["amt"])
        b = w[(w.ProductCD == f["ProductCD"]) & ((w.amt - a).abs() <= p["tol"] * a + 0.01)]
        if len(b) < p["n"]:
            return []
        what = f"{len(b)} {f['ProductCD']} transactions of about ${a:,.2f} on card {ctx.card_id} within {p['minutes']} minutes"
        return [_signal(spec, self.group, what, _ids(b), "card_window", kw)]


class NewDeviceProxy(Family):
    """A rare device profile used on min_cards or more cards within +-30 days, mostly marked New and behind a proxy."""
    name = "new_device_proxy"
    group = "network"
    keywords = ("anonymous proxy", "never seen on this account", "device", "proxy", "new device")

    def grid(self):
        return [{"min_cards": k, "new_share": ns, "anon_share": a}
                for k in (3, 5, 10) for ns in (0.5, 0.8, 0.95) for a in (0.0, 0.5, 0.8)]

    def builtin(self):
        return [("scripted_device_ring", {"min_cards": 6, "new_share": 0.8, "anon_share": 0.5})]

    def stat_key(self, p):
        return ()

    def stats_sql(self, p, ev):
        s = NDP_DAYS * 86400
        return f"""with e as (select x.TransactionID, t.device_key, t.es from {ev} x join t using (TransactionID)
                              join devpop dp on dp.device_key = t.device_key where dp.pop <= {RARE}),
                    b as (select device_key, card_id, es, id_15, id_23 from t where device_key in (select device_key from e))
                    select e.TransactionID, count(distinct b.card_id) cards,
                           avg(case when b.id_15 = 'New' then 1.0 else 0.0 end) new_share,
                           avg(case when b.id_23 like '%ANONYMOUS%' then 1.0 else 0.0 end) anon_share
                    from e join b on b.device_key = e.device_key and b.es between e.es - {s} and e.es + {s} group by 1"""

    def rule(self, s, p):
        return (s.cards >= p["min_cards"]) & (s.new_share >= p["new_share"]) & (s.anon_share >= p["anon_share"])

    def text(self, p):
        return (f"a rare device profile (at most {RARE} cards ever) used on {p['min_cards']} or more cards within {NDP_DAYS} days, "
                f"with at least {100 * p['new_share']:g}% of its transactions marked New for the account and at least "
                f"{100 * p['anon_share']:g}% behind an anonymous proxy")

    def slug(self, p):
        return f"new_device_proxy_k{p['min_cards']}_new{_num(100 * p['new_share'])}_anon{_num(100 * p['anon_share'])}"

    def apply(self, ctx, spec):
        p = spec["params"]
        v = _clean_device(ctx.device)
        if v is None or not (0 < int(ctx.device_pop or 0) <= RARE):
            return []
        kw = {"device_key": v, "t0": ctx.t0, "days": NDP_DAYS}
        rows = _call(ctx, "device_txns", **kw)
        if rows.empty:
            return []
        cards = sorted(rows.card_id.unique())
        ns = float((rows.id_15 == "New").mean())
        an = float(rows.id_23.fillna("").astype(str).str.contains("ANONYMOUS").mean())
        if not (len(cards) >= p["min_cards"] and ns >= p["new_share"] and an >= p["anon_share"]):
            return []
        what = (f"device profile {v} was used on {len(cards)} cards within {NDP_DAYS} days, {ns:.0%} of its transactions marked "
                f"New and {an:.0%} behind an anonymous proxy")
        return [_signal(spec, self.group, what, cards[:12], "device_txns", kw)]


FAMILIES: dict[str, Family] = {f.name: f for f in (BandBurst(), SharedEntity(), Dormancy(), Micro(), FanIn(), RepeatAmount(),
                                                   NewDeviceProxy())}


def apply(ctx, spec: dict) -> list[Signal]:
    """Run one approved detector spec on a case context (runtime)."""
    return FAMILIES[spec["family"]].apply(ctx, spec)
