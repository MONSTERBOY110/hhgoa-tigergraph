"""Episode membership model: which transactions in the card's recent window belong to the fraud episode.

The bank draws episodes at card level, over days, across holder keys, devices, emails and regions, and never
past the case opening (round1-postmortem.md section 6). This module learns that from the closed cases:

  candidates  every transaction on the card from LOOKBACK days before the anchor (3, or 30 when a testing ladder
              or an account with earlier confirmed fraud is involved) up to the case opened_at
  label       1 if the transaction is in the closed case's txn_ids, else 0 (same card, same window)
  model       logistic regression, numpy only, standardised features, L2 penalty, Newton steps
  threshold   chosen on the training cases to maximise exact-episode matches (predicted set == txn_ids)

Two variants are fitted: shape features only (no risk_score) and shape plus risk_score. The score variant is
used only for transactions before 2017-01-01: the bank's model was not retrained for January.

    .venv\\Scripts\\python.exe -m kavach.episode_model [duck_path]     # refit and write kavach/episode_model.json
"""
import json
import math
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

MODEL = Path(__file__).with_name("episode_model.json")
LOOKBACK_DAYS = 3        # 90th percentile span of closed episodes is 2 to 3 days for every pattern but card_testing
LOOKBACK_LONG = 30       # testing ladders and accounts with earlier confirmed fraud run longer
JANUARY = pd.Timestamp("2017-01-01")
SHAPE = ["amt_z", "prod_unseen", "chan_diff", "dev_new", "same_dev", "log_hours", "same_day", "amt_twin",
         "region_unseen", "small"]
WITH_SCORE = SHAPE + ["risk_score"]
L2 = 1.0
NEG_CAP = 200            # negatives per case used for fitting (all of them are used to choose the threshold)
COLS = ["TransactionID", "card_id", "ts", "amt", "ProductCD", "channel", "addr1", "device_key", "id_15", "risk_score",
        "holder_key"]
_MODEL = None


# ---------------------------------------------------------------- features
def _valid_holder(hk) -> bool:
    return isinstance(hk, str) and "||" not in hk and not hk.endswith("|")


def _generic(key) -> bool:
    from kavach.detectors.network import generic_device
    return generic_device(key, 0)


def window(h: pd.DataFrame, t_anchor, opened, long: bool) -> pd.DataFrame:
    """Candidate rows: the card's transactions from the look-back start up to the case opening (never after)."""
    start = pd.Timestamp(t_anchor) - timedelta(days=LOOKBACK_LONG if long else LOOKBACK_DAYS)
    end = max(pd.Timestamp(opened), pd.Timestamp(t_anchor))
    return h[(h.ts >= start) & (h.ts <= end)]


def features(h: pd.DataFrame, cand: pd.DataFrame, flag: dict, holder_ok: bool = True) -> pd.DataFrame:
    """Shape features of each candidate relative to the card's history before the window and to the flagged txn.
    h: the card's full history (sorted by ts); cand: the candidate rows; flag: the flagged (anchor) row."""
    ws = cand.ts.min()
    prior = h[h.ts < ws]
    la = np.log1p(cand.amt.astype(float).values)
    # amount z against the account's prior (holder within the card when the key is usable, else the card)
    lp = np.log1p(prior.amt.astype(float))
    card_mu, card_sd = (lp.mean(), max(lp.std() if len(lp) > 1 else 0.0, 0.25)) if len(lp) else (np.nan, np.nan)
    mu = np.full(len(cand), card_mu)
    sd = np.full(len(cand), card_sd)
    if holder_ok and len(prior):
        g = pd.DataFrame({"hk": prior.holder_key.values, "la": lp.values}).groupby("hk").la.agg(["mean", "std", "size"])
        g = g[g["size"] >= 3]
        hk = cand.holder_key.values
        for i, k in enumerate(hk):
            if _valid_holder(k) and k in g.index:
                mu[i] = g.at[k, "mean"]
                sd[i] = max(g.at[k, "std"] if not math.isnan(g.at[k, "std"]) else 0.0, 0.25)
    z = np.where(np.isnan(mu), 0.0, (la - np.nan_to_num(mu)) / np.where(np.isnan(sd), 1.0, sd))
    enough = len(prior) >= 5
    prods = set(prior.ProductCD)
    major = prior.channel.mode().iloc[0] if len(prior) else None
    regions = set(prior.addr1.dropna())
    fdev = flag.get("device_key")
    fdev = None if _generic(fdev) else fdev
    ft = pd.Timestamp(flag["ts"])
    hours = (cand.ts - ft).abs().dt.total_seconds().values / 3600
    amts = cand.amt.astype(float).values
    twin = np.zeros(len(cand))
    if len(cand) > 1:
        o = np.argsort(amts)
        s = amts[o]
        near = np.zeros(len(s), bool)
        near[1:] |= np.abs(np.diff(s)) <= 0.02 * s[1:]
        near[:-1] |= np.abs(np.diff(s)) <= 0.02 * s[:-1]
        twin[o] = near
    X = pd.DataFrame({
        "TransactionID": cand.TransactionID.astype(int).values,
        "amt_z": np.clip(z, -4, 6),
        "prod_unseen": (~cand.ProductCD.isin(prods)).values * enough,
        "chan_diff": ((cand.channel != major).values * enough) if major is not None else np.zeros(len(cand)),
        "dev_new": (cand.id_15 == "New").values.astype(float),
        "same_dev": (cand.device_key == fdev).values.astype(float) if fdev else np.zeros(len(cand)),
        "log_hours": np.log1p(hours),
        "same_day": (cand.ts.dt.date == ft.date()).values.astype(float),
        "amt_twin": twin,
        "region_unseen": ((cand.channel == "in_person") & cand.addr1.notna() & ~cand.addr1.isin(regions)).values * enough,
        "small": (cand.amt < 10).values.astype(float),
        "risk_score": cand.risk_score.astype(float).fillna(0.0).values,
    })
    return X.astype({c: float for c in WITH_SCORE})


