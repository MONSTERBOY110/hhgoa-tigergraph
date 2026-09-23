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


class TgTools:
    def __init__(self, conn=None):
        self.conn = conn or connect()

    # installed-query parameters that are typed vertices (JSON {"id": ...}) or sets of them
    VERTEX_PARAMS = {"c", "cu", "d", "t", "r", "e"}
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
        r = self._q("closed_cases_for", cards=sorted(set(cards)), txns=[str(t) for t in txn_ids], devices=list(device_keys))
        rows = []
        for v in r[0]["R"]:
            a = v["attributes"]
            rows.append({"case_id": v["v_id"], "via": ",".join(sorted(a["R.@via"])), "outcome": a["R.outcome"], "pattern": a["R.pattern"],
                         "opened_at": a["R.opened_at"], "n_txns": a["R.n_txns"], "exposure_usd": a["R.exposure_usd"],
                         "report_filed": a["R.report_filed"], "analyst_notes": a["R.analyst_notes"]})
        df = pd.DataFrame(rows)
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
            raise ValueError("amount_peers on the TigerGraph lane needs an email domain to anchor the traversal")
        r = self._q("amount_peers_r" if email_field == "R_emaildomain" else "amount_peers_p", e=email, product=product_cd,
                    lo=amt * (1 - tol), hi=amt * (1 + tol), t_from=_ts(t0 - timedelta(days=days)), t_to=_ts(t0 + timedelta(days=days)))
        return _vertices_to_txn_frame(r[0]["T"])

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
