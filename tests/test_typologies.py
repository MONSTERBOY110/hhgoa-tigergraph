"""Typology detectors and their backtest on synthetic data (no dependency on the real index).

Each test builds a small transaction table, serves it through the real DuckDB tool functions (kavach.duckq,
kavach.duckq_ext) on an in-memory connection, and runs the detector through a fake ctx and tools object.
"""
import math
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from kavach import duckq, duckq_ext
from kavach.detectors import Findings
from kavach.detectors import typologies as ty

COLS = ["TransactionID", "TransactionDT", "card_id", "customer_id", "ts", "TransactionAmt", "ProductCD", "channel", "risk_score",
        "addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain", "device_key", "DeviceType", "id_15", "id_23", "id_31",
        "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "C1", "C2", "C13", "C14", "D1", "D2", "D3", "D4", "D10", "D15",
        "holder_key", "card1", "card4", "card6"]
T0 = pd.Timestamp("2016-07-02")


class Rows:
    def __init__(self):
        self.rows, self.next = [], 1

    def add(self, card, ts, amt, product="W", channel=None, **kw):
        r = {c: None for c in COLS}
        r.update(TransactionID=self.next, card_id=card, customer_id=card.split("-")[0], ts=pd.Timestamp(ts), TransactionAmt=float(amt),
                 ProductCD=product, channel=channel or ("in_person" if product == "W" else "online"), risk_score=0.1, addr1=100.0,
                 holder_key=None)
        r.update(kw)
        self.rows.append(r)
        self.next += 1
        return r["TransactionID"]

    def frame(self):
        df = pd.DataFrame(self.rows, columns=COLS)
        num = ("addr1", "addr2", "dist1", "dist2", "C1", "C2", "C13", "C14", "D1", "D2", "D3", "D4", "D10", "D15", "card1", "risk_score")
        for c in num:
            df[c] = df[c].astype("float64")
        for c in set(COLS) - set(num) - {"TransactionID", "TransactionDT", "ts", "TransactionAmt"}:
            df[c] = df[c].astype("string")          # all-None object columns would otherwise load as integers
        return df


class Tools:
    """Fake ToolRegistry: same call()/ref() interface, served by the real lane-A functions."""

    def __init__(self):
        self.calls, self.trace, self.step = 0, [], 0

    def call(self, name, **kw):
        self.calls += 1
        self.trace.append(name)
        return (getattr(duckq, name, None) or getattr(duckq_ext, name))(**kw)

    @staticmethod
    def ref(name, **kw):
        return f"query:{name}(" + ", ".join(f"{k}={v}" for k, v in kw.items()) + ")"


@pytest.fixture
def db():
    """Install an in-memory index; returns a loader(frame)."""
    saved = duckq._con
    con = duckdb.connect()

    def load(df):
        con.register("src", df)
        con.execute("create or replace table txn as select * from src")
        duckq_ext._DATA_START = None
        duckq._con = con
    yield load
    duckq._con = saved
    duckq_ext._DATA_START = None
    con.close()


def ctx_for(card, tid):
    T = Tools()
    h = duckq.card_history(card)
    f = h[h.TransactionID == tid].iloc[0].to_dict()
    return SimpleNamespace(f=f, h=h, card_id=card, tools=T, t0=pd.Timestamp(f["ts"]), case={"card_id": card})


def bt(name, status="validated", lift=10.0, **params):
    rec = {"params": {**ty.DEFAULTS[name], **params}, "status": status, "lift": lift, "support": 20, "cleared_hits": 0}
    rec["llr"] = ty.lift_to_llr(rec)
    return {name: rec}


def background(R, n_cards, start, end, product="W", **kw):
    """n_cards ordinary cards with one purchase each, spread over [start, end] (gives the active-card denominators)."""
    span = (pd.Timestamp(end) - pd.Timestamp(start)) / max(n_cards, 1)
    for i in range(n_cards):
        R.add(f"B{i:05d}-K1", pd.Timestamp(start) + i * span, 40 + i % 30, product, **kw)


# ------------------------------------------------------------------------------------------------ helpers
def test_band_label_matches_sql():
    con = duckdb.connect()
    for amt in (0.5, 4.99, 5.0, 19.99, 349.0, 499.99, 500.0, 2499.0, 2500.0, 9000.0):
        sql = con.execute(f"select {duckq_ext._band_sql('x')} from (select {amt} x)").fetchone()[0]
        assert sql == duckq_ext.band_label(amt), amt


