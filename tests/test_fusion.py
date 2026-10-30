"""Graph and vector fused (fused_precedent) and the ring component graph algorithm (ring_wcc), kavach/graph/fusion.py.

Offline: a small in-memory DuckDB with the index's table and column names stands in for the bank, so the SQL lane,
the union-find and the result parsers are tested without TigerGraph. The two-lane parity on the 18 HHF cases runs
against the real workspace (KAVACH_PARITY=1, as tests/test_parity.py)."""
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from kavach.graph import fusion

T0 = datetime(2017, 1, 20, 12, 0, 0)
GSQL = Path(fusion.__file__).with_name("queries_fusion.gsql")


# ---------------------------------------------------------------------------------------------------------------- data
def _bank():
    """Cards A..F with five months of ordinary history before the window, then in the window:
    A, B, C pay the rare recipient domain ring.example from atypical rows (a product never used before);
    D pays ring.example with an ordinary row (not atypical, so it does not join);
    E and F share a handset device profile on atypical rows; G..Z pay the common domain big.example (too common to link).
    Closed cases: CC-1 on A, CC-2 lists A as connected, CC-3 on E, CC-4 on F (cleared)."""
    rows, tid = [], 1000
    cards = ["A", "B", "C", "D", "E", "F"] + [f"G{i:02d}" for i in range(30)]

    def add(card, ts, amt, prod, r_email=None, dev=None, info=None, os_=None, br=None, screen=None, proxy=None, id15=None, dtype=None):
        nonlocal tid
        tid += 1
        rows.append(dict(TransactionID=tid, card_id=card, customer_id=card, ts=ts, TransactionAmt=amt, ProductCD=prod,
                         channel="online", addr1=100.0, R_emaildomain=r_email, P_emaildomain="x.com", device_key=dev,
                         DeviceInfo=info, id_30=os_, id_31=br, id_33=screen, id_23=proxy, id_15=id15, DeviceType=dtype))
        return tid

    for c in cards:                                   # history: 6 product W purchases of $50 to $100 in the autumn
        for k in range(6):
            add(c, T0 - timedelta(days=150 - 7 * k), 50 + 10 * k, "W")
    win = {}
    for i, c in enumerate(["A", "B", "C"]):           # atypical (first product C) to the rare domain
        win[c] = add(c, T0 - timedelta(days=5 - i), 40.0, "C", r_email="ring.example")
    win["D"] = add("D", T0 - timedelta(days=3), 60.0, "W", r_email="ring.example")      # ordinary row
    dev = "SM-X1 Build/AB12 | Android 7 | chrome 60 | 1080x1920"
    for c in ("E", "F"):                              # atypical (above every earlier amount) on one rare handset
        win[c] = add(c, T0 - timedelta(days=2), 900.0, "W", dev=dev, info="SM-X1 Build/AB12", os_="Android 7", br="chrome 60",
                     screen="1080x1920")
    for c in cards[6:]:                               # a common domain: 30 cards in the window, atypical rows too
        add(c, T0 - timedelta(days=4), 500.0, "C", r_email="big.example")
    add("A", T0 - timedelta(days=1), 500.0, "C", r_email="big.example")
    txn = pd.DataFrame(rows)
    closed = pd.DataFrame([
        dict(case_id="CC-1", card_id="A", customer_id="A", outcome="confirmed_fraud", pattern="card_not_present_fraud",
             opened_at=T0 - timedelta(days=60)),
        dict(case_id="CC-3", card_id="E", customer_id="E", outcome="confirmed_fraud", pattern="account_takeover",
             opened_at=T0 - timedelta(days=40)),
        dict(case_id="CC-4", card_id="B", customer_id="B", outcome="cleared", pattern="none", opened_at=T0 - timedelta(days=30)),
        dict(case_id="CC-5", card_id="Z", customer_id="Z", outcome="confirmed_fraud", pattern="card_testing",
             opened_at=T0 - timedelta(days=20)),
    ])
    closed_conn = pd.DataFrame([dict(case_id="CC-2x", card_id="A"), dict(case_id="CC-5", card_id="C")])
    closed = pd.concat([closed, pd.DataFrame([dict(case_id="CC-2x", card_id="Q", customer_id="Q", outcome="confirmed_fraud",
                                                   pattern="undocumented", opened_at=T0 - timedelta(days=10))])])
    return txn, closed, closed_conn, win