# ---------------------------------------------------------------- logistic regression (numpy only)
def _fit_lr(X: np.ndarray, y: np.ndarray, l2: float = L2, iters: int = 25) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Z = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    w = np.zeros(Z.shape[1])
    pen = np.full(Z.shape[1], l2)
    pen[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - y) + pen * w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(pen + 1e-9)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w, mu, sd


def _prob(X: np.ndarray, m: dict) -> np.ndarray:
    Z = (X - np.array(m["mean"])) / np.array(m["sd"])
    return 1 / (1 + np.exp(-np.clip(m["intercept"] + Z @ np.array(m["coef"]), -30, 30)))


def _auc(p: np.ndarray, y: np.ndarray) -> float | None:
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return None
    r = pd.Series(p).rank().values
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


# ---------------------------------------------------------------- training data from the closed cases
def _load(duck_path=None):
    import duckdb
    from kavach.config import DUCK_PATH
    con = duckdb.connect(str(duck_path or DUCK_PATH), read_only=True)
    t = con.execute("""select TransactionID, card_id, ts, TransactionAmt amt, ProductCD, channel, cast(addr1 as integer) addr1,
                       case when replace(replace(coalesce(device_key,''),'|',''),' ','') = '' then null else device_key end device_key,
                       id_15, risk_score, holder_key from txn order by card_id, ts, TransactionID""").df()
    cc = con.execute("""select case_id, card_id, cast(opened_at as timestamp) opened, outcome, pattern, txn_ids from closed
                        where outcome = 'confirmed_fraud' and txn_ids is not null and txn_ids <> ''""").df()
    hf = con.execute("""select distinct t.holder_key, cast(c.opened_at as timestamp) opened from closed_txn ct join txn t using (TransactionID)
                        join closed c using (case_id) where ct.outcome = 'confirmed_fraud'""").df()
    con.close()
    return t, cc, hf


def build_training(duck_path=None, max_cases: int | None = None, seed: int = 1):
    """One record per closed fraud case: candidate features, labels, anchor id, whether every member is in the window."""
    t, cc, hf = _load(duck_path)
    rng = np.random.default_rng(seed)
    if max_cases and len(cc) > max_cases:
        cc = cc.sample(max_cases, random_state=seed)
    by_card = {k: g.reset_index(drop=True) for k, g in t.groupby("card_id", sort=False)}
    first_fraud = hf.groupby("holder_key").opened.min()
    out = []
    for r in cc.itertuples():
        h = by_card.get(r.card_id)
        if h is None:
            continue
        mem = {int(x) for x in str(r.txn_ids).split("|") if x}
        mrows = h[h.TransactionID.isin(mem)]
        if mrows.empty:
            continue
        anchor = mrows.iloc[int(rng.integers(len(mrows)))]
        hk = anchor.holder_key
        prior_fraud = _valid_holder(hk) and hk in first_fraud.index and first_fraud[hk] < r.opened - timedelta(days=1)
        long = bool(prior_fraud) or r.pattern == "card_testing"
        cand = window(h, anchor.ts, r.opened, long)
        X = features(h, cand, anchor.to_dict())
        y = X.TransactionID.isin(mem).values.astype(float)
        keep = (X.TransactionID != int(anchor.TransactionID)).values
        out.append({"case_id": r.case_id, "pattern": r.pattern, "anchor": int(anchor.TransactionID), "X": X[keep], "y": y[keep],
                    "complete": len(mem - set(X.TransactionID)) == 0, "n": len(mem)})
    return out


def _threshold(recs, probs) -> tuple[float, float]:
    """Threshold maximising exact matches: a case matches iff every member scores >= thr > every non-member."""
    lo, hi, ok = [], [], []
    for r, p in zip(recs, probs):
        y = r["y"] == 1
        lo.append(p[~y].max() if (~y).any() else -1.0)
        hi.append(p[y].min() if y.any() else 2.0)
        ok.append(r["complete"])
    lo, hi, ok = np.array(lo), np.array(hi), np.array(ok)
    grid = np.unique(np.concatenate([np.linspace(0.01, 0.99, 99), hi[(hi > 0) & (hi < 1)]]))
    best = max(grid, key=lambda th: (((lo < th) & (th <= hi) & ok).sum(), -abs(th - 0.5)))
    return float(best), float(((lo < best) & (best <= hi) & ok).mean())


