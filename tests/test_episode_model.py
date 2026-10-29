"""Episode membership model: numpy logistic regression fitted on (synthetic) closed cases, and the shipped model file."""
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest

from kavach import episode_model as em


def _synthetic_db(path, n_cards=80, seed=3):
    rng = np.random.default_rng(seed)
    txn, closed, ctxn = [], [], []
    tid = 1
    t_start = pd.Timestamp("2016-08-01")
    for c in range(n_cards):
        card = f"C{c:05d}-K1"
        hk = f"{c}|264.0|10"
        for i in range(40):                                   # normal history: in person, product W, home region
            txn.append((tid, card, t_start + pd.Timedelta(hours=36 * i + rng.uniform(0, 5)), float(rng.lognormal(3.9, 0.4)), "W",
                        "in_person", 264, None, None, float(rng.uniform(0, 0.3)), hk))
            tid += 1
        t_ep = t_start + pd.Timedelta(days=62)
        # background noise inside the episode window
        for j in range(4):
            txn.append((tid, card, t_ep - pd.Timedelta(hours=40 + 5 * j), float(rng.lognormal(3.9, 0.4)), "W", "in_person", 264, None,
                        None, float(rng.uniform(0, 0.3)), hk))
            tid += 1
        members = []
        for k in range(3):                                    # the episode: online C, large, one device, New, close in time
            txn.append((tid, card, t_ep + pd.Timedelta(hours=2 * k), float(rng.uniform(350, 450)), "C", "online", None,
                        "SM-X1 Build/A | Android 7 | chrome 60 | 1080x1920", "New", float(rng.uniform(0.6, 0.95)), hk))
            members.append(tid)
            tid += 1
        opened = t_ep + pd.Timedelta(hours=7)
        cid = f"CC-{c:04d}"
        closed.append((cid, card, str(opened), "confirmed_fraud", "card_not_present_new_device", "|".join(map(str, members))))
        ctxn += [(cid, "confirmed_fraud", m) for m in members]
    con = duckdb.connect(str(path))
    con.execute("""create table txn (TransactionID bigint, card_id varchar, ts timestamp, TransactionAmt double, ProductCD varchar,
                   channel varchar, addr1 double, device_key varchar, id_15 varchar, risk_score double, holder_key varchar)""")
    con.executemany("insert into txn values (?,?,?,?,?,?,?,?,?,?,?)", [tuple(x if not isinstance(x, pd.Timestamp) else x.to_pydatetime()
                                                                         for x in r) for r in txn])
    con.execute("create table closed (case_id varchar, card_id varchar, opened_at varchar, outcome varchar, pattern varchar, txn_ids varchar)")
    con.executemany("insert into closed values (?,?,?,?,?,?)", closed)
    con.execute("create table closed_txn (case_id varchar, outcome varchar, TransactionID bigint)")
    con.executemany("insert into closed_txn values (?,?,?)", ctxn)
    con.close()
    return pd.DataFrame(txn, columns=["TransactionID", "card_id", "ts", "amt", "ProductCD", "channel", "addr1", "device_key", "id_15",
                                      "risk_score", "holder_key"])


def test_logistic_regression_recovers_the_direction():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(2000, 2))
    y = (rng.random(2000) < 1 / (1 + np.exp(-(2.0 * X[:, 0] - 1.0 * X[:, 1])))).astype(float)
    w, mu, sd = em._fit_lr(X, y)
    assert w[1] > 1.0 and w[2] < -0.5


def test_fit_on_synthetic_closed_cases_and_members(tmp_path):
    db = tmp_path / "syn.duckdb"
    t = _synthetic_db(db)
    out = tmp_path / "model.json"
    res = em.fit(duck_path=db, out=out, seed=2)
    assert out.exists()
    for name in ("shape", "score"):
        v = res["variants"][name]
        assert 0 < v["threshold"] < 1
        assert v["heldout"]["auc_median"] > 0.9 and v["heldout"]["exact"] >= 0.8
    assert "risk_score" not in res["variants"]["shape"]["features"]
    assert res["variants"]["score"]["features"][-1] == "risk_score"
    # runtime: the flagged transaction is always included and the episode members are found among the candidates
    em._MODEL = res
    try:
        card = t[t.card_id == "C00001-K1"].sort_values("ts").reset_index(drop=True)
        mem = list(card[card.channel == "online"].TransactionID)
        flagged = mem[-1]
        ctx = SimpleNamespace(h=card)
        cand = em.window(card, card[card.TransactionID == flagged].ts.iloc[0], card.ts.max(), long=False)
        got = em.members(ctx, cand, flagged, use_score=False)
        assert flagged in got and set(mem) <= set(got)
        assert len(got) <= len(mem) + 1
    finally:
        em._MODEL = None


def test_shipped_model_file_is_complete():
    m = em.load()
    assert set(m["variants"]) == {"shape", "score"}
    for name, v in m["variants"].items():
        assert len(v["coef"]) == len(v["features"]) == len(v["mean"]) == len(v["sd"])
        assert 0 < v["threshold"] < 1
        for split in ("train", "heldout"):
            assert v[split]["auc_median"] is not None and 0.5 < v[split]["auc_median"] <= 1
    assert "risk_score" not in m["variants"]["shape"]["features"]
    # the model beats the Round 1 behaviour (flagged transaction alone) on held-out closed cases
    assert m["variants"]["shape"]["heldout"]["jaccard_mean"] > m["baseline_flagged_only"]["jaccard_mean"]


@pytest.mark.parametrize("flag_ts,use", [("2016-12-20 12:00", True), ("2017-01-05 12:00", False)])
def test_build_uses_the_score_only_before_january(monkeypatch, flag_ts, use):
    from kavach import episode
    from kavach.detectors import Findings
    seen = {}

    def fake_members(ctx, cand, fid, use_score):
        seen["use_score"] = use_score
        return [fid]

    monkeypatch.setattr(em, "members", fake_members)
    t0 = pd.Timestamp(flag_ts)
    h = pd.DataFrame({"TransactionID": [1, 2], "ts": [t0 - pd.Timedelta(hours=5), t0], "amt": [10.0, 20.0],
                      "channel": ["online"] * 2, "id_15": [None] * 2, "holder_key": ["k"] * 2, "risk_score": [0.1, 0.2]})
    ctx = SimpleNamespace(h=h, f=h.iloc[1].to_dict(), t0=t0, opened=t0 + pd.Timedelta(hours=1))
    ep = episode.build(ctx, Findings(), "fraud")
    assert seen["use_score"] is use and ep.txn_ids == ["2"]