@pytest.fixture(scope="module")
def bank():
    txn, closed, closed_conn, win = _bank()
    con = duckdb.connect()
    con.register("_t", txn)
    con.execute("create table txn as select * from _t")
    con.register("_c", closed)
    con.execute("create table closed as select * from _c")
    con.register("_cc", closed_conn)
    con.execute("create table closed_conn as select * from _cc")
    yield con, win
    con.close()


# ---------------------------------------------------------------------------------------------------------------- pure
def test_components_partition_and_root_are_order_free():
    els = {"e1": ["C", "B"], "e2": ["D", "C"], "e3": ["X", "Y"]}
    a = fusion.components(els)
    b = fusion.components(dict(reversed(list(els.items()))))
    assert a == b == {"B": "B", "C": "B", "D": "B", "X": "X", "Y": "X"}


def test_component_of_edges_and_stats():
    out = fusion.component_of("C", {"e1": ["B", "C"], "e2": ["C", "D"], "e3": ["X", "Y"]})
    assert out["cards"] == ["B", "C", "D"] and out["size"] == 3
    assert out["n_components"] == 2 and out["largest"] == 3 and out["n_elements"] == 3
    assert out["edges"] == [{"a": "B", "b": "C", "elements": ["e1"]}, {"a": "C", "b": "D", "elements": ["e2"]}]
    assert set(out["elements"]) == {"e1", "e2"}
    alone = fusion.component_of("Q", {"e3": ["X", "Y"]})
    assert alone["cards"] == ["Q"] and alone["edges"] == []


@pytest.mark.parametrize("info,screen,n,ok", [
    ("SM-G930V Build/NRD90M", "", 3, True),          # a handset model links even without a screen
    ("Windows", "1920x1080", 10, True),              # a family name with a screen is a fingerprint at this rarity
    ("Windows", "", 10, False),                       # a bare family name links nothing
    ("Trident/7.0", "", 2, False), ("rv:11.0", "", 2, False), ("iOS Device", "", 2, False),
    ("", "1920x1080", 2, False),                      # no DeviceInfo
    ("SM-G930V Build/NRD90M", "1080x1920", 26, False),  # too many cards
])
def test_linking_device_and_its_sql_twin_agree(info, screen, n, ok):
    assert fusion.linking_device(info, screen, n) is ok
    con = duckdb.connect()
    got = con.execute(f"select {fusion._linking_sql(fusion.MAX_DEV_CARDS)} from (select ? info, ? screen, ? n)",
                      [info, screen, n]).fetchone()[0]
    assert bool(got) is ok


def test_gsql_family_test_matches_python_constants():
    text = GSQL.read_text(encoding="utf-8")
    for name in fusion.FAMILY_NAMES:
        assert f'"{name}"' in text
    for p in fusion.FAMILY_PREFIXES:
        assert f'LIKE "{p}%"' in text


def test_keep_elements_rules():
    meta = {"dev1": (3, "SM-1 Build/X", ""), "dev2": (3, "Windows", "")}
    raw = {"device:dev1": ["A", "B"], "device:dev2": ["A", "B"], "r_email:rare": ["A", "C"], "r_email:big": ["A", "D"],
           "signature:C|p|New|mobile": ["E"], "template:ring": ["A", "Z"]}
    kept = fusion.keep_elements(raw, meta, window_pop={"r_email:rare": 4, "r_email:big": 400})
    assert set(kept) == {"device:dev1", "r_email:rare", "template:ring"}


def test_signature_key_matches_entity_field():
    assert fusion.signature_key("C", "IP_PROXY:ANONYMOUS", "New", "mobile") == "C|IP_PROXY:ANONYMOUS|New|mobile"
    assert fusion.signature_key("C", None, "New", "mobile") is None
    assert fusion.signature_key("W", "IP_PROXY:HIDDEN", float("nan"), None) == "W|IP_PROXY:HIDDEN||"


