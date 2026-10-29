"""TigerGraph lane: connection, schema setup, installed-query calls, write-back.

TgTools exposes the same tool names and return shapes as kavach.duckq, backed by
installed GSQL queries (see queries.gsql), so the agent runs unchanged on either lane.
"""
from datetime import timedelta

import pandas as pd

from kavach.config import env

TXN_MAP = {
    "id": "TransactionID", "amt": "amt", "product_cd": "ProductCD", "channel": "channel", "risk_score": "risk_score",
    "addr1": "addr1", "addr2": "addr2", "dist1": "dist1", "p_email": "P_emaildomain", "r_email": "R_emaildomain",
    "m1": "M1", "m2": "M2", "m3": "M3", "m4": "M4", "m5": "M5", "m6": "M6", "m7": "M7", "m8": "M8", "m9": "M9",
    "c1": "C1", "c2": "C2", "c13": "C13", "c14": "C14", "d1": "D1", "d2": "D2", "d3": "D3", "d4": "D4", "d10": "D10", "d15": "D15",
    "proxy_type": "id_23", "device_type": "DeviceType", "card_id": "card_id", "customer_id": "customer_id",
    "device_key": "device_key", "id_15": "id_15", "id_31": "id_31", "holder_key": "holder_key",
    "card1": "card1", "card4": "card4", "card6": "card6", "ts": "ts",
}


def connect():
    import pyTigerGraph as tg

    host, graph = env("TG_HOST"), env("TG_GRAPH", "Fraud")
    user, pwd, secret = env("TG_USERNAME"), env("TG_PASSWORD"), env("TG_SECRET")
    if not host or not ((user and pwd) or secret):
        raise RuntimeError("TigerGraph credentials missing in .env (TG_HOST + TG_USERNAME/TG_PASSWORD or TG_SECRET)")
    conn = tg.TigerGraphConnection(host=host, graphname=graph, username=user or "tigergraph", password=pwd or "",
                                   gsqlSecret=secret or "", tgCloud=True)
    if secret:
        conn.getToken(secret)
    else:
        conn.getToken(conn.createSecret())
    return conn


def _ts(t):
    return pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def _vertices_to_txn_frame(vs: list) -> pd.DataFrame:
    rows = []
    for v in vs:
        a = dict(v.get("attributes", {}))
        a["id"] = v["v_id"]
        rows.append({TXN_MAP.get(k, k): val for k, val in a.items() if k in TXN_MAP})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["TransactionID"] = df.TransactionID.astype("int64")
    df["ts"] = pd.to_datetime(df.ts)
    for c in ("device_key", "P_emaildomain", "R_emaildomain", "id_23", "id_15", "id_31"):
        if c in df:
            df[c] = df[c].replace("", None)
    return df.sort_values(["ts", "TransactionID"]).reset_index(drop=True)


# ---- Round 2 typology mirrors: helpers shared by the TgTools methods below (pure, unit-tested offline) ----
NO_MATCH = "<none>"          # a set member no email domain, account key or signature can equal (an empty set is not sent)
_M_BOOL = ("M1", "M2", "M3", "M5", "M6", "M7", "M8", "M9")    # boolean in the DuckDB index, "true"/"false"/"" on the graph


def duck_txn_columns() -> list[str]:
    """Column names of kavach.duckq.TXN_COLS, in order (the alias of every select item)."""
    from kavach.duckq import TXN_COLS
    return [c.strip().split()[-1] for c in TXN_COLS.replace("\n", " ").split(",")]


