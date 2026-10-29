"""Backtests for the typology detectors (kavach/detectors/typologies.py) over the bank's closed cases.

    python -m kavach.backtest [name|all] [--pop N] [--workers W] [--duck PATH] [--january]

For each detector the same fetch and rule functions the agent runs are replayed, on the DuckDB lane, over
  * every closed case's first transaction (labels from the closed-case table: confirmed_fraud uses first_fraud_txn_id or
    the first of txn_ids; cleared uses the single txn in txn_ids, or the case pack's flagged txn when the bank left
    txn_ids empty, matched on card_id and opened_at), and
  * a reference sample of transactions before 2017 that are not in any confirmed-fraud case (reservoir, seed 42).
Every grid point gets support (confirmed-fraud cases hit), cleared hits (legitimate customers it would have acted
against), population fire rate, lift = P(fire | fraud) / P(fire | reference) and precision on the closed cases. The
chosen point maximises lift subject to cleared hits <= 3 and support >= 15 ("validated" when lift >= 3); otherwise the
best weak point is kept and its runtime LLR is capped at 0.5. Output: detectors/backtests/<name>.json and the table in
docs/round2/backtests.md.
"""
import argparse
import itertools
import json
import math
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

from kavach.config import ROOT
from kavach.detectors import typologies as ty

OUT_DIR = ty.BACKTEST_DIR
DOC = ROOT / "docs" / "round2" / "backtests.md"
REF_BEFORE = "2017-01-01"
POP_N = {"dormant_wake": 200_000, "micro_charges": 200_000, "micro_charges_peers": 200_000, "recipient_fanin": 200_000,
         "common_origin": 20_000, "proxy_new_burst": 200_000, "proxy_new_burst_ring": 200_000, "amount_ladder": 200_000,
         "amount_ladder_ring": 200_000}          # common_origin costs up to ~20 tool calls per anchor; see backtests.md

ALL_FIELDS = ty.DEFAULTS["common_origin"]["fields"]
GRIDS = {
    "dormant_wake": {"gap_days": [30, 45, 60, 90], "min_online": [2, 3], "burst_hours": [48], "above_max": [0, 1.0, 1.5],
                     "min_prior": [3, 5]},
    "micro_charges": {"max_amt": [5.0, 6.0, 10.0], "min_n": [3, 4, 5], "days": [7, 14, 30], "big_amt": [20.0], "habit_max": [0, 2]},
    "micro_charges_peers": {"max_amt": [5.0, 10.0], "min_cards": [3, 5, 8], "days": [7], "tol": [0.001]},
    "recipient_fanin": {"min_cards": [3, 5, 8], "days": [7, 14, 30], "ratio": [5.0], "template": [False, True],
                        "fields": [("R_emaildomain",), ("R_emaildomain", "P_emaildomain")]},
    "common_origin": {"days": [7, 14, 21], "min_cards": [3, 5], "max_share": [0.005, 0.01, 0.02], "min_frac": [0.3, 0.5, 0.7],
                      "rarity_days": [30], "use_device_new": [False],
                      "fields": [ALL_FIELDS, tuple(f for f in ALL_FIELDS if f != "product_band")]},
    "proxy_new_burst": {"min_n": [2, 3], "min_amt": [100.0, 200.0], "hours": [24, 72], "require_unseen": [False, True]},
    "proxy_new_burst_ring": {"min_n": [2], "min_amt": [100.0, 200.0], "hours": [72], "require_unseen": [False], "min_other": [2, 3, 5],
                             "days": [14]},
    "amount_ladder": {"min_n": [4], "min_steps": [2, 3], "ratio": [1.5, 1.8], "min_max": [500.0, 1000.0], "days": [7, 14]},
    "amount_ladder_ring": {"min_n": [4], "min_steps": [2, 3], "ratio": [1.5], "min_max": [1000.0], "days": [14], "min_other": [2, 3, 5],
                           "ring_days": [14]},
}
SINGLE_OF = {"proxy_new_burst_ring": ("min_n", "min_amt", "hours", "require_unseen"),
             "amount_ladder_ring": ("min_n", "min_steps", "ratio", "min_max", "days")}


