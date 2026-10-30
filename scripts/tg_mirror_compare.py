"""Live check: every TigerGraph mirror of a typology tool against the DuckDB tool on real inputs from the 18 HHF cases.
Usage: PYTHONPATH=. timeout 1800 .venv/Scripts/python.exe logs/tg_mirror_compare.py [HHF-001 ...]"""
import pathlib, sys; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # run from anywhere
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

from kavach import duckq, duckq_ext as dx
from kavach.graph.client import TgTools

ONLY = sys.argv[1:]
FIELDS = sorted(dx.ENTITY_FIELDS)
NB_COLS = ["TransactionID", "card_id", "customer_id", "ts", "amt", "ProductCD", "channel", "risk_score", "addr1", "P_emaildomain",
           "R_emaildomain", "DeviceType", "id_15", "id_23", "holder_key", "card1", "card4", "card6", "M1", "M4"]   # D*, C*, dist1: a null number is 0 on the graph


def norm(v):
    if v is None or v is pd.NA or v is pd.NaT or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (pd.Timestamp, datetime, np.datetime64)):
        return pd.Timestamp(v)
    if isinstance(v, (float, np.floating)):
        return round(float(v), 6)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def diff_frames(a, b, cols):
    if list(a.columns) != list(b.columns):
        return f"columns differ: {list(a.columns)} vs {list(b.columns)}"
    if len(a) != len(b):
        return f"rows {len(a)} vs {len(b)}"
    for c in cols:
        if c not in a.columns:
            continue
        x, y = [norm(v) for v in a[c].astype(object)], [norm(v) for v in b[c].astype(object)]
        if x != y:
            i = next(i for i in range(len(x)) if x[i] != y[i])
            return f"column {c} row {i}: {x[i]!r} vs {y[i]!r}"
    return None


tg = TgTools()
res = {"same": 0, "diff": 0}
t_tg = t_duck = 0.0


def check(label, fd, ft, cols=None):
    global t_tg, t_duck
    t = time.perf_counter(); d = fd(); t_duck += time.perf_counter() - t
    t = time.perf_counter(); g = ft(); t_tg += time.perf_counter() - t
    if isinstance(d, dict):
        err = None if d == g else f"{d} vs {g}"
    else:
        err = diff_frames(d, g, cols or list(d.columns))
    res["diff" if err else "same"] += 1
    n = len(d) if not isinstance(d, dict) else d.get("cards_window")
    print(f"  {'SAME' if not err else 'DIFF'} {label} n={n}" + (f"\n    {err}" if err else ""), flush=True)


for c in duckq.case_pack(2).to_dict("records"):
    if ONLY and c["case_id"] not in ONLY:
        continue
    a = duckq.txn_detail(c["flagged_txn_id"])
    ts = pd.Timestamp(a["ts"])
    t_end = ts.normalize() + pd.Timedelta(days=1)
    print(f"{c['case_id']} card {c['card_id']} txn {c['flagged_txn_id']} {ts} {a['ProductCD']} ${a['amt']}", flush=True)
    # card_atypical_window on the case card (common_origin window), and on the neighbour cards of the R domain
    w0 = t_end - pd.Timedelta(days=28)
    check("card_atypical_window(card)", lambda: dx.card_atypical_window(c["card_id"], w0, t_end),
          lambda: tg.card_atypical_window(c["card_id"], w0, t_end), [x for x in dx.card_atypical_window([], w0, t_end).columns] +
          ["addr1", "P_emaildomain", "R_emaildomain", "id_15", "id_23", "DeviceType", "risk_score", "holder_key", "prior_max"])
    own = dx.card_atypical_window(c["card_id"], w0, t_end)
    row = dict(a, amt=a["amt"])
    for f in FIELDS:
        v = dx.entity_value(f, row)
        if v is None:
            continue
        check(f"entity_neighbors({f}={v})", lambda: dx.entity_neighbors(f, v, w0, t_end), lambda: tg.entity_neighbors(f, v, w0, t_end),
              NB_COLS + ["entity"])
        for days in (14, 30):
            check(f"entity_baseline({f}={v}, {days}d)", lambda: dx.entity_baseline(f, v, t_end, days),
                  lambda: tg.entity_baseline(f, v, t_end, days))
        vals = sorted({x for r in own.to_dict("records") if (x := dx.entity_value(f, r)) is not None} | {v})
        check(f"entity_shares({f}, {len(vals)} values)", lambda: dx.entity_shares(f, vals, t_end, 30),
              lambda: tg.entity_shares(f, vals, t_end, 30))
        if f == "R_emaildomain":
            nb = dx.entity_neighbors(f, v, t_end - pd.Timedelta(days=14), t_end)
            others = [x for x in dict.fromkeys(nb.card_id) if x != c["card_id"]][:12]
            if others:
                check(f"card_atypical_window({len(others)} R neighbour cards)", lambda: dx.card_atypical_window(others, w0, t_end),
                      lambda: tg.card_atypical_window(others, w0, t_end),
                      list(dx.card_atypical_window([], w0, t_end).columns) + ["prior_max", "id_23", "holder_key"])
    # access signature of a proxied anchor over the proxy-ring window
    if a.get("id_23") is not None and a.get("id_23") == a.get("id_23"):
        sig = f"{a['ProductCD']}|{a['id_23']}|{a['id_15'] or ''}|{a['DeviceType'] or ''}"
        check(f"entity_neighbors(access_signature ring window)", lambda: dx.entity_neighbors("access_signature", sig, t_end - pd.Timedelta(days=15), t_end + pd.Timedelta(days=14)),
              lambda: tg.entity_neighbors("access_signature", sig, t_end - pd.Timedelta(days=15), t_end + pd.Timedelta(days=14)), NB_COLS + ["entity"])
    # amount_peers without an email (micro peers: 7 days, tol 0.001; ladder ring style: 14 days)
    for days, tol in ((7, 0.001), (14, 0.001)):
        amt = round(float(a["amt"]), 2)
        check(f"amount_peers(no email, {amt}, {days}d)",
              lambda: duckq.amount_peers(a["ProductCD"], amt, ts, days=days, tol=tol).sort_values(["ts", "TransactionID"]).reset_index(drop=True),
              lambda: tg.amount_peers(a["ProductCD"], amt, ts, days=days, tol=tol), NB_COLS)
    # small_online_txns (the lead's tool for the cross-customer micro-charge ring): 210 days up to the alert, under $6
    op = pd.Timestamp(c["opened_at"])
    check("small_online_txns(210d to the alert, < $6)", lambda: dx.small_online_txns(op - pd.Timedelta(days=210), op, 6.0),
          lambda: tg.small_online_txns(op - pd.Timedelta(days=210), op, 6.0))
print(f"\nmirror checks: {res['same']} same, {res['diff']} different; time duck {t_duck:.1f}s, tg {t_tg:.1f}s")