def test_rank_traversal_and_path_type():
    c = pd.DataFrame([{"case_id": "CC-9", "via": ["r_email:x"], "opened_at": "2016-12-01"},
                      {"case_id": "CC-2", "via": ["card"], "opened_at": "2016-08-01"},
                      {"case_id": "CC-3", "via": ["connected_card", "device:d"], "opened_at": "2016-11-01"},
                      {"case_id": "CC-4", "via": ["card"], "opened_at": "2016-09-01"}])
    assert fusion.rank_traversal(c, 3) == ["CC-4", "CC-2", "CC-3"]
    assert fusion.path_type(["region:1", "device:x"]) == "device"


def test_query_text_skips_documents_and_memory():
    a = {"case": {"verdict": "fraud", "pattern": "undocumented", "evidence": [
        {"claim": "Pattern definition, x", "source": "document"}, {"claim": "Case memory: earlier", "source": "graph"},
        {"claim": "R email ring.example on 3 cards", "source": "graph"}, {"claim": "Amount above every earlier", "source": "dataset"}]}}
    assert fusion.query_text(a) == "confirmed fraud undocumented: R email ring.example on 3 cards Amount above every earlier"


# ---------------------------------------------------------------------------------------------------------------- duck lane
def test_ring_component_duck_lane(bank):
    con, win = bank
    tf, tt = T0 - timedelta(days=30), T0
    r = fusion.ring_component(None, "A", tf, tt, con=con)
    assert r["cards"] == ["A", "B", "C"]                          # D's ordinary row does not join; big.example is too common
    assert list(r["elements"]) == ["r_email:ring.example"]
    assert r["element_txns"]["r_email:ring.example"] == sorted(str(win[c]) for c in "ABC")
    assert r["n_components"] == 2 and r["largest"] == 3            # {A,B,C} and the device pair {E,F}
    e = fusion.ring_component(None, "E", tf, tt, con=con)
    assert e["cards"] == ["E", "F"] and list(e["elements"])[0].startswith("device:SM-X1")
    t = fusion.ring_component(None, "A", tf, tt, template=("ladder", ["A", "E"]), con=con)
    assert t["cards"] == ["A", "B", "C", "E", "F"]                # a detector's ring template bridges the two
    prop = fusion.ring_cards_for_answer(t, "A", ["E"])
    assert prop == {"connected": ["B", "C", "E", "F"], "added": ["B", "C", "F"], "template_not_reached": []}