def grid_points(name: str) -> list[dict]:
    g = GRIDS[name]
    keys = list(g)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(g[k] for k in keys))]


# ------------------------------------------------------------------------------------------------ tools
class BacktestTools:
    """The DuckDB lane's tool functions (kavach.duckq, kavach.duckq_ext) behind the same call() interface as
    kavach.tools.ToolRegistry, memoised (the backtest repeats identical calls across anchors) and without the per-case
    bounds (a backtest is thousands of cases)."""

    def __init__(self):
        from kavach import duckq, duckq_ext
        self._impls = (duckq, duckq_ext)
        self.calls = 0
        self.hits = 0
        self._memo = {}
        self.step = 0

    @staticmethod
    def _key(name, kw):
        def norm(v):
            if isinstance(v, (list, tuple)):
                return tuple(norm(x) for x in v)
            if isinstance(v, pd.Timestamp):
                return str(v)
            return v
        return (name,) + tuple(sorted((k, norm(v)) for k, v in kw.items()))

    def call(self, name, **kw):
        k = self._key(name, kw)
        if k in self._memo:
            self.hits += 1
            return self._memo[k]
        fn = getattr(self._impls[0], name, None) or getattr(self._impls[1], name)
        out = fn(**kw)
        self.calls += 1
        if len(self._memo) > 20000:
            self._memo.clear()
        self._memo[k] = out
        return out

    def card_facts(self, card_id) -> dict:
        """{"h": card_history(card_id)} shared by every anchor on the card, so the rules' per-card caches are reused."""
        if getattr(self, "_card", (None,))[0] != card_id:
            self._card = (card_id, {"h": self.call("card_history", card_id=card_id)})
        return self._card[1]

    @staticmethod
    def ref(name, **kw):
        from kavach.tools import ToolRegistry
        return ToolRegistry.ref(name, **kw)


# ------------------------------------------------------------------------------------------------ data
def _connect(duck_path):
    """Point kavach.duckq at the requested index (read-only)."""
    from kavach import duckq
    from kavach.data.duck import connect
    if duck_path:
        import duckdb
        duckq._con = duckdb.connect(str(duck_path), read_only=True)
    elif duckq._con is None:
        duckq._con = connect()
    return duckq


def labels(duck_path=None) -> pd.DataFrame:
    """One anchor transaction per closed case: case_id, outcome, tid, card_id."""
    dq = _connect(duck_path)
    cc = dq.q("select case_id, outcome, card_id, opened_at, first_fraud_txn_id, txn_ids from closed")
    cols = [r[0] for r in dq.con().execute("describe cases").fetchall()]
    pack = dq.q("select card_id, opened_at, flagged_txn_id from cases" + (" where round = 1" if "round" in cols else ""))
    pk = {(r.card_id, str(r.opened_at)): r.flagged_txn_id for r in pack.itertuples()}

    def s(v) -> str:
        return "" if v is None or (isinstance(v, float) and math.isnan(v)) or str(v) in ("nan", "None") else str(v)

    def anchor(r):
        if r.outcome == "confirmed_fraud" and s(r.first_fraud_txn_id):
            return int(float(s(r.first_fraud_txn_id)))
        ids = [x for x in s(r.txn_ids).split("|") if x]
        if ids:
            return int(ids[0])
        f = s(pk.get((r.card_id, str(r.opened_at))))
        return int(float(f)) if f else None
    cc["tid"] = [anchor(r) for r in cc.itertuples()]
    return cc[cc.tid.notna()].astype({"tid": "int64"})[["case_id", "outcome", "card_id", "tid"]].reset_index(drop=True)


def anchor_rows(tids, duck_path=None) -> pd.DataFrame:
    dq = _connect(duck_path)
    from kavach.duckq import TXN_COLS
    return dq.q(f"select {TXN_COLS} from txn where TransactionID in (select unnest(?)) order by ts", [int(t) for t in tids])