def test_entity_value_matches_sql(db):
    R = Rows()
    R.add("C00001-K1", T0, 480.0, "C", R_emaildomain="ring.example", id_23="IP_PROXY:ANONYMOUS", id_15="New", DeviceType="mobile")
    R.add("C00001-K1", T0 + pd.Timedelta(hours=1), 60.0, "W", addr1=231.0, holder_key="123|231|17000")
    R.add("C00001-K1", T0 + pd.Timedelta(hours=2), 70.0, "H", holder_key="123||")
    db(R.frame())
    rows = duckq.card_history("C00001-K1").to_dict("records")
    for f in duckq_ext.ENTITY_FIELDS:
        for r in rows:
            v = duckq_ext.entity_value(f, r)
            got = duckq_ext.entity_neighbors(f, v, T0 - pd.Timedelta(days=1), T0 + pd.Timedelta(days=1)) if v is not None else None
            if v is not None:
                assert r["TransactionID"] in set(got.TransactionID), (f, v)
    assert duckq_ext.entity_value("access_signature", rows[0]) == "C|IP_PROXY:ANONYMOUS|New|mobile"
    assert duckq_ext.entity_value("access_signature", rows[1]) is None          # no proxy, no signature
    assert duckq_ext.entity_value("holder_key", rows[2]) is None                 # incomplete key links nothing


def test_lift_to_llr():
    assert ty.lift_to_llr({"lift": math.e ** 2, "status": "validated"}) == 1.0
    assert ty.lift_to_llr({"lift": 1e6, "status": "validated"}) == 2.0
    assert ty.lift_to_llr({"lift": 1e6, "status": "weak"}) == 0.5
    assert ty.lift_to_llr({"lift": 0.5, "status": "weak"}) == 0.0
    assert ty.lift_to_llr({"lift": None, "status": "weak"}) == 0.0


# ------------------------------------------------------------------------------------------------ dormant_wake
def _dormant(gap_days):
    R = Rows()
    for i in range(6):
        R.add("C00002-K1", T0 + pd.Timedelta(days=i), 30 + i)
    t = T0 + pd.Timedelta(days=5 + gap_days)
    a = R.add("C00002-K1", t, 80.0, "C")
    R.add("C00002-K1", t + pd.Timedelta(hours=5), 95.0, "C")
    return R, a


def test_dormant_wake_fires(db):
    R, a = _dormant(60)
    db(R.frame())
    ctx, fnd = ctx_for("C00002-K1", a), Findings()
    sigs = ty.detect(ctx, fnd, bt("dormant_wake", gap_days=45))
    assert [s.name for s in sigs] == ["dormant_wake"]
    s = sigs[0]
    assert s.group == "sequence" and s.how == {"tool": "card_history", "params": {"card_id": "C00002-K1"}}
    assert str(a) in s.entity_ids and "60 days" in s.claim
    assert fnd.notes["typologies"][0]["by"] == "online_burst"


def test_dormant_wake_no_fire_short_gap(db):
    R, a = _dormant(20)
    db(R.frame())
    assert ty.detect(ctx_for("C00002-K1", a), Findings(), bt("dormant_wake", gap_days=45)) == []


# ------------------------------------------------------------------------------------------------ micro_charges
def _micro(follow_big=False, habit=0):
    R = Rows()
    for i in range(8):
        R.add("C00003-K1", T0 + pd.Timedelta(days=i), 60 + i)
    base = T0 + pd.Timedelta(days=40)
    for i in range(habit):
        R.add("C00003-K1", base - pd.Timedelta(days=30 + i), 2.5, "C")
    ids = [R.add("C00003-K1", base + pd.Timedelta(days=i * 1.2), 1.5 + i * 0.6, "C") for i in range(5)]
    if follow_big:
        R.add("C00003-K1", base + pd.Timedelta(days=4.8, minutes=20), 250.0, "C")
    return R, ids


def test_micro_charges_fires(db):
    R, ids = _micro()
    db(R.frame())
    ctx, fnd = ctx_for("C00003-K1", ids[-1]), Findings()
    sigs = ty.detect(ctx, fnd, bt("micro_charges"))
    assert [s.name for s in sigs] == ["micro_charges"]
    assert set(sigs[0].entity_ids) == {str(i) for i in ids} and sigs[0].how["tool"] == "card_history"


def test_micro_charges_no_fire_when_r5_shape(db):
    R, ids = _micro(follow_big=True)
    db(R.frame())
    assert ty.detect(ctx_for("C00003-K1", ids[-1]), Findings(), bt("micro_charges")) == []


