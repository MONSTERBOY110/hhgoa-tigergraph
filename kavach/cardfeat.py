"""Per-transaction features relative to the card's own prior history, computed in pandas.

Same definitions as kavach/features.py (the SQL version used to fit weights), so the
agent computes identical signals from whichever backend served the card history.
"""
import numpy as np
import pandas as pd


def empty_device(k) -> bool:
    return k is None or (isinstance(k, float) and np.isnan(k)) or str(k).replace("|", "").strip() == ""


def card_features(h: pd.DataFrame) -> pd.DataFrame:
    h = h.sort_values(["ts", "TransactionID"]).reset_index(drop=True).copy()
    h["device_key"] = [None if empty_device(k) else k for k in h.device_key]
    h["prior_n"] = np.arange(len(h))
    for col, name in (("ProductCD", "prior_same_product"), ("addr1", "prior_same_region"),
                      ("device_key", "prior_same_device"), ("P_emaildomain", "prior_same_pemail")):
        h[name] = h.groupby(h[col].astype(str)).cumcount()
        h.loc[h[col].isna(), name] = np.nan
    la = np.log1p(h.amt)
    h["prior_mean_logamt"] = la.expanding().mean().shift(1)
    h["prior_sd_logamt"] = la.expanding().std().shift(1)
    h["prior_max_amt"] = h.amt.cummax().shift(1)
    h["amt_z"] = (la - h.prior_mean_logamt) / h.prior_sd_logamt.replace(0, np.nan)
    es = h.ts.astype("int64") // 10**9
    h["hours_since_prev"] = es.diff() / 3600
    s = pd.Series(1, index=h.ts)
    h["n_prev_48h"] = s.rolling("48h").count().values - 1
    h["n_prev_1h"] = s.rolling("1h").count().values - 1
    return h
