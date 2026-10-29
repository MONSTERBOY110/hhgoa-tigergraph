"""Offline checks of the TigerGraph mirrors of the typology tools (kavach/graph/client.py, queries_r2.gsql).

A synthetic transaction table is loaded twice: into an in-memory DuckDB (the real kavach.duckq_ext SQL runs on it) and
into a fake TigerGraph connection that answers the installed queries the way the GSQL in queries_r2.gsql computes them,
over the graph's encoding of the same rows ("" for a null string, 0 for a null number, "true"/"false" M flags, a
placeholder device key dropped). Each TgTools mirror must then return exactly what the DuckDB tool returns.
The live comparison on the workspace is in docs/round2/STATUS.md (tg-mirror entries).
"""
import re
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd
import pytest

from kavach import duckq, duckq_ext
from kavach.graph import client as gc
from kavach.graph.setup import GDIR

T0 = datetime(2016, 9, 1)


def _rows(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    cards = [f"C{c:05d}-K1" for c in range(1, 13)]
    out, tid = [], 3000000
    for i, card in enumerate(cards):
        t = T0 + timedelta(hours=int(rng.integers(0, 48)))
        for _ in range(int(rng.integers(8, 30))):
            tid += 1
            # ties: sometimes the same second as the previous row of the card (ordering falls back to TransactionID)
            t = t if rng.random() < 0.1 else t + timedelta(hours=float(rng.choice([0.2, 3, 30, 200])))
            prod = str(rng.choice(["C", "H", "R", "W", "W"]))
            proxied = prod != "W" and rng.random() < 0.4
            out.append({
                "TransactionID": tid, "card_id": card, "customer_id": card.split("-")[0], "ts": t.replace(microsecond=0),
                "TransactionAmt": float(rng.choice([1.99, 4.99, 5.0, 12.5, 49.99, 120.0, 499.0, 777.0, 2600.0])) * (1 + float(rng.integers(0, 3)) / 100),
                "ProductCD": prod, "channel": "in_person" if prod == "W" else "online", "risk_score": round(float(rng.random()), 2),
                "addr1": None if rng.random() < 0.15 else float(rng.choice([204, 299, 330])), "addr2": 87.0, "dist1": None, "dist2": None,
                "P_emaildomain": rng.choice([None, "gmail.com", "yahoo.com", "rare.example"]),
                "R_emaildomain": rng.choice([None, None, "gmail.com", "fanin.example"]),
                "device_key": rng.choice([None, "| | |", "SM-G935F|Android|chrome|1920x1080"]) if prod != "W" else None,
                "DeviceType": rng.choice([None, "mobile", "desktop"]) if prod != "W" else None,
                "id_15": rng.choice([None, "New", "Found"]) if prod != "W" else None,
                "id_23": rng.choice(["IP_PROXY:ANONYMOUS", "IP_PROXY:HIDDEN"]) if proxied else None, "id_31": None,
                **{f"M{k}": rng.choice([None, True, False]) for k in (1, 2, 3, 5, 6, 7, 8, 9)}, "M4": rng.choice([None, "M0", "M2"]),
                "C1": 1.0, "C2": 1.0, "C13": 1.0, "C14": 1.0, "D1": 0.0, "D2": None, "D3": None, "D4": None, "D10": None, "D15": None,
                "holder_key": rng.choice([f"{1000 + i}|299.0|-6", f"{1000 + i % 3}|299.0|-6", f"{1000 + i}||-6", f"{1000 + i}|299.0|"]),
                "card1": 1000 + i, "card4": "visa", "card6": "debit"})
    df = pd.DataFrame(out)
    for c in [f"M{k}" for k in (1, 2, 3, 5, 6, 7, 8, 9)]:
        df[c] = df[c].astype("boolean")
    return df


# graph attribute name -> source column (kavach/graph/loading.py EXPORTS["txn"] and schema.gsql)
G_ATTR = {"amt": "TransactionAmt", "product_cd": "ProductCD", "channel": "channel", "risk_score": "risk_score", "addr1": "addr1",
          "addr2": "addr2", "dist1": "dist1", "p_email": "P_emaildomain", "r_email": "R_emaildomain",
          **{f"m{k}": f"M{k}" for k in range(1, 10)}, "c1": "C1", "c2": "C2", "c13": "C13", "c14": "C14", "d1": "D1", "d2": "D2",
          "d3": "D3", "d4": "D4", "d10": "D10", "d15": "D15", "proxy_type": "id_23", "device_type": "DeviceType", "card_id": "card_id",
          "customer_id": "customer_id", "device_key": "device_key", "id_15": "id_15", "id_31": "id_31", "holder_key": "holder_key",
          "card1": "card1", "card4": "card4", "card6": "card6"}
NUM = {"amt", "risk_score", "addr1", "addr2", "dist1", "c1", "c2", "c13", "c14", "d1", "d2", "d3", "d4", "d10", "d15", "card1"}


def _graph_rows(df: pd.DataFrame) -> list[dict]:
    """The rows as the TigerGraph Transaction vertices store them."""
    out = []
    for r in df.to_dict("records"):
        a = {"id": str(r["TransactionID"]), "ts": r["ts"].strftime("%Y-%m-%d %H:%M:%S")}
        for g, c in G_ATTR.items():
            v = r[c]
            null = v is None or v is pd.NA or (isinstance(v, float) and v != v)
            if g in NUM:
                a[g] = 0 if null else (int(v) if g in ("addr1", "addr2", "card1") else float(v))
            elif g.startswith("m") and c != "M4":
                a[g] = "" if null else ("true" if v else "false")
            elif g == "device_key":
                a[g] = "" if null or not str(v).replace("|", "").replace(" ", "") else str(v)
            else:
                a[g] = "" if null else str(v)
        out.append(a)
    return out


def _band(amt: float) -> str:
    return duckq_ext.band_label(amt)


class FakeConn:
    """Answers the installed queries of queries_r2.gsql (and txn_date_range) the way the GSQL computes them."""

    def __init__(self, g: list[dict]):
        self.g = g
        self.calls = []

    @staticmethod
    def _v(a, extra=None):
        return {"v_id": a["id"], "v_type": "Transaction", "attributes": {**a, **(extra or {})}}

    @staticmethod
    def _in(a, lo, hi):
        return lo <= a["ts"] <= hi

    def runInstalledQuery(self, name, params=None, timeout=None, usePost=True):  # noqa: N802 (pyTigerGraph name)
        p = params or {}
        self.calls.append((name, p))
        g = self.g
        if name == "txn_date_range":
            ts = sorted(a["ts"] for a in g)
            return [{"d0": ts[0], "d1": ts[-1]}]
        if name in ("entity_neighbors_r", "entity_neighbors_p", "entity_neighbors_region"):
            attr = {"entity_neighbors_r": "r_email", "entity_neighbors_p": "p_email"}.get(name)
            # vertices exist only for non-empty values (the loader skips nulls); regions are str(int(addr1))
            vals = set(p["vals"])
            hit = [a for a in g if self._in(a, p["t_from"], p["t_to"]) and (
                (attr and a[attr] != "" and a[attr] in vals) or
                (not attr and a["addr1"] != 0 and str(a["addr1"]) in vals and a["channel"] == "in_person"))]
            return [{"T": [self._v(a) for a in hit]}]
        if name == "entity_neighbors_attr":
            return [{"T": [self._v(a) for a in g if self._in(a, p["t_from"], p["t_to"]) and self._match(a, p)]}]
        if name == "amount_peers_any":
            return [{"T": [self._v(a) for a in g if a["product_cd"] == p["product"] and a["channel"] == "online"
                           and p["lo"] <= a["amt"] <= p["hi"] and self._in(a, p["t_from"], p["t_to"])]}]
        if name == "card_atypical_window":
            cards = set(p["card_ids"])
            out = []
            for w in g:
                if w["card_id"] not in cards or not self._in(w, p["t_from"], p["t_to"]):
                    continue
                prior = [e for e in g if e["card_id"] == w["card_id"] and (e["ts"] < w["ts"] or (e["ts"] == w["ts"] and int(e["id"]) < int(w["id"])))]
                same = [e for e in prior if e["product_cd"] == w["product_cd"]]
                low = -1.7976931348623157e308
                out.append(self._v(w, {"@prior_n": len(prior), "@prior_same_product": len(same),
                                       "@prior_proxy": sum(1 for e in prior if e["proxy_type"] != ""),
                                       "@prior_max": max([e["amt"] for e in prior], default=low),
                                       "@prior_max_product": max([e["amt"] for e in same], default=low),
                                       "@prev_ts": max([e["ts"] for e in prior], default="1970-01-01 00:00:00")}))
            return [{"W": out}]
        if name == "entity_shares_any":
            win = [a for a in g if self._in(a, p["t_from"], p["t_to"])]
            cards, icards, f = {}, {}, p["field"]
            for a in win:
                key = None
                if f == "R_emaildomain" and a["r_email"] in p["vals"]:
                    key = a["r_email"]
                elif f == "P_emaildomain" and a["p_email"] in p["vals"]:
                    key = a["p_email"]
                elif f == "holder_key" and a["holder_key"] in p["vals"]:
                    key = a["holder_key"]
                elif f == "access_signature" and a["proxy_type"] != "":
                    key = f"{a['product_cd']}|{a['proxy_type']}|{a['id_15']}|{a['device_type']}"
                elif f == "product_band":
                    key = f"{a['product_cd']}|{_band(a['amt'])}"
                elif f == "addr1" and a["channel"] == "in_person" and a["addr1"] in p["ivals"]:
                    icards.setdefault(a["addr1"], set()).add(a["card_id"])
                if key is not None:
                    cards.setdefault(key, set()).add(a["card_id"])
            return [{"active": len({a["card_id"] for a in win}),
                     "n": {k: len(v) for k, v in cards.items() if k in p["vals"]},
                     "ni": {str(k): len(v) for k, v in icards.items()}}]
        if name == "entity_baseline_any":
            active = {a["card_id"] for a in g if self._in(a, p["w0"], p["t0"])}
            window, buckets = set(), {}
            epoch = lambda s: int((datetime.strptime(s, "%Y-%m-%d %H:%M:%S") - datetime(1970, 1, 1)).total_seconds())  # noqa: E731
            for a in g:
                if not self._in(a, p["b0"], p["t0"]) or not self._match(a, p):
                    continue
                if a["ts"] >= p["w0"]:
                    window.add(a["card_id"])
                else:
                    buckets.setdefault(int((epoch(a["ts"]) - epoch(p["b0"])) / p["bucket_s"]), set()).add(a["card_id"])
            return [{"cards_window": len(window), "active_window": len(active),
                     "per": {str(k): len(v) for k, v in buckets.items()}}]
        raise AssertionError(f"unexpected query {name}")

    @staticmethod
    def _match(a, p) -> bool:
        f = p["field"]
        return ((f == "R_emaildomain" and a["r_email"] == p["s1"]) or (f == "P_emaildomain" and a["p_email"] == p["s1"])
                or (f == "addr1" and a["channel"] == "in_person" and a["addr1"] == p.get("ival"))
                or (f == "product_band" and a["product_cd"] == p["s1"] and p["lo"] <= a["amt"] < p["hi"])
                or (f == "access_signature" and a["proxy_type"] != "" and a["product_cd"] == p["s1"] and a["proxy_type"] == p["s2"]
                    and a["id_15"] == p["s3"] and a["device_type"] == p["s4"])
                or (f == "holder_key" and a["holder_key"] == p["s1"]))


@pytest.fixture(scope="module")
def lanes():
    df = _rows()
    con = duckdb.connect()
    con.register("src", df)
    con.execute("create table txn as select * from src")
    mp = pytest.MonkeyPatch()
    run = lambda sql, *params: con.execute(sql, list(params)).df()   # noqa: E731
    mp.setattr(duckq_ext, "q", run)
    mp.setattr(duckq, "q", run)
    mp.setattr(duckq_ext, "_DATA_START", None)
    tg = gc.TgTools(conn=FakeConn(_graph_rows(df)))
    yield df, tg
    mp.undo()
    con.close()


def _same(a: pd.DataFrame, b: pd.DataFrame, cols=None):
    assert list(a.columns) == list(b.columns)
    assert len(a) == len(b)
    for c in cols or a.columns:
        x, y = a[c].astype(object).tolist(), b[c].astype(object).tolist()
        norm = lambda v: None if v is None or v is pd.NA or v is pd.NaT or (isinstance(v, float) and v != v) else (  # noqa: E731
            pd.Timestamp(v) if isinstance(v, (pd.Timestamp, datetime, np.datetime64)) else (round(v, 9) if isinstance(v, float) else v))
        assert [norm(v) for v in x] == [norm(v) for v in y], c


ENTITY_COLS = ["TransactionID", "card_id", "customer_id", "ts", "amt", "ProductCD", "channel", "risk_score", "addr1", "P_emaildomain",
               "R_emaildomain", "DeviceType", "id_15", "id_23", "holder_key", "card1", "card4", "card6", "M1", "M4", "entity"]


def _values(df, field):
    return sorted({v for r in df.rename(columns={"TransactionAmt": "amt"}).to_dict("records")
                   if (v := duckq_ext.entity_value(field, r)) is not None})


@pytest.mark.parametrize("field", sorted(duckq_ext.ENTITY_FIELDS))
def test_entity_neighbors_matches_duck(lanes, field):
    df, tg = lanes
    vals = _values(df, field) + ["", "nope|x"]
    t_from, t_to = T0, T0 + timedelta(days=60)
    n = 0
    for v in vals:
        d, t = duckq_ext.entity_neighbors(field, v, t_from, t_to), tg.entity_neighbors(field, v, t_from, t_to)
        _same(d, t, ENTITY_COLS)
        n += len(d)
    assert n > 0


@pytest.mark.parametrize("field", sorted(duckq_ext.ENTITY_FIELDS))
def test_entity_baseline_and_shares_match_duck(lanes, field):
    df, tg = lanes
    vals = _values(df, field)
    n = 0
    for t0, days in ((T0 + timedelta(days=12), 7), (T0 + timedelta(days=40), 14), (T0 + timedelta(days=200), 30),
                     (T0 + timedelta(days=95), 7)):
        for v in vals + [""]:
            assert duckq_ext.entity_baseline(field, v, t0, days) == tg.entity_baseline(field, v, t0, days), (field, v, t0, days)
        d, t = duckq_ext.entity_shares(field, vals + ["", "zz"], t0, days), tg.entity_shares(field, vals + ["", "zz"], t0, days)
        _same(d, t)
        n += len(d)
        assert duckq_ext.entity_shares(field, [], t0, days).columns.tolist() == tg.entity_shares(field, [], t0, days).columns.tolist()
    assert n > 0


def test_card_atypical_window_matches_duck(lanes):
    df, tg = lanes
    cards = sorted(df.card_id.unique())
    for sel in (cards[0], cards[:5], cards, ["C99999-K1"]):
        for t_from, t_to in ((T0, T0 + timedelta(days=400)), (T0 + timedelta(days=20), T0 + timedelta(days=50))):
            d, t = duckq_ext.card_atypical_window(sel, t_from, t_to), tg.card_atypical_window(sel, t_from, t_to)
            cols = [c for c in d.columns if c != "device_key"]
            _same(d, t, cols)
    assert tg.card_atypical_window([], T0, T0).columns.tolist() == duckq_ext.card_atypical_window([], T0, T0).columns.tolist()
    flags = tg.card_atypical_window(cards, T0, T0 + timedelta(days=400))
    assert flags.atypical.any() and flags.prior_n.max() >= 5            # the synthetic data exercises the flags


def test_amount_peers_without_email_matches_duck(lanes):
    df, tg = lanes
    for amt, prod in ((4.99, "C"), (499.0, "H"), (12.5, "R"), (777.0, "W")):
        for tol in (0.001, 0.01, 0.05):
            d = duckq.amount_peers(prod, amt, T0 + timedelta(days=30), days=60, tol=tol)
            t = tg.amount_peers(prod, amt, T0 + timedelta(days=30), days=60, tol=tol)
            d = d.sort_values(["ts", "TransactionID"]).reset_index(drop=True)
            _same(d, t, [c for c in ENTITY_COLS if c != "entity"])
    assert tg.conn.calls[-1][0] == "amount_peers_any"


def test_duck_txn_frame_columns_and_placeholders():
    g = _graph_rows(_rows().head(3).assign(addr1=None, DeviceType=None, M1=pd.array([True, False, None], dtype="boolean")))
    f = gc.duck_txn_frame([FakeConn._v(a) for a in g])
    assert list(f.columns) == gc.duck_txn_columns()
    assert f.addr1.isna().all() and f.DeviceType.isna().all()
    assert f.M1.tolist()[:2] == [True, False] and f.M1.isna().tolist()[2]
    assert list(gc.duck_txn_frame([]).columns) == gc.duck_txn_columns()


def test_entity_match_args():
    m = gc.entity_match_args
    assert m("product_band", "C|350-500") == {**m("product_band", "C|350-500"), "field": "product_band", "s1": "C", "lo": 350.0, "hi": 500.0}
    assert m("product_band", "C|0-5")["lo"] < -1e100 and m("product_band", "C|2500+")["hi"] > 1e100
    assert m("product_band", "C|300-500")["field"] == "none" and m("product_band", "|0-5")["field"] == "none"
    assert m("access_signature", "C|IP_PROXY:ANONYMOUS||")["s3"] == "" and m("access_signature", "C||New|mobile")["field"] == "none"
    assert m("holder_key", "1|2.0|-6")["s1"] == "1|2.0|-6" and m("holder_key", "1||-6")["field"] == "none"
    assert m("addr1", "299")["ival"] == 299 and m("addr1", "0299")["field"] == "none" and m("addr1", "0")["field"] == "none"
    assert m("R_emaildomain", "")["field"] == "none"
    with pytest.raises(ValueError):
        m("device_key", "x")


def test_band_labels_in_gsql_match_duck():
    """The band chain of entity_shares_any must produce kavach.duckq_ext.band_label for every amount."""
    text = (GDIR / "queries_r2.gsql").read_text(encoding="utf-8")
    chain = re.findall(r'IF t\.amt < (\d+) THEN @@cards \+= \(t\.product_cd \+ "\|([^"]+)"', text)
    last = re.search(r'ELSE @@cards \+= \(t\.product_cd \+ "\|([^"]+)"', text).group(1)
    assert [int(e) for e, _ in chain] == list(duckq_ext.BAND_EDGES)
    for amt in (0.5, 4.99, 5, 9.99, 10, 349.99, 350, 499.99, 500, 2499.99, 2500, 10000):
        label = next((lab for e, lab in chain if amt < int(e)), last)
        assert label == duckq_ext.band_label(amt)
        lo, hi = gc.band_bounds(label)
        assert lo <= amt < hi