def test_micro_charges_no_fire_on_habit(db):
    R, ids = _micro(habit=3)
    db(R.frame())
    assert ty.detect(ctx_for("C00003-K1", ids[-1]), Findings(), bt("micro_charges", habit_max=0)) == []


def test_micro_charges_peers(db):
    R = Rows()
    anchor = R.add("C00010-K1", T0 + pd.Timedelta(days=3), 3.17, "C")
    for i in range(4):
        R.add(f"C0002{i}-K1", T0 + pd.Timedelta(days=i), 3.17, "C")
    db(R.frame())
    fnd = Findings()
    sigs = ty.detect(ctx_for("C00010-K1", anchor), fnd, bt("micro_charges_peers", min_cards=5))
    assert [s.name for s in sigs] == ["micro_charges_peers"] and sigs[0].group == "network"
    assert sigs[0].how["tool"] == "amount_peers" and len(fnd.notes["related_cards"]) == 4
    assert ty.detect(ctx_for("C00010-K1", anchor), Findings(), bt("micro_charges_peers", min_cards=6)) == []


# ------------------------------------------------------------------------------------------------ recipient_fanin
def _fanin(steady: bool):
    R = Rows()
    background(R, 400, T0, T0 + pd.Timedelta(days=150))
    if steady:   # the domain is paid by two cards every week for months: no spike
        for w in range(20):
            for j in range(2):
                R.add(f"S{w:02d}{j}-K1", T0 + pd.Timedelta(days=7 * w + j), 50, "C", R_emaildomain="shop.example")
    end = T0 + pd.Timedelta(days=140)
    ids = [R.add(f"C1{i:04d}-K1", end - pd.Timedelta(days=10 - i), 120 + i, "C", R_emaildomain="shop.example") for i in range(6)]
    return R, ids


def test_recipient_fanin_fires(db):
    R, ids = _fanin(steady=False)
    db(R.frame())
    fnd = Findings()
    sigs = ty.detect(ctx_for("C10005-K1", ids[-1]), fnd, bt("recipient_fanin", min_cards=5, days=14))
    assert [s.name for s in sigs] == ["recipient_fanin"]
    s = sigs[0]
    assert s.how["tool"] == "entity_neighbors" and s.how["params"]["value"] == "shop.example"
    assert "C10000-K1" in s.entity_ids and "shop.example" in fnd.shared_element
    out = getattr(duckq_ext, s.how["tool"])(**s.how["params"])
    assert set(s.entity_ids) <= set(out.card_id) | {str(x) for x in out.TransactionID}
    assert set(fnd.connected_cards) >= {"C10000-K1", "C10004-K1"}          # validated record: listed as connected
    weak = Findings()
    ty.detect(ctx_for("C10005-K1", ids[-1]), weak, bt("recipient_fanin", status="weak", lift=2.0, min_cards=5, days=14))
    assert weak.connected_cards == {} and weak.notes["related_cards"]   # weak: related only, never connected


def test_recipient_fanin_no_fire_on_steady_domain(db):
    R, ids = _fanin(steady=True)
    db(R.frame())
    assert ty.detect(ctx_for("C10005-K1", ids[-1]), Findings(), bt("recipient_fanin", min_cards=5, days=14)) == []


# ------------------------------------------------------------------------------------------------ common_origin
def _origin(atypical: bool):
    R = Rows()
    background(R, 400, T0 + pd.Timedelta(days=30), T0 + pd.Timedelta(days=60))
    start = T0 + pd.Timedelta(days=50)
    anchor = None
    for i in range(6):
        card = f"C2{i:04d}-K1"
        for j in range(6):   # an in-person history; when not atypical the card already uses product C at similar amounts
            R.add(card, T0 + pd.Timedelta(days=j), 40.0, "W" if atypical else "C")
        if not atypical:
            R.add(card, T0 + pd.Timedelta(days=7), 900.0, "C")
        tid = R.add(card, start + pd.Timedelta(days=i), 300.0, "C", R_emaildomain="cpp.example")
        anchor = tid if i == 5 else anchor
    return R, anchor