def population(n: int, duck_path=None, seed: int = 42) -> list[int]:
    dq = _connect(duck_path)
    df = dq.q(f"""select TransactionID from (select TransactionID from txn where ts < '{REF_BEFORE}'
                   and TransactionID not in (select TransactionID from closed_txn where outcome = 'confirmed_fraud'))
                  using sample reservoir({int(n)} rows) repeatable ({int(seed)})""")
    return sorted(int(x) for x in df.TransactionID)


# ------------------------------------------------------------------------------------------------ evaluation
def _fetch_params(name: str, grid: list[dict]) -> dict:
    """Widest values over the grid, so one fetch serves every grid point."""
    g = GRIDS[name]
    if name == "common_origin":
        variants = sorted({(tuple(f), ms, mc) for f in g["fields"] for ms in g["max_share"] for mc in g["min_cards"]})
        return {**ty.DEFAULTS[name], "days": max(g["days"]), "max_share_fetch": max(g["max_share"]),
                "min_cards_fetch": min(g["min_cards"]), "variants": variants, "rarity_days": g["rarity_days"][0],
                "use_device_new": False, "fields": ALL_FIELDS}
    if name == "recipient_fanin":
        return {"fields": ("R_emaildomain", "P_emaildomain"), "days_list": tuple(g["days"]), "min_cards": min(g["min_cards"]),
                "ratio": min(g["ratio"])}
    if name == "micro_charges_peers":
        return {"max_amt": max(g["max_amt"]), "days": max(g["days"]), "tol": g["tol"][0]}
    if name in SINGLE_OF:
        keys = SINGLE_OF[name]
        variants = [dict(v) for v in sorted({tuple((k, gp[k]) for k in keys) for gp in grid})]
        out = {"single_variants": variants}
        if name == "proxy_new_burst_ring":
            out["days"] = max(g["days"])
        else:
            out.update(days=max(g["days"]), ring_days=max(g["ring_days"]))
        return out
    return {}


def _brief(hit: dict) -> dict:
    """Matched transaction and card ids of one hit (kept small)."""
    if not hit:
        return {}
    txns = hit.get("burst_ids") or hit.get("ids") or (hit.get("origin_ids", []) + hit.get("own_run_ids", []))
    if hit.get("ring_ids"):
        txns = list(txns) + list(hit["ring_ids"])
    cards = hit.get("cards", [])
    extra = {k: hit[k] for k in ("value", "field", "gap_days", "n", "k") if k in hit}
    return {"txns": [str(x) for x in txns][:12], "cards": list(cards)[:12], **extra}


def evaluate_anchor(name: str, T, a: dict, grid: list[dict], fp: dict) -> dict:
    """grid index -> brief hit, for one anchor."""
    out = {}
    if name in ("dormant_wake", "micro_charges"):
        facts = T.card_facts(a["card_id"])           # same frame card_history returns; rule caches are anchor-independent
        rule = ty.dormant_rule if name == "dormant_wake" else ty.micro_rule
    elif name == "micro_charges_peers":
        facts, rule = ty.micro_peers_fetch(T, a, fp), ty.micro_peers_rule
    elif name == "recipient_fanin":
        facts, rule = ty.fanin_fetch(T, a, fp), ty.fanin_rule
    elif name in ("proxy_new_burst", "amount_ladder"):
        facts = T.card_facts(a["card_id"])
        rule = ty.proxy_burst_rule if name == "proxy_new_burst" else ty.ladder_rule
    elif name == "proxy_new_burst_ring":
        facts, rule = ty.proxy_ring_fetch(T, a, fp), ty.proxy_ring_rule
    elif name == "amount_ladder_ring":
        facts, rule = ty.ladder_ring_fetch(T, a, fp), ty.ladder_ring_rule
    else:
        facts, rule = ty.origin_fetch(T, a, fp), ty.origin_rule
    for i, p in enumerate(grid):
        h = rule(facts, a, p)
        if h:
            out[i] = _brief(h)
    return out