def _evaluate(recs, probs, thr) -> dict:
    aucs, jac, exact = [], [], 0
    for r, p in zip(recs, probs):
        y = r["y"] == 1
        a = _auc(p, r["y"])
        if a is not None:
            aucs.append(a)
        pred = set(r["X"].TransactionID[p >= thr]) | {r["anchor"]}
        true = set(r["X"].TransactionID[y]) | {r["anchor"]}
        jac.append(len(pred & true) / len(pred | true))
        exact += int(pred == true and r["complete"])
    return {"auc_median": round(float(np.median(aucs)), 4) if aucs else None, "auc_mean": round(float(np.mean(aucs)), 4) if aucs else None,
            "n_auc_cases": len(aucs), "exact": round(exact / max(len(recs), 1), 4), "jaccard_mean": round(float(np.mean(jac)), 4),
            "n_cases": len(recs)}


def fit(duck_path=None, out: Path | None = MODEL, max_cases: int | None = None, seed: int = 1) -> dict:
    """Fit both variants on the closed fraud cases, choose thresholds, evaluate on a 20 percent held-out split, save."""
    recs = build_training(duck_path, max_cases, seed)
    rng = np.random.default_rng(seed)
    test = rng.random(len(recs)) < 0.2
    train = [r for r, te in zip(recs, test) if not te]
    held = [r for r, te in zip(recs, test) if te]
    result = {"fitted_on": str(duck_path or "default DUCK_PATH"), "n_cases": len(recs), "n_train": len(train), "n_heldout": len(held),
              "lookback_days": LOOKBACK_DAYS, "lookback_long_days": LOOKBACK_LONG, "l2": L2, "neg_cap": NEG_CAP,
              "complete_share": round(float(np.mean([r["complete"] for r in recs])), 4), "variants": {}}
    for name, cols in (("shape", SHAPE), ("score", WITH_SCORE)):
        Xs, ys = [], []
        for r in train:
            X, y = r["X"][cols].values, r["y"]
            neg = np.flatnonzero(y == 0)
            if len(neg) > NEG_CAP:
                sel = np.concatenate([np.flatnonzero(y == 1), rng.choice(neg, NEG_CAP, replace=False)])
                X, y = X[sel], y[sel]
            Xs.append(X)
            ys.append(y)
        w, mu, sd = _fit_lr(np.vstack(Xs), np.concatenate(ys))
        m = {"features": cols, "intercept": float(w[0]), "coef": [float(x) for x in w[1:]], "mean": [float(x) for x in mu],
             "sd": [float(x) for x in sd]}
        ptr = [_prob(r["X"][cols].values, m) for r in train]
        thr, ex_tr = _threshold(train, ptr)
        m["threshold"] = thr
        m["train"] = _evaluate(train, ptr, thr)
        m["heldout"] = _evaluate(held, [_prob(r["X"][cols].values, m) for r in held], thr)
        m["coef_by_feature"] = dict(zip(cols, [round(float(x), 3) for x in w[1:]]))
        result["variants"][name] = m
    # baseline: the flagged transaction alone (what Round 1 did when no detector fired)
    result["baseline_flagged_only"] = _evaluate(held, [np.zeros(len(r["y"])) for r in held], 0.5)
    if out:
        Path(out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    global _MODEL
    _MODEL = None
    return result


def load() -> dict:
    global _MODEL
    if _MODEL is None:
        _MODEL = json.loads(MODEL.read_text(encoding="utf-8"))
    return _MODEL


# ---------------------------------------------------------------- runtime
def scores(ctx, candidates: pd.DataFrame, flagged_id: int, use_score: bool) -> pd.Series:
    """Membership probability per candidate TransactionID."""
    if candidates is None or candidates.empty:
        return pd.Series(dtype=float)
    m = load()["variants"]["score" if use_score else "shape"]
    h = ctx.h
    flag = h[h.TransactionID == flagged_id].iloc[0].to_dict()
    holder_ok = pd.Timestamp(flag["ts"]) < JANUARY
    X = features(h, candidates, flag, holder_ok=holder_ok)
    return pd.Series(_prob(X[m["features"]].values, m), index=X.TransactionID.values)


def members(ctx, candidates: pd.DataFrame, flagged_id: int, use_score: bool) -> list[int]:
    """Episode members among the candidates: probability at or above the fitted threshold, flagged always included."""
    flagged_id = int(flagged_id)
    p = scores(ctx, candidates, flagged_id, use_score)
    thr = load()["variants"]["score" if use_score else "shape"]["threshold"]
    ids = [int(t) for t, v in p.items() if v >= thr]
    return sorted(set(ids) | {flagged_id})


if __name__ == "__main__":
    r = fit(sys.argv[1] if len(sys.argv) > 1 else None)
    for k, v in r["variants"].items():
        print(k, "threshold", round(v["threshold"], 3), "train", v["train"], "heldout", v["heldout"])
        print("   coef", v["coef_by_feature"])
    print("flagged-only baseline", r["baseline_flagged_only"], "complete share", r["complete_share"])