def test_common_origin_fires(db):
    R, a = _origin(atypical=True)
    db(R.frame())
    fnd = Findings()
    sigs = ty.detect(ctx_for("C20005-K1", a), fnd, bt("common_origin", days=14, min_cards=3, min_frac=0.5))
    assert [s.name for s in sigs] == ["common_origin"]
    s = sigs[0]
    assert s.group == "network" and s.how["tool"] == "entity_neighbors"
    assert fnd.notes["typologies"][0]["k"] == 5
    assert set(fnd.notes["related_cards"]) == {f"C2{i:04d}-K1" for i in range(5)}
    assert fnd.shared_element
    # every id the signal cites is in the re-run of its `how` query (what the attestation stream checks)
    out = getattr(duckq_ext, s.how["tool"])(**s.how["params"])
    assert set(s.entity_ids) <= set(out.card_id) | {str(x) for x in out.TransactionID}


def test_common_origin_no_fire_when_neighbours_typical(db):
    R, a = _origin(atypical=False)
    db(R.frame())
    assert ty.detect(ctx_for("C20005-K1", a), Findings(), bt("common_origin", days=14, min_cards=3, min_frac=0.5)) == []


def test_no_backtest_file_emits_nothing(db, tmp_path):
    R, a = _dormant(60)
    db(R.frame())
    assert ty.load_backtests(tmp_path) == {}
    assert ty.detect(ctx_for("C00002-K1", a), Findings(), ty.load_backtests(tmp_path)) == []


# ------------------------------------------------------------------------------------------------ backtest smoke
def test_backtest_run_smoke(tmp_path, monkeypatch):
    from kavach import backtest
    R = Rows()
    background(R, 60, T0, T0 + pd.Timedelta(days=120))
    fraud_anchors = []
    for k in range(3):                                  # three dormant cards that wake up: the confirmed-fraud cases
        card = f"C3{k:04d}-K1"
        for i in range(6):
            R.add(card, T0 + pd.Timedelta(days=i), 30.0)
        t = T0 + pd.Timedelta(days=80 + k)
        fraud_anchors.append((card, R.add(card, t, 90.0, "C")))
        R.add(card, t + pd.Timedelta(hours=3), 95.0, "C")
    cleared = ("B00001-K1", 2)
    df = R.frame()
    path = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.register("src", df)
    con.execute("create table txn as select * from src")
    closed = [{"case_id": f"CC-{i}", "customer_id": c.split("-")[0], "card_id": c, "opened_at": "2016-10-01 00:00:00",
               "outcome": "confirmed_fraud", "first_fraud_txn_id": str(t), "txn_ids": str(t)} for i, (c, t) in enumerate(fraud_anchors)]
    closed.append({"case_id": "CC-9", "customer_id": "B00001", "card_id": cleared[0], "opened_at": "2016-08-01 00:00:00",
                   "outcome": "cleared", "first_fraud_txn_id": None, "txn_ids": str(cleared[1])})
    con.register("cl", pd.DataFrame(closed))
    con.execute("create table closed as select * from cl")
    con.execute("create table closed_txn as select case_id, outcome, cast(txn_ids as bigint) TransactionID from closed")
    con.execute("create table cases as select '' card_id, '' opened_at, '' flagged_txn_id where false")
    con.close()
    saved = duckq._con
    monkeypatch.setitem(backtest.GRIDS, "dormant_wake", {"gap_days": [45, 90], "min_online": [2], "burst_hours": [48],
                                                         "above_max": [0], "min_prior": [3]})
    try:
        out = backtest.run("dormant_wake", duck_path=path, pop_n=40, workers=1, january=False, write=False, log=lambda m: None)
    finally:
        duckq._con = saved
        duckq_ext._DATA_START = None
    assert out["n_fraud"] == 3 and out["n_cleared"] == 1 and len(out["results"]) == 2
    r45 = next(r for r in out["results"] if r["params"]["gap_days"] == 45)
    assert r45["support"] == 3 and r45["cleared_hits"] == 0
    assert {h["case_id"] for h in out["hits"]["fraud"]} == {"CC-0", "CC-1", "CC-2"} or out["chosen"]["params"]["gap_days"] == 90
    assert out["chosen"]["status"] == "weak"            # support 3 < 15: never validated on a tiny set
    assert out["chosen"]["llr"] <= ty.WEAK_CAP


# ------------------------------------------------------------------------------------------------ proxy_new_burst
def _proxy(n_ring: int, shared_device: bool = False):
    R = Rows()
    background(R, 50, T0, T0 + pd.Timedelta(days=60))
    start = T0 + pd.Timedelta(days=40)
    anchor = None
    for i in range(n_ring + 1):
        card = f"C4{i:04d}-K1"
        for j in range(6):
            R.add(card, T0 + pd.Timedelta(days=j), 40.0)
        dev = "SM-X | Android 7.0 | chrome | 1080x1920" if shared_device else f"DEV{i} | Android 7.0 | chrome | 1080x1920"
        for k in range(2):
            tid = R.add(card, start + pd.Timedelta(days=i, hours=10 * k), 450.0 + 50 * k, "C", id_15="New", id_23="IP_PROXY:ANONYMOUS",
                        DeviceType="mobile", device_key=dev)
            if i == 0 and k == 1:
                anchor = tid
    return R, anchor