def test_fused_precedent_duck_lane(bank):
    con, _ = bank
    ids = ["CC-1", "CC-2x", "CC-3", "CC-4", "CC-5"]
    rng = np.random.default_rng(7)
    vecs = rng.normal(size=(5, 8)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    q = vecs[3] + 0.1 * vecs[0]                                     # closest to CC-4, then CC-1
    out = fusion.fused_precedent(None, ["A"], q, T0, k=3, con=con, vecs={"case_ids": np.array(ids), "case_vecs": vecs})
    cands = out["candidates"].set_index("case_id")
    # A's own case, A as connected card, and B's / C's cases reached through the rare recipient domain;
    # CC-3 (card E) is not connected to A and must not appear however similar it is
    assert set(cands.index) == {"CC-1", "CC-2x", "CC-4", "CC-5"}
    assert cands.loc["CC-4", "via"] == ["r_email:ring.example"] and cands.loc["CC-4", "through"] == ["B"]
    assert cands.loc["CC-5", "through"] == ["C"] and cands.loc["CC-1", "via"] == ["card"]
    hits = out["hits"]
    assert hits[0]["case_id"] == "CC-4" and hits[0]["path"] == "r_email" and len(hits) == 3
    assert [h["similarity"] for h in hits] == sorted([h["similarity"] for h in hits], reverse=True)
    none = fusion.fused_precedent(None, ["NOPE"], q, T0, con=con, vecs={"case_ids": np.array(ids), "case_vecs": vecs})
    assert none["hits"] == [] and none["n_candidates"] == 0


# ---------------------------------------------------------------------------------------------------------------- tg lane parsing
class _Lane:
    """A TgTools-like lane: `_q` returns canned installed-query output (as the REST API shapes it)."""

    def __init__(self, results):
        self.results, self.calls = results, []

    def _q(self, name, **params):
        self.calls.append((name, params))
        return self.results[name]


def test_tg_ring_result_parses_like_the_duck_lane(bank):
    con, _ = bank
    tf, tt = T0 - timedelta(days=30), T0
    d = fusion.ring_component(None, "A", tf, tt, con=con)
    res = [{"component": list(reversed(d["cards"]))}, {"elements": d["elements"]}, {"element_txns": d["element_txns"]},
           {"n_components": d["n_components"], "largest": d["largest"], "n_elements": d["n_elements"],
            "n_window_txns": 99, "n_atypical": d["n_atypical"], "iterations": 2}]
    lane = _Lane({"ring_wcc": res})
    t = fusion.ring_component(lane, "A", tf, tt)
    for k in ("cards", "size", "elements", "edges", "element_txns", "n_components", "largest", "n_elements", "n_atypical"):
        assert t[k] == d[k], k
    name, params = lane.calls[0]
    assert name == "ring_wcc" and params["tmpl_cards"] == [""] and params["min_prior"] == fusion.MIN_PRIOR


def test_tg_precedent_result_parses_and_ranks():
    res = [{"hits": [{"v_id": "CC-1", "attributes": {"H.outcome": "confirmed_fraud"}},
                     {"v_id": "CC-4", "attributes": {"H.outcome": "cleared"}}]},
           {"dist": {"CC-1": 0.4, "CC-4": 0.1}}, {"n_candidates": 2}, {"k": 5},
           {"candidates": [{"v_id": "CC-4", "attributes": {"X.outcome": "cleared", "X.pattern": "none", "X.opened_at": "2016-12-21 12:00:00",
                                                          "X.@via": ["r_email:ring.example"], "X.@through": ["B"]}},
                           {"v_id": "CC-1", "attributes": {"X.outcome": "confirmed_fraud", "X.pattern": "cnp",
                                                          "X.opened_at": "2016-11-21 12:00:00", "X.@via": ["card"], "X.@through": []}}]}]
    out = fusion.fused_precedent(_Lane({"fused_precedent": res}), ["A"], np.ones(4), T0, k=5)
    assert [(h["case_id"], h["similarity"], h["path"]) for h in out["hits"]] == [("CC-4", 0.9, "r_email"), ("CC-1", 0.6, "card")]
    assert out["n_candidates"] == 2 and out["how"]["lane"] == "tg"


def test_gsql_queries_are_read_only_and_named():
    text = GSQL.read_text(encoding="utf-8")
    names = re.findall(r"CREATE OR REPLACE QUERY (\w+)\(", text)
    assert names == ["fused_precedent", "ring_wcc", "case_life", "detector_life"]
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))
    for verb in ("INSERT INTO", "DELETE", "UPDATE ", "upsert"):
        assert verb not in body
    assert "vectorSearch({ClosedCase.emb}, qv, kk, {candidate_set: X, distance_map: @@dist})" in body
    assert chr(0x2014) not in text and chr(0x2013) not in text


# ---------------------------------------------------------------------------------------------------------------- live parity
HAVE_TG = os.environ.get("KAVACH_PARITY") == "1"