def duck_txn_frame(vs: list) -> pd.DataFrame:
    """Transaction vertices as the DuckDB lane returns `select TXN_COLS from txn`: same columns in the same order, nulls
    restored where the graph stores a placeholder ("" for a missing string, 0 for a missing region; no real addr1 or
    addr2 is 0), M flags as booleans. dist2 is not on the graph (always null here); a placeholder device key ("| | |")
    is null on the graph (the loader drops it), as on every other graph tool."""
    cols = duck_txn_columns()
    df = _vertices_to_txn_frame(vs)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["ts"] = df.ts.astype("datetime64[us]")
    for c in ("DeviceType", "holder_key", "card4", "card6", "M4", "customer_id"):
        if c in df:
            df[c] = df[c].replace("", None)
    for c in ("addr1", "addr2"):
        if c in df:
            df[c] = pd.array([None if (x is None or x != x or int(x) == 0) else int(x) for x in df[c]], dtype="Int32")
    for c in _M_BOOL:
        if c in df:
            df[c] = pd.array([{"true": True, "false": False}.get(str(x).lower()) for x in df[c]], dtype="boolean")
    return df.reindex(columns=cols)


def band_bounds(label: str) -> tuple[float, float] | None:
    """(lo, hi) of a kavach.duckq_ext band label ("350-500" -> 350 <= amt < 500; "0-5" is everything below 5, as the SQL
    case expression has no lower edge; "2500+" has no upper edge); None for a label band_label() never produces."""
    from kavach.duckq_ext import BAND_EDGES
    lo = None
    for e in BAND_EDGES:
        if label == f"{lo or 0}-{e}":
            return (-1e300 if lo is None else float(lo), float(e))
        lo = e
    return (float(lo), 1e300) if label == f"{lo}+" else None


def entity_match_args(entity_field: str, value) -> dict:
    """Arguments of entity_neighbors_attr / entity_baseline_any that select exactly the rows whose DuckDB entity expression
    (kavach.duckq_ext.ENTITY_FIELDS) equals `value`; field "none" when no row can (empty value, a region that is not a
    canonical integer, an unknown band, a signature without a proxy, an invalid account key)."""
    from kavach.duckq_ext import ENTITY_FIELDS
    if entity_field not in ENTITY_FIELDS:
        raise ValueError(f"unknown entity field {entity_field!r}; allowed: {sorted(ENTITY_FIELDS)}")
    v = str(value)
    none = {"field": "none", "s1": "", "s2": "", "s3": "", "s4": "", "ival": -1, "lo": 0.0, "hi": 0.0}
    args = dict(none, field=entity_field)
    if entity_field in ("R_emaildomain", "P_emaildomain"):
        return dict(args, s1=v) if v else none
    if entity_field == "addr1":
        ok = v.lstrip("-").isdigit() and str(int(v)) == v and int(v) != 0
        return dict(args, ival=int(v)) if ok else none
    if entity_field == "product_band":
        prod, _, label = v.partition("|")
        b = band_bounds(label) if prod else None
        return dict(args, s1=prod, lo=b[0], hi=b[1]) if b else none
    if entity_field == "access_signature":
        parts = v.split("|")
        if len(parts) != 4 or not parts[0] or not parts[1]:
            return none
        return dict(args, s1=parts[0], s2=parts[1], s3=parts[2], s4=parts[3])
    # holder_key: the SQL keeps only keys without an empty part ("||") and without an empty last part
    return dict(args, s1=v) if v and "||" not in v and not v.endswith("|") else none


ATYP_EMPTY_COLS = ["TransactionID", "card_id", "ts", "amt", "ProductCD", "channel", "prior_n", "product_unseen",
                   "amt_above_prior_max", "amt_above_product_max", "device_new", "proxy_unseen", "hours_since_prev", "atypical"]
ATYP_COLS = ["TransactionID", "card_id", "ts", "amt", "ProductCD", "channel", "addr1", "P_emaildomain", "R_emaildomain",
             "device_key", "id_15", "id_23", "DeviceType", "risk_score", "holder_key", "prior_n", "prior_max", "product_unseen",
             "amt_above_prior_max", "amt_above_product_max", "device_new", "proxy_unseen", "hours_since_prev", "atypical"]