def test_proxy_new_burst_and_ring(db):
    R, a = _proxy(n_ring=3)
    db(R.frame())
    fnd = Findings()
    bts = {**bt("proxy_new_burst"), **bt("proxy_new_burst_ring", min_other=3)}
    sigs = ty.detect(ctx_for("C40000-K1", a), fnd, bts)
    assert [s.name for s in sigs] == ["proxy_new_burst", "proxy_new_burst_ring"]
    assert sigs[0].group == "sequence" and sigs[1].group == "network"
    assert fnd.coordinated_undocumented and fnd.undocumented_kind == "proxy_new_burst" and fnd.pattern_votes.get("undocumented")
    assert set(fnd.connected_cards) == {"C40001-K1", "C40002-K1", "C40003-K1"}
    assert len(fnd.episode["proxy_new_burst"]) == 2 and a in fnd.episode["proxy_new_burst"]
    out = getattr(duckq_ext, sigs[1].how["tool"])(**sigs[1].how["params"])
    assert set(sigs[1].entity_ids) <= set(out.card_id) | {str(x) for x in out.TransactionID}


def test_proxy_ring_needs_enough_cards_and_no_shared_device(db):
    R, a = _proxy(n_ring=2)
    db(R.frame())
    fnd = Findings()
    sigs = ty.detect(ctx_for("C40000-K1", a), fnd, {**bt("proxy_new_burst"), **bt("proxy_new_burst_ring", min_other=3)})
    assert [s.name for s in sigs] == ["proxy_new_burst"] and not fnd.coordinated_undocumented and fnd.connected_cards == {}
    R, a = _proxy(n_ring=4, shared_device=True)              # a shared device is a device ring, not this template
    db(R.frame())
    assert [s.name for s in ty.detect(ctx_for("C40000-K1", a), Findings(), bt("proxy_new_burst_ring", min_other=3))] == []


# ------------------------------------------------------------------------------------------------ amount_ladder
def _ladder(n_ring: int, steps=(180.0, 360.0, 800.0, 1800.0, 1800.0, 1800.0)):
    R = Rows()
    start = T0 + pd.Timedelta(days=30)
    anchor = None
    for i in range(n_ring + 1):
        card = f"C5{i:04d}-K1"
        for j in range(6):
            R.add(card, T0 + pd.Timedelta(days=j), 40.0)
        for k, amt in enumerate(steps):
            tid = R.add(card, start + pd.Timedelta(days=i * 0.5 + k * 1.5), amt, "C")
            if i == 0 and k == len(steps) - 1:
                anchor = tid
    return R, anchor


def test_amount_ladder_and_ring(db):
    R, a = _ladder(n_ring=3)
    db(R.frame())
    fnd = Findings()
    sigs = ty.detect(ctx_for("C50000-K1", a), fnd, {**bt("amount_ladder"), **bt("amount_ladder_ring", min_other=3)})
    assert [s.name for s in sigs] == ["amount_ladder", "amount_ladder_ring"]
    assert len(fnd.episode["amount_ladder"]) == 6 and fnd.undocumented_kind == "amount_ladder"
    assert set(fnd.connected_cards) == {"C50001-K1", "C50002-K1", "C50003-K1"}
    out = getattr(duckq, sigs[1].how["tool"])(**sigs[1].how["params"])
    assert set(sigs[1].entity_ids) <= set(out.card_id) | {str(x) for x in out.TransactionID}


def test_amount_ladder_no_fire_on_flat_or_small(db):
    R, a = _ladder(n_ring=0, steps=(100.0, 120.0, 150.0, 170.0, 1100.0))     # one big step is not a ladder
    db(R.frame())
    assert ty.detect(ctx_for("C50000-K1", a), Findings(), bt("amount_ladder")) == []
    R, a = _ladder(n_ring=0, steps=(20.0, 45.0, 100.0, 220.0, 400.0))        # a ladder that never reaches the top amount
    db(R.frame())
    assert ty.detect(ctx_for("C50000-K1", a), Findings(), bt("amount_ladder", min_max=1000.0)) == []