@pytest.mark.skipif(not HAVE_TG, reason="TigerGraph parity: set KAVACH_PARITY=1 with credentials in .env")
@pytest.mark.parametrize("case_id", ["HHF-006", "HHF-013"])
def test_lanes_agree_on_the_graph(case_id):
    from kavach import duckq
    from kavach.graph.client import connect
    row = next(r for r in duckq.case_pack(2).to_dict("records") if r["case_id"] == case_id)
    conn = connect()
    t0 = pd.Timestamp(row["opened_at"])
    a = fusion.ring_component(conn, row["card_id"], t0 - timedelta(days=30), t0)
    b = fusion.ring_component(None, row["card_id"], t0 - timedelta(days=30), t0)
    assert (a["cards"], a["elements"], a["element_txns"]) == (b["cards"], b["elements"], b["element_txns"])
    text = "confirmed fraud undocumented: recipient email shared by several cards"
    p = fusion.fused_precedent(conn, [row["card_id"]], text, t0)
    q = fusion.fused_precedent(None, [row["card_id"]], text, t0)
    assert [(h["case_id"], h["similarity"]) for h in p["hits"]] == [(h["case_id"], h["similarity"]) for h in q["hits"]]


# ---------------------------------------------------------------------------------------------------------------- agent hooks
def test_register_routes_by_lane_and_shapes_for_the_digest(monkeypatch, bank):
    con, _ = bank
    seen = []

    class Reg:
        def __init__(self, backend, impl=None):
            self.backend, self._impl, self.tools = backend, impl, {}

        def register(self, name, fn):
            self.tools[name] = fn

    real_ring, real_prec = fusion.ring_component, fusion.fused_precedent
    monkeypatch.setattr(fusion, "ring_component", lambda lane, *a, **k: seen.append(lane) or real_ring(None, *a, con=con, **k))
    monkeypatch.setattr(fusion, "fused_precedent", lambda lane, *a, **k: seen.append(lane) or {"hits": [{"case_id": "CC-1"}]})
    duck, tg = Reg("duck"), Reg("tg", impl="TGIMPL")
    fusion.register(duck)
    fusion.register(tg)
    out = duck.tools["ring_component"](card_id="A", t_from=str(T0 - timedelta(days=30)), t_to=str(T0),
                                       template_name="ladder", template_cards=["A", "E"])
    assert out["card_id"] == ["A", "B", "C", "E", "F"] and out["TransactionID"]       # ids the registry digest collects
    assert tg.tools["fused_precedent"](card_ids=["A"], text="x", t0=str(T0)) == [{"case_id": "CC-1"}]
    assert seen == [None, "TGIMPL"]
    monkeypatch.setattr(fusion, "ring_component", real_ring)
    monkeypatch.setattr(fusion, "fused_precedent", real_prec)


def test_precedent_signal_only_for_cross_card_precedent():
    params = {"card_ids": ["A"], "text": "t", "t0": "2017-01-20 12:00:00", "k": 5}
    own = [{"case_id": "CC-1", "path": "card", "via": ["card"], "through": [], "outcome": "confirmed_fraud", "pattern": "p",
            "similarity": 0.9}]
    assert fusion.precedent_signal(own, params) is None
    cross = own + [{"case_id": "CC-4", "path": "r_email", "via": ["r_email:ring.example"], "through": ["B"], "outcome": "cleared",
                    "pattern": "none", "similarity": 0.8}]
    s = fusion.precedent_signal(cross, params)
    assert s.llr == 0.0 and s.group == "history" and s.entity_ids == ["CC-4", "B"]
    assert "recipient email domain ring.example" in s.claim and s.how == {"tool": "fused_precedent", "params": params}
    assert chr(0x2014) not in s.claim and chr(0x2013) not in s.claim


def test_ring_signal_names_cards_elements_and_window():
    params = {"card_id": "A", "t_from": "2016-12-21 12:00:00", "t_to": "2017-01-20 12:00:00", "template_name": "", "template_cards": []}
    comp = {"card_id": ["A", "B", "C"], "elements": {"r_email:ring.example": ["A", "B", "C"]},
            "element_txns": {"r_email:ring.example": ["1001", "1002", "1003"]}, "n_components": 2, "largest": 3}
    s = fusion.ring_signal(comp, "A", params)
    assert s.llr == 0.0 and s.group == "network" and s.entity_ids == ["B", "C", "1001", "1002", "1003"]
    assert "component of 3 cards" in s.claim and "2016-12-21 to 2017-01-20" in s.claim and s.how["tool"] == "ring_component"
    assert fusion.ring_signal(dict(comp, card_id=["A", "B"]), "A", params) is None