def atypical_frame(vs: list) -> pd.DataFrame:
    """card_atypical_window rows from the query's window vertices and their accumulators over the same card's earlier rows,
    with the flags computed exactly as the SQL of kavach.duckq_ext.card_atypical_window."""
    df = duck_txn_frame(vs)
    if df.empty:
        return pd.DataFrame(columns=ATYP_COLS)

    def acc(a, k):
        return a.get("@" + k, a.get("W.@" + k))
    accs = pd.DataFrame([{"TransactionID": int(v["v_id"]), "prior_n": int(acc(v["attributes"], "prior_n")),
                          "psp": int(acc(v["attributes"], "prior_same_product")), "pproxy": int(acc(v["attributes"], "prior_proxy")),
                          "pmax": float(acc(v["attributes"], "prior_max")), "pmaxp": float(acc(v["attributes"], "prior_max_product")),
                          "prev": acc(v["attributes"], "prev_ts")} for v in vs])
    df = df.merge(accs, on="TransactionID", how="left")
    n, psp = df.prior_n.astype("int64"), df.psp.astype("int64")
    df["prior_n"] = n
    df["prior_max"] = df.pmax.where(n > 0)
    pmaxp = df.pmaxp.where(psp > 0)
    prev = pd.to_datetime(df.prev).astype("datetime64[us]").where(n > 0)
    df["product_unseen"] = ((psp == 0) & (n >= 5)).astype(bool)
    df["amt_above_prior_max"] = ((n >= 5) & (df.amt > df.prior_max)).astype(bool)
    df["amt_above_product_max"] = ((psp >= 3) & (df.amt > pmaxp)).astype(bool)
    df["device_new"] = (df.id_15.astype(object) == "New").astype(bool)
    df["proxy_unseen"] = (df.id_23.notna() & (df.pproxy == 0) & (n >= 5)).astype(bool)
    df["hours_since_prev"] = (df.ts - prev).dt.total_seconds() / 3600.0
    df["atypical"] = df.product_unseen | df.amt_above_prior_max | df.device_new | df.proxy_unseen
    return df.sort_values(["card_id", "ts", "TransactionID"]).reset_index(drop=True)[ATYP_COLS]


def merged(result: list) -> dict:
    """The PRINT objects of one installed-query response merged into one dict."""
    out = {}
    for x in result or []:
        out.update(x)
    return out


def shares_frame(result: list, values) -> pd.DataFrame:
    """entity_shares rows (value, cards, active, share) from entity_shares_any: requested values that occur, sorted."""
    out = merged(result)
    found = {str(k): int(v) for k, v in (out.get("n") or {}).items()}
    found.update({str(int(k)): int(v) for k, v in (out.get("ni") or {}).items()})
    rows = sorted((k, found[k]) for k in {str(v) for v in values} if k in found)
    df = pd.DataFrame(rows, columns=["value", "cards"])
    df["cards"] = df.cards.astype("int64")
    df["active"] = pd.Series([int(out.get("active") or 0)] * len(df), dtype="int64")
    df["share"] = (df.cards / df.active).round(6) if len(df) else []
    return df


def baseline_dict(result: list, entity_field: str, value, t0, days: float, b0, n_b: int, data_start) -> dict:
    """entity_baseline's dict from entity_baseline_any, exactly as kavach.duckq_ext.entity_baseline finishes it."""
    out = merged(result)
    per = {int(k): int(v) for k, v in (out.get("per") or {}).items()}
    complete = [i for i in range(n_b) if b0 + timedelta(days=i * days) >= data_start]
    base = sum(per.get(i, 0) for i in complete) / len(complete) if complete else None
    k, n = int(out.get("cards_window") or 0), int(out.get("active_window") or 0)
    return {"entity_field": entity_field, "value": str(value), "t0": str(t0), "days": days,
            "cards_window": k, "active_window": n, "share_window": round(k / n, 6) if n else 0.0,
            "baseline_per_window": None if base is None else round(base, 3), "baseline_buckets": len(complete)}