def _worker_init(duck_path):
    _connect(duck_path)


def _worker(task):
    name, rows, grid = task
    fp = _fetch_params(name, grid)
    T = BacktestTools()
    res = []
    for a in rows:
        a["ts"] = pd.Timestamp(a["ts"])
        try:
            res.append((int(a["TransactionID"]), evaluate_anchor(name, T, a, grid, fp), None))
        except Exception as e:                       # recorded, never silently dropped
            res.append((int(a["TransactionID"]), {}, f"{type(e).__name__}: {str(e)[:120]}"))
    return res, T.calls, T.hits


def _chunks(rows: pd.DataFrame, by: str, n: int) -> list[list[dict]]:
    rows = rows.sort_values([by, "ts"]) if by else rows
    recs = rows.to_dict("records")
    size = max(1, math.ceil(len(recs) / n))
    out, i = [], 0
    while i < len(recs):
        j = min(len(recs), i + size)
        if by:                                            # keep one card (or one day) inside one chunk for the memo
            while j < len(recs) and recs[j][by] == recs[j - 1][by]:
                j += 1
        out.append(recs[i:j])
        i = j
    return out


def replay(name, rows: pd.DataFrame, grid, workers: int, duck_path=None, log=print) -> tuple[dict, dict, int]:
    """TransactionID -> {grid index: brief}; errors; tool calls made."""
    if not len(rows):
        return {}, {}, 0
    rows = rows.copy()
    rows["day"] = rows.ts.dt.normalize()
    by = "card_id" if name in ("dormant_wake", "micro_charges", "proxy_new_burst", "proxy_new_burst_ring", "amount_ladder",
                               "amount_ladder_ring") else "day"
    tasks = [(name, ch, grid) for ch in _chunks(rows, by, max(1, workers * 6))]
    fired, errors, calls = {}, {}, 0
    t0 = time.time()
    if workers <= 1:
        _worker_init(duck_path)
        results = map(_worker, tasks)
    else:
        pool = Pool(workers, initializer=_worker_init, initargs=(duck_path,))
        results = pool.imap_unordered(_worker, tasks)
    for k, (res, c, _) in enumerate(results, 1):
        calls += c
        for tid, hits, err in res:
            fired[tid] = hits
            if err:
                errors[tid] = err
        if k % max(1, len(tasks) // 10) == 0:
            log(f"    {name}: {k}/{len(tasks)} chunks, {time.time() - t0:.0f}s")
    if workers > 1:
        pool.close()
        pool.join()
    return fired, errors, calls


def lift_ci95(f: int, nf: int, r: int, nr: int) -> list | None:
    """Approximate 95% interval of a ratio of two proportions (log scale); None when either side has no hits."""
    if not f or not r or not nf or not nr:
        return None
    se = math.sqrt(max(1 / f - 1 / nf + 1 / r - 1 / nr, 0.0))
    lift = (f / nf) / (r / nr)
    return [round(lift * math.exp(-1.96 * se), 2), round(lift * math.exp(1.96 * se), 2)]


def summarise(name, grid, lab: pd.DataFrame, fired_closed: dict, fired_pop: dict, n_pop: int) -> tuple[list, dict]:
    fr = lab[lab.outcome == "confirmed_fraud"]
    cl = lab[lab.outcome == "cleared"]
    nf, nc = len(fr), len(cl)
    results = []
    for i, p in enumerate(grid):
        f_hits = sum(1 for t in fr.tid if i in fired_closed.get(int(t), {}))
        c_hits = sum(1 for t in cl.tid if i in fired_closed.get(int(t), {}))
        p_hits = sum(1 for h in fired_pop.values() if i in h)
        pf, pp = f_hits / nf if nf else 0.0, p_hits / n_pop if n_pop else 0.0
        lift = (pf / max(pp, 0.5 / n_pop)) if f_hits else 0.0
        results.append({"i": i, "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()},
                        "support": f_hits, "cleared_hits": c_hits, "pop_hits": p_hits,
                        "p_fraud": round(pf, 6), "p_cleared": round(c_hits / nc, 6) if nc else 0.0, "p_pop": round(pp, 7),
                        "lift": round(lift, 3), "lift_ci95": lift_ci95(f_hits, nf, p_hits, n_pop), "lift_note": "population had no hits; lift uses 0.5 hits as the floor" if f_hits and not p_hits else "",
                        "precision_closed": round(f_hits / (f_hits + c_hits), 3) if f_hits + c_hits else None})
    return results, decide(name, results)


def decide(name, results) -> dict:
    """Choose the grid point and the status from the per-grid rows.
    validated: lift >= 3 at the best point with cleared hits <= 3 and support >= 15 (LLR min(2, 0.5 ln lift));
    weak:      a point with cleared hits <= 3 exists but misses that bar, or the rule never fires on confirmed fraud
               (no example of the shape in the closed cases: LLR 0); LLR capped at 0.5;
    rejected:  it fires on >= 15 frauds but every grid point also acts against > 3 cleared customers: the agent does not run it."""
    # a usable point fires relatively more often on confirmed fraud than on cleared alerts (p_fraud > p_cleared)
    ok = [r for r in results if r["cleared_hits"] <= ty.VALIDATED["max_cleared"] and r["support"] >= ty.VALIDATED["min_support"]
          and r["p_fraud"] > r["p_cleared"]]
    cand = [r for r in results if r["cleared_hits"] <= ty.VALIDATED["max_cleared"] and r["support"] >= 1 and r["p_fraud"] > r["p_cleared"]]
    if ok:
        best = max(ok, key=lambda r: (r["lift"], r["support"]))
        status = "validated" if best["lift"] >= ty.VALIDATED["min_lift"] else "weak"
        reason = ("highest lift among grid points with cleared hits <= 3 and support >= 15"
                  + ("" if status == "validated" else f"; lift {best['lift']:.2f} is below 3, so it stays a weak signal"))
    elif cand:
        best = max(cand, key=lambda r: (r["support"] >= 5, r["lift"], r["support"]))
        status = "weak"
        reason = (f"no grid point reaches support >= 15 with cleared hits <= 3; kept the best weak point "
                  f"(support {best['support']}, lift {best['lift']:.2f}); runtime LLR capped at {ty.WEAK_CAP}")
    else:
        dflt = {k: (list(v) if isinstance(v, tuple) else v) for k, v in ty.DEFAULTS[name].items()}
        best = next((r for r in results if all(r["params"].get(k) == v for k, v in dflt.items() if k in r["params"])), results[0])
        top = max(results, key=lambda r: (r["lift"], r["support"]))
        if max(r["support"] for r in results) >= ty.VALIDATED["min_support"]:
            status = "rejected"
            reason = (f"every grid point acts against more than {ty.VALIDATED['max_cleared']} cleared (legitimate) customers; the best "
                      f"grid lift is {top['lift']:.2f} (support {top['support']}, cleared {top['cleared_hits']}, population "
                      f"{top['pop_hits']}); the default point is reported; REJECTED: the agent does not run it")
        else:
            status = "weak"
            if max(r["support"] for r in results) == 0:
                why = "the rule never fires on a confirmed-fraud closed case at any grid point: the closed cases hold no example of this shape"
            else:
                why = (f"its few closed-case hits (best: support {top['support']}, cleared {top['cleared_hits']}) fall at least as often on "
                       "cleared alerts as on confirmed fraud")
            reason = (f"{why}, so its lift cannot be measured; default parameters kept, runtime LLR 0 (see the January forward fires)")
    chosen = {**{k: best.get(k) for k in ("params", "support", "cleared_hits", "pop_hits", "p_fraud", "p_cleared", "p_pop", "lift",
                                           "lift_ci95", "lift_note", "precision_closed")}, "status": status, "reason": reason}
    chosen["llr"] = 0.0 if status == "rejected" else ty.lift_to_llr(chosen)
    chosen["grid_index"] = best["i"]
    return chosen


def rechoose(name: str) -> dict:
    """Re-apply decide() to a stored backtest file without re-running it. Refuses when the chosen grid point moves, because
    the stored hit lists belong to the previously chosen point."""
    path = OUT_DIR / f"{name}.json"
    b = json.loads(path.read_text(encoding="utf-8"))
    for r in b["results"]:
        r.setdefault("lift_ci95", lift_ci95(r["support"], b["n_fraud"], r["pop_hits"], b["n_population"]))
    new = decide(name, b["results"])
    if new["grid_index"] != b["chosen"]["grid_index"]:
        raise SystemExit(f"{name}: the chosen grid point moves ({b['chosen']['grid_index']} -> {new['grid_index']}); re-run instead")
    b["chosen"] = new
    path.write_text(json.dumps(b, indent=1, default=str), encoding="utf-8")
    write_doc()
    return new


def hit_lists(lab, fired_closed, fired_pop, gi) -> dict:
    out = {"fraud": [], "cleared": [], "population_sample": []}
    for r in lab.itertuples():
        h = fired_closed.get(int(r.tid), {}).get(gi)
        if h is not None:
            key = "fraud" if r.outcome == "confirmed_fraud" else "cleared"
            out[key].append({"case_id": r.case_id, "txn": str(r.tid), "card": r.card_id, "matched_txns": h.get("txns", []),
                             "matched_cards": h.get("cards", []), **{k: v for k, v in h.items() if k not in ("txns", "cards")}})
    for tid, hs in sorted(fired_pop.items()):
        if gi in hs and len(out["population_sample"]) < 25:
            out["population_sample"].append({"txn": str(tid), "matched_txns": hs[gi].get("txns", []), "matched_cards": hs[gi].get("cards", [])})
    return out


def january_fires(name, params, workers, duck_path=None, log=print) -> dict:
    """Where the chosen rule fires in January (the month under investigation): every January transaction as an anchor
    (common_origin: skipped, too costly per anchor; the 18 cases are run through the agent instead)."""
    if name == "common_origin":
        return {"skipped": "per-anchor cost; see the per-case run in STATUS.md"}
    dq = _connect(duck_path)
    from kavach.duckq import TXN_COLS
    rows = dq.q(f"select {TXN_COLS} from txn where ts >= '{REF_BEFORE}' order by ts")
    fired, errors, _ = replay(name, rows, [params], workers, duck_path, log)
    hit_t = sorted(t for t, h in fired.items() if 0 in h)
    cards = sorted(set(rows[rows.TransactionID.isin(hit_t)].card_id))
    seeded = [t for t in hit_t if t >= 3660000]
    return {"anchors": int(len(rows)), "fired_txns": len(hit_t), "fired_cards": len(cards), "cards": cards[:60],
            "fired_txns_in_block_3660000_plus": len(seeded), "errors": len(errors)}


def run(name: str, duck_path=None, pop_n: int | None = None, workers: int | None = None, january: bool = True, write: bool = True,
        labels_df: pd.DataFrame | None = None, log=print) -> dict:
    t_start = time.time()
    workers = workers or max(1, min(8, (os.cpu_count() or 2) // 2))
    grid = grid_points(name)
    lab = labels(duck_path) if labels_df is None else labels_df
    log(f"[{name}] {len(grid)} grid points, {len(lab)} closed cases ({(lab.outcome == 'confirmed_fraud').sum()} fraud), workers {workers}")
    rows = anchor_rows(lab.tid.unique(), duck_path)
    missing = sorted(set(lab.tid) - set(rows.TransactionID))
    fired_c, err_c, calls_c = replay(name, rows, grid, workers, duck_path, log)
    n_pop = POP_N[name] if pop_n is None else pop_n
    pop_t = population(n_pop, duck_path) if n_pop else []
    prow = anchor_rows(pop_t, duck_path) if pop_t else rows.iloc[0:0]
    log(f"[{name}] population sample {len(prow)}")
    fired_p, err_p, calls_p = replay(name, prow, grid, workers, duck_path, log)
    results, chosen = summarise(name, grid, lab, fired_c, fired_p, max(len(prow), 1))
    hits = hit_lists(lab, fired_c, fired_p, chosen["grid_index"])
    from kavach import duckq
    try:
        dv = duckq.dataset_version()
    except Exception:
        dv = {"version": "unknown"}
    out = {"name": name, "run_at": pd.Timestamp.now().isoformat(timespec="seconds"), "duck_path": str(duck_path or _db_name()),
           "dataset_version": dv.get("version", "unknown"),
           "n_closed": int(len(lab)), "n_fraud": int((lab.outcome == "confirmed_fraud").sum()), "n_cleared": int((lab.outcome == "cleared").sum()),
           "closed_anchor_missing": [str(x) for x in missing[:20]], "n_population": int(len(prow)),
           "population": f"reservoir sample (seed 42) of transactions before {REF_BEFORE} not in any confirmed-fraud closed case",
           "grid": GRIDS[name] if not any(isinstance(v[0], tuple) for v in GRIDS[name].values()) else
           {k: [list(x) if isinstance(x, tuple) else x for x in v] for k, v in GRIDS[name].items()},
           "chosen": chosen, "hits": hits, "results": results,
           "errors": {"closed": len(err_c), "population": len(err_p), "examples": list({**err_c, **err_p}.items())[:5]},
           "tool_calls": calls_c + calls_p}
    if january:
        out["january"] = january_fires(name, {**ty.DEFAULTS[name], **{k: (tuple(v) if isinstance(v, list) else v)
                                                                       for k, v in chosen["params"].items()}}, workers, duck_path, log)
    out["seconds"] = round(time.time() - t_start, 1)
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"{name}.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        write_doc()
    log(f"[{name}] chosen {chosen['params']} status {chosen['status']} lift {chosen['lift']} support {chosen['support']} "
        f"cleared {chosen['cleared_hits']} pop {chosen['pop_hits']}/{len(prow)} llr {chosen['llr']} ({out['seconds']}s)")
    return out


def _db_name():
    from kavach.config import DUCK_PATH
    return DUCK_PATH.name


# ------------------------------------------------------------------------------------------------ report
WHY = {
    "common_origin": "Common point of purchase: several cards touch one rare entity (recipient or purchaser email domain, card-present "
                     "region, product and amount band, proxied access signature, account key) and each starts loss-like activity "
                     "within days of that touch.",
    "dormant_wake": "An account (holder_key with history, else the card) silent for weeks that wakes with an online burst or an "
                    "amount above anything it did before.",
    "micro_charges": "Slow probing: several online charges of a few dollars spread over days, with no larger purchase after them "
                     "(the one-hour R5 test is excluded) on a card with no micro-charge habit.",
    "micro_charges_peers": "The same micro amount, to the cent, on several cards within a week.",
    "recipient_fanin": "A recipient (or purchaser) email domain suddenly paid by many distinct cards against its own 90-day baseline.",
    "proxy_new_burst": "Two or more online purchases of one product, each above a floor, within hours on one card, every one from a "
                       "device marked New behind an anonymous proxy (optionally on a card that never used the product).",
    "proxy_new_burst_ring": "The same New-device anonymous-proxy purchase template on several other cards within two weeks, with no "
                            "device shared with the case card (coordinated across customers, R9).",
    "amount_ladder": "A stepped cash-out: consecutive online purchases of one product growing by a fixed ratio several times in a "
                     "row up to a high top amount, often repeated at a cap.",
    "amount_ladder_ring": "The same ladder top (cap) reached by ladders on several other cards within two weeks (coordinated, R9).",
}


def write_doc() -> None:
    rows, notes = [], []
    for n in ty.NAMES:
        p = OUT_DIR / f"{n}.json"
        if not p.exists():
            continue
        b = json.loads(p.read_text(encoding="utf-8"))
        c = b["chosen"]
        prm = ", ".join(f"{k}={'/'.join(v) if isinstance(v, list) else v}" for k, v in c["params"].items())
        jan = b.get("january", {})
        jan_s = jan.get("skipped") and "n/a" or f"{jan.get('fired_cards', '?')} cards ({jan.get('fired_txns_in_block_3660000_plus', '?')} block txns)"
        ci = c.get("lift_ci95") or lift_ci95(c["support"], b["n_fraud"], c["pop_hits"], b["n_population"])
        lift_s = f"{c['lift']:.2f}" + (f" ({ci[0]} to {ci[1]})" if ci else "")
        rows.append(f"| `{n}` | {prm} | {c['support']} / {b['n_fraud']} | {c['cleared_hits']} / {b['n_cleared']} | "
                    f"{c['pop_hits']} / {b['n_population']} | {lift_s} | {c['precision_closed'] if c['precision_closed'] is not None else 'n/a'} | "
                    f"{c['status']} | {c['llr']} | {jan_s} |")
        fr = ", ".join(f"{h['case_id']} (txn {h['txn']}, card {h['card']})" for h in b["hits"]["fraud"][:8]) or "none"
        cl = ", ".join(f"{h['case_id']} (txn {h['txn']}, card {h['card']})" for h in b["hits"]["cleared"][:8]) or "none"
        ln = f" Note: {c['lift_note']}, so the lift is an upper reading, not an estimate." if c.get("lift_note") else ""
        notes.append(f"### `{n}`\n\n{WHY[n]}\n\n- Chosen: {c['reason']}.{ln}\n- Confirmed-fraud hits (first {min(8, len(b['hits']['fraud']))}): {fr}.\n"
                     f"- Cleared hits (legitimate customers it would have acted against): {cl}.\n"
                     f"- Grid: {len(b['results'])} points; population sample {b['n_population']:,}; tool calls {b['tool_calls']:,}; "
                     f"errors {b['errors']['closed'] + b['errors']['population']}; run {b['seconds']} s on `{b['duck_path']}` "
                     f"(dataset version {b['dataset_version']}).\n")
    head = ("# Typology backtests (S2)\n\nGenerated by `python -m kavach.backtest all` (kavach/backtest.py) from the closed-case history "
            "(5,585 cases) and a reference sample of non-fraud transactions before January 2017. Thresholds are chosen on a small grid "
            "by the highest lift with cleared hits <= 3 and support >= 15; a rule is `validated` when that lift is >= 3, and its "
            "runtime LLR is min(2.0, 0.5 ln lift). Anything else is `weak`: LLR min(0.5, 0.5 ln lift), and 0 when the rule never "
            "fires on confirmed fraud. The per-hit transaction and card ids are in `detectors/backtests/<name>.json`.\n\n"
            "| detector | chosen thresholds | fraud hits | cleared hits | population hits | lift | precision (closed) | status | LLR | January fires |\n"
            "|---|---|---|---|---|---|---|---|---|---|\n")
    DOC.write_text(head + "\n".join(rows) + "\n\n" + "\n".join(notes), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m kavach.backtest")
    ap.add_argument("name", nargs="?", default="all", choices=("all",) + ty.NAMES)
    ap.add_argument("--pop", type=int, default=None, help="population sample size (default per detector, see POP_N)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--duck", default=None, help="DuckDB index (default kavach.config.DUCK_PATH)")
    ap.add_argument("--no-january", action="store_true")
    a = ap.parse_args(argv)
    names = ("dormant_wake", "micro_charges", "micro_charges_peers", "recipient_fanin", "proxy_new_burst", "proxy_new_burst_ring",
             "amount_ladder", "amount_ladder_ring", "common_origin") if a.name == "all" else (a.name,)
    lab = labels(a.duck)
    for n in names:
        run(n, duck_path=a.duck, pop_n=a.pop, workers=a.workers, january=not a.no_january, labels_df=lab,
            log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