class TgTools:
    def __init__(self, conn=None):
        self.conn = conn or connect()

    # installed-query parameters that are typed vertices (JSON {"id": ...}) or sets of them
    VERTEX_PARAMS = {"c", "cu", "d", "t", "r", "e", "h"}
    VERTEX_SET_PARAMS = {"cards", "txns", "devices"}

    def _q(self, name: str, **params):
        body = {}
        for k, v in params.items():
            if k in self.VERTEX_PARAMS:
                body[k] = {"id": str(v)}
            elif k in self.VERTEX_SET_PARAMS:
                body[k] = [{"id": str(x)} for x in v]
            else:
                body[k] = v
        return self.conn.runInstalledQuery(name, params=body, timeout=120_000, usePost=True)

    def card_history(self, card_id: str) -> pd.DataFrame:
        return _vertices_to_txn_frame(self._q("card_history", c=card_id)[0]["T"])

    def card_window(self, card_id: str, t0, hours_before: float = 48, hours_after: float = 48) -> pd.DataFrame:
        t0 = pd.Timestamp(t0)
        r = self._q("card_window", c=card_id, t_from=_ts(t0 - timedelta(hours=hours_before)),
                    t_to=_ts(t0 + timedelta(hours=hours_after)))
        return _vertices_to_txn_frame(r[0]["T"])

    def txn_detail(self, txn_id) -> dict:
        df = _vertices_to_txn_frame(self._q("txn_detail", t=str(txn_id))[0]["S"])
        return df.iloc[0].to_dict() if len(df) else {}

    def customer_cards(self, customer_id: str) -> pd.DataFrame:
        vs = self._q("customer_cards", cu=customer_id)[0]["C"]
        return pd.DataFrame([{"card_id": v["v_id"], "n": v["attributes"]["C.@n"],
                              "first_ts": pd.Timestamp(v["attributes"]["C.@first_ts"]), "last_ts": pd.Timestamp(v["attributes"]["C.@last_ts"]),
                              "card4": v["attributes"]["C.card4"], "card6": v["attributes"]["C.card6"]} for v in vs]).sort_values("card_id")

    def _agg(self, vs, extra=()):
        rows = []
        for v in vs:
            a = v["attributes"]
            r = {"card_id": v["v_id"], "customer_id": v["v_id"].split("-")[0], "n": a["C.@n"], "first_ts": pd.Timestamp(a["C.@first_ts"]),
                 "last_ts": pd.Timestamp(a["C.@last_ts"]), "amt": a["C.@amt"], "mean_risk": a["C.@mean_risk"]}
            for k, name in extra:
                r[name] = a[k]
            rows.append(r)
        df = pd.DataFrame(rows)
        return df.sort_values("first_ts").reset_index(drop=True) if len(df) else df

    def device_neighbors(self, device_key: str, t0=None, days: float = 30) -> pd.DataFrame:
        t0 = pd.Timestamp(t0) if t0 is not None else pd.Timestamp("2016-10-01")
        d = days if t0 is not None else 400
        r = self._q("device_neighbors", d=device_key, t_from=_ts(t0 - timedelta(days=d)), t_to=_ts(t0 + timedelta(days=d)))
        return self._agg(r[0]["C"], [("C.@n_new", "n_new")])

    def device_txns(self, device_key: str, t0=None, days: float = 30) -> pd.DataFrame:
        t0 = pd.Timestamp(t0) if t0 is not None else pd.Timestamp("2016-10-01")
        r = self._q("device_txns", d=device_key, t_from=_ts(t0 - timedelta(days=days)), t_to=_ts(t0 + timedelta(days=days)))
        return _vertices_to_txn_frame(r[0]["T"])

    def device_popularity(self, device_key: str) -> int:
        r = self._q("device_popularity", d=device_key)[0]["S"]
        return int(r[0]["attributes"]["S.n_cards"]) if r else 0

    def region_activity(self, addr1: int, t0, days: float = 7) -> pd.DataFrame:
        t0 = pd.Timestamp(t0)
        r = self._q("region_activity", r=str(int(addr1)), t_from=_ts(t0 - timedelta(days=days)), t_to=_ts(t0 + timedelta(days=days)))
        df = self._agg(r[0]["C"], [("C.@seen_before", "seen_before")])
        if len(df):
            df["first_time"] = ~df.pop("seen_before").astype(bool)
        return df

    def email_neighbors(self, domain: str, t0, days: float = 7, field: str = "R_emaildomain") -> pd.DataFrame:
        t0 = pd.Timestamp(t0)
        r = self._q("email_neighbors_r" if field == "R_emaildomain" else "email_neighbors_p", e=domain,
                    t_from=_ts(t0 - timedelta(days=days)), t_to=_ts(t0 + timedelta(days=days)))
        return self._agg(r[0]["C"])

    def holder_cards(self, card_id: str) -> pd.DataFrame:
        vs = self._q("holder_cards", c=card_id)[0]["C"]
        return pd.DataFrame([{"card_id": v["v_id"], "customer_id": v["v_id"].split("-")[0], "n": v["attributes"]["C.@n"]} for v in vs])

    def closed_cases_for(self, card_ids=(), customer_ids=(), txn_ids=(), device_keys=()) -> pd.DataFrame:
        cards = list(card_ids)
        for cu in customer_ids:
            cards += list(self.customer_cards(cu).card_id)
        ph = lambda xs: sorted({str(x) for x in xs}) or [""]   # noqa: E731  (empty sets are sent as {""}: matches no vertex)
        r = self._q("closed_cases_for", card_ids=ph(cards), txn_ids=ph(txn_ids), device_ids=ph(device_keys))
        rows = []
        for v in r[0]["R"]:
            a = v["attributes"]
            cards, conn = sorted(a.get("R.@card", [])), sorted(a.get("R.@conn", []))
            rows.append({"case_id": v["v_id"], "via": ",".join(sorted(a["R.@via"])), "card_id": cards[0] if cards else None,
                         "outcome": a["R.outcome"], "pattern": a["R.pattern"],
                         "opened_at": a["R.opened_at"], "n_txns": a["R.n_txns"], "exposure_usd": a["R.exposure_usd"],
                         "connected_card_ids": "|".join(conn) if conn else None,
                         "report_filed": a["R.report_filed"], "analyst_notes": a["R.analyst_notes"]})
        cols = ["case_id", "via", "card_id", "outcome", "pattern", "opened_at", "n_txns", "exposure_usd", "connected_card_ids",
                "report_filed", "analyst_notes"]
        df = pd.DataFrame(rows, columns=cols)
        return df.sort_values("opened_at").reset_index(drop=True) if len(df) else df

    def holder_fraud_history(self, holder_keys, before) -> pd.DataFrame:
        keys = [k for k in holder_keys if k and not k.endswith("|")]
        if not keys:
            return pd.DataFrame(columns=["case_id", "outcome", "pattern", "opened_at", "holder_key"])
        r = self._q("holder_fraud_history", keys=keys, before=_ts(before))
        rows = [{"case_id": v["v_id"], "outcome": v["attributes"]["X.outcome"], "pattern": v["attributes"]["X.pattern"],
                 "opened_at": v["attributes"]["X.opened_at"], "holder_key": hk}
                for v in r[0]["X"] for hk in v["attributes"]["X.@hk"]]
        df = pd.DataFrame(rows, columns=["case_id", "outcome", "pattern", "opened_at", "holder_key"])
        return df.sort_values("opened_at").reset_index(drop=True)

    def amount_peers(self, product_cd: str, amt: float, t0, days: float = 7, tol: float = 0.01,
                     email: str | None = None, email_field: str = "P_emaildomain") -> pd.DataFrame:
        t0 = pd.Timestamp(t0)
        if not email:
            # no email anchor (micro_charges_peers, amount_ladder_ring): the same amount on any card (queries_r2.gsql)
            r = self._q("amount_peers_any", product=product_cd, lo=amt * (1 - tol), hi=amt * (1 + tol),
                        t_from=_ts(t0 - timedelta(days=days)), t_to=_ts(t0 + timedelta(days=days)))
            return duck_txn_frame(r[0]["T"])
        r = self._q("amount_peers_r" if email_field == "R_emaildomain" else "amount_peers_p", e=email, product=product_cd,
                    lo=amt * (1 - tol), hi=amt * (1 + tol), t_from=_ts(t0 - timedelta(days=days)), t_to=_ts(t0 + timedelta(days=days)))
        return _vertices_to_txn_frame(r[0]["T"])

    def small_online_txns(self, t_from, t_to, max_amt: float = 6.0) -> pd.DataFrame:
        """Same rows as kavach.duckq_ext.small_online_txns, from the installed band_txns query (online, lo <= amt < hi)."""
        r = self._q("band_txns", lo=0.0, hi=float(max_amt), t_from=_ts(t_from), t_to=_ts(t_to))
        df = pd.DataFrame([{"TransactionID": int(v["v_id"]), "card_id": v["attributes"]["T.card_id"],
                            "ts": pd.Timestamp(v["attributes"]["T.ts"]), "amt": float(v["attributes"]["T.amt"])} for v in r[0]["T"]],
                          columns=["TransactionID", "card_id", "ts", "amt"])
        return df.sort_values(["ts", "TransactionID"]).reset_index(drop=True)

    def band_bursts(self, amt_lo: float, amt_hi: float, t0, days: float = 30, minutes: int = 40, min_n: int = 3) -> pd.DataFrame:
        t0 = pd.Timestamp(t0)
        r = self._q("band_txns", lo=amt_lo, hi=amt_hi, t_from=_ts(t0 - timedelta(days=days)), t_to=_ts(t0 + timedelta(days=days)))
        df = pd.DataFrame([{"card_id": v["attributes"]["T.card_id"], "ts": pd.Timestamp(v["attributes"]["T.ts"]),
                            "amt": v["attributes"]["T.amt"]} for v in r[0]["T"]])
        out = []
        for card, g in (df.groupby("card_id") if len(df) else []):
            g = g.sort_values("ts")
            k = max(((g.ts >= t - pd.Timedelta(minutes=minutes)) & (g.ts <= t)).sum() for t in g.ts)
            if k >= min_n:
                out.append({"card_id": card, "first_ts": g.ts.min(), "last_ts": g.ts.max(), "n": k, "amt": g.amt.sum()})
        return pd.DataFrame(out, columns=["card_id", "first_ts", "last_ts", "n", "amt"]).sort_values("first_ts") if out else             pd.DataFrame(columns=["card_id", "first_ts", "last_ts", "n", "amt"])

    def similar_cases(self, **kw) -> list:
        # fingerprint retrieval over the closed cases; vector retrieval over analyst notes lives in kavach.vectors
        from kavach import recall
        return recall.similar(**kw)

    def vector_search_cases(self, text: str, k: int = 8) -> list:
        """TigerVector search over ClosedCase.emb (analyst-note embeddings)."""
        from kavach.graph import vectors_tg
        return vectors_tg.search_closed(self.conn, text, k)

    def vector_search_docs(self, text: str, k: int = 2) -> list:
        """TigerVector search over PolicyDoc.emb (README patterns and Fraud Policy sections)."""
        from kavach.graph import vectors_tg
        return vectors_tg.search_docs(self.conn, text, k)

    def case_memory(self, card_ids) -> list[dict]:
        """Earlier InvestigationCases written back by the agent that touch these cards (graph memory)."""
        out = []
        for cid in card_ids:
            for v in self._q("case_memory", c=cid)[0]["I"]:
                out.append({"id": v["v_id"], "case_id": v["attributes"].get("case_id"), "verdict": v["attributes"].get("verdict"),
                            "pattern": v["attributes"].get("pattern"), "card_id": cid})
        return out

    def similar_investigations(self, text: str, k: int = 3) -> list[dict]:
        """Earlier InvestigationCases with the closest summaries (TigerVector)."""
        from kavach.graph import vectors_tg
        return vectors_tg.search_investigations(self.conn, text, k)

    # ---- grown-detector tools (same names and shapes as kavach/grow/gtools.py; queries in queries_grow.gsql) ----
    _ENTITY_Q = {"R_emaildomain": ("r", "e"), "P_emaildomain": ("p", "e"), "addr1": ("region", "r"), "holder_key": ("holder", "h")}

    def _entity_args(self, entity: str, value) -> tuple[str, dict]:
        suffix, param = self._ENTITY_Q[entity]
        if entity == "addr1":
            return suffix, {"r": str(int(float(value)))}
        if entity == "holder_key":
            return suffix, {"h": str(value), "key": str(value)}
        return suffix, {"e": str(value)}

    def grow_entity_txns(self, entity: str, value, t0, days: float = 7) -> pd.DataFrame:
        t0 = pd.Timestamp(t0)
        rng = {"t_from": _ts(t0 - timedelta(days=days)), "t_to": _ts(t0 + timedelta(days=days))}
        if entity == "device_key":
            return self.device_txns(value, t0, days)
        suffix, args = self._entity_args(entity, value)
        return _vertices_to_txn_frame(self._q(f"entity_txns_{suffix}", **args, **rng)[0]["T"])

    def grow_entity_popularity(self, entity: str, value) -> int:
        if entity == "device_key":
            return self.device_popularity(value)
        suffix, args = self._entity_args(entity, value)
        return int(self._q(f"entity_cards_{suffix}", **args)[0]["n"])

    def grow_entity_daily_cards(self, entity: str, value, d_from, d_to) -> pd.DataFrame:
        rng = {"t_from": _ts(pd.Timestamp(d_from).normalize()), "t_to": _ts(pd.Timestamp(d_to).normalize() + timedelta(days=1))}
        if entity == "device_key":
            pairs = self._q("entity_daily_cards_device", d=str(value), **rng)[0]["pairs"]
        elif entity == "holder_key":
            t = self.grow_entity_txns(entity, value, pd.Timestamp(d_from) + (pd.Timestamp(d_to) - pd.Timestamp(d_from)) / 2,
                                      days=(pd.Timestamp(d_to) - pd.Timestamp(d_from)).days / 2 + 1)
            pairs = [f"{pd.Timestamp(r.ts):%Y-%m-%d}|{r.card_id}" for r in t.itertuples()] if len(t) else []
        else:
            suffix, args = self._entity_args(entity, value)
            pairs = self._q(f"entity_daily_cards_{suffix}", **args, **rng)[0]["pairs"]
        rows = sorted({tuple(p.split("|", 1)) for p in pairs})
        df = pd.DataFrame(rows, columns=["d", "card_id"])
        df["d"] = pd.to_datetime(df.d).dt.date if len(df) else df.d
        lo, hi = pd.Timestamp(d_from).date(), pd.Timestamp(d_to).date()
        return df[(df.d >= lo) & (df.d <= hi)].reset_index(drop=True) if len(df) else df

    def grow_date_range(self) -> dict:
        if not hasattr(self, "_date_range"):
            r = self._q("txn_date_range")
            d0 = next(x["d0"] for x in r if "d0" in x)
            d1 = next(x["d1"] for x in r if "d1" in x)
            # same string form as the DuckDB lane (a date cast read back by pandas prints as a midnight timestamp)
            self._date_range = {"d0": str(pd.Timestamp(pd.Timestamp(d0).date())), "d1": str(pd.Timestamp(pd.Timestamp(d1).date()))}
        return self._date_range

    # ---- typology tools (same names, columns and values as kavach/duckq_ext.py; queries in queries_r2.gsql) ----
    _NEIGHBOR_Q = {"R_emaildomain": "entity_neighbors_r", "P_emaildomain": "entity_neighbors_p", "addr1": "entity_neighbors_region"}

    def entity_neighbors(self, entity_field: str, value, t_from, t_to) -> pd.DataFrame:
        """Every transaction (any card) whose entity value equals `value` in [t_from, t_to], with the value in column `entity`.
        Vertex entities (email domains, billing regions) are a one-hop traversal; attribute entities a filtered scan."""
        args = entity_match_args(entity_field, value)          # validates the field like the DuckDB lane
        rng = {"t_from": _ts(t_from), "t_to": _ts(t_to)}
        if entity_field in self._NEIGHBOR_Q:
            vs = self._q(self._NEIGHBOR_Q[entity_field], vals=[str(value)], **rng)[0]["T"]
        elif args["field"] == "none":
            vs = []                                             # no row can carry this value (see entity_match_args)
        else:
            vs = self._q("entity_neighbors_attr", **{k: v for k, v in args.items() if k != "ival"}, **rng)[0]["T"]
        df = duck_txn_frame(vs)
        df["entity"] = pd.Series([str(value)] * len(df), dtype=object)
        return df

    def card_atypical_window(self, card_id, t_from, t_to) -> pd.DataFrame:
        """The card's (or several cards') window transactions with the "atypical for this card" flags; the aggregates over
        each card's earlier transactions come from the graph (two hops: transaction, card, earlier transactions)."""
        cards = [card_id] if isinstance(card_id, str) else list(card_id)
        if not cards:
            return pd.DataFrame(columns=ATYP_EMPTY_COLS)
        r = self._q("card_atypical_window", card_ids=sorted({str(c) for c in cards}), t_from=_ts(t_from), t_to=_ts(t_to))
        return atypical_frame(r[0]["W"])

    def _txn_start(self) -> pd.Timestamp:
        """First transaction timestamp (the DuckDB lane's min(ts)), from the installed txn_date_range query."""
        if not hasattr(self, "_t_start"):
            self._t_start = pd.Timestamp(next(x["d0"] for x in self._q("txn_date_range") if "d0" in x))
        return self._t_start

    def entity_baseline(self, entity_field: str, value, t0, days: float) -> dict:
        args = entity_match_args(entity_field, value)
        t0 = pd.Timestamp(pd.Timestamp(t0).to_pydatetime())
        w0 = t0 - timedelta(days=days)
        n_b = max(1, int(90 // days))
        b0 = w0 - timedelta(days=n_b * days)
        r = self._q("entity_baseline_any", **args, b0=_ts(b0), w0=_ts(w0), t0=_ts(t0), bucket_s=float(days) * 86400.0)
        return baseline_dict(r, entity_field, value, t0, days, b0, n_b, self._txn_start())

    def entity_shares(self, entity_field: str, values, t0, days: float) -> pd.DataFrame:
        entity_match_args(entity_field, "")                     # validates the field
        vals = [str(v) for v in values]
        if not vals:
            return pd.DataFrame(columns=["value", "cards", "active", "share"])
        t0 = pd.Timestamp(t0)
        w0 = t0 - timedelta(days=days)
        if entity_field == "addr1":
            svals = [NO_MATCH]
            ivals = sorted({entity_match_args("addr1", v)["ival"] for v in vals} - {-1}) or [-1]
        else:
            # only values the SQL entity expression can produce: an empty string or an invalid account key would match
            # the graph's "" placeholders or raw keys
            svals = sorted({v for v in vals if entity_match_args(entity_field, v)["field"] != "none"}) or [NO_MATCH]
            ivals = [-1]
        r = self._q("entity_shares_any", field=entity_field, vals=svals, ivals=ivals, t_from=_ts(w0), t_to=_ts(t0))
        return shares_frame(r, vals)

    def card_profile(self, card_id: str, before=None) -> dict:
        from kavach import duckq
        h = self.card_history(card_id)
        return duckq.profile_from_history(card_id, h, before)

    def recurring_check(self, card_id: str, amt: float, product_cd: str, before=None, tol: float = 0.02) -> pd.DataFrame:
        from kavach import duckq
        return duckq.recurring_from_history(self.card_history(card_id), amt, product_cd, before, tol)

    def closed_case(self, case_id: str) -> dict:
        r = self.conn.getVerticesById("ClosedCase", case_id)
        return r[0]["attributes"] if r else {}
