"""Likelihood ratios per signal and product code, fitted from the bank's closed cases.

Fitted within each ProductCD: match flags only exist for some products and device records
only for online ones, so a pooled ratio would mostly measure product mix.

Two reference sets, because the cleared cases are all high-score false alarms
(risk 0.82 to 0.94) and are therefore selected on the risk model:
  * LR_pop: P(signal | first txn of a confirmed-fraud case) / P(signal | random transaction)
  * LR_hi:  the same, restricted to alerts scored above 0.7, versus cleared cases
The assessor uses LR_hi for high-score alerts and LR_pop otherwise.
"""
import json

import duckdb

from kavach.config import DATA, DUCK_PATH
from kavach.features import FEATURES

WEIGHTS = DATA.parent / "kavach" / "weights.json"

SIGNALS = {
    "device_new_for_account": "id_15 = 'New'",
    "proxy": "id_23 is not null",
    "product_unseen": "prior_same_product = 0 and prior_n >= 5",
    "region_unseen": "prior_same_region = 0 and prior_n >= 5",
    "device_unseen_on_card": "prior_same_device = 0 and prior_n >= 5",
    "email_unseen_on_card": "prior_same_pemail = 0 and prior_n >= 5",
    "amount_outlier": "amt_z > 2",
    "amount_above_card_max": "amt > prior_max_amt and prior_n >= 5",
    "burst_1h": "n_prev_1h >= 2",
    "new_card": "prior_n < 3",
    "m4_m2": "M4 = 'M2'",
    "m5_true": "M5 = 'T'",
    "m6_false": "M6 = 'F'",
    "dist1_far": "dist1 > 100",
    "risk_high": "risk_score > 0.7",
    "risk_mid": "risk_score between 0.4 and 0.7",
    "risk_low": "risk_score < 0.2",
}


def _p(path) -> str:
    return str(path).replace("\\", "/")


def fit() -> dict:
    con = duckdb.connect()
    con.execute(f"attach '{_p(DUCK_PATH)}' as src (read_only)")
    con.execute(f"create view f as select * from '{_p(FEATURES)}'")
    con.execute("""create temp table cc as
        select c.outcome, coalesce(try_cast(try_cast(c.first_fraud_txn_id as double) as bigint),
                                   cast(split_part(c.txn_ids, '|', 1) as bigint)) tid
        from src.closed c""")
    con.execute("create temp table pop as select * from f using sample 200000 (reservoir, 42)")
    out = {}
    for prod in ("C", "H", "R", "S", "W"):
        out[prod] = {}
        for name, expr in SIGNALS.items():
            q = f"""select
                avg(case when outcome = 'confirmed_fraud' and ({expr}) then 1.0 when outcome = 'confirmed_fraud' then 0 end),
                avg(case when outcome = 'confirmed_fraud' and f.risk_score > 0.7 and ({expr}) then 1.0
                         when outcome = 'confirmed_fraud' and f.risk_score > 0.7 then 0 end),
                avg(case when outcome = 'cleared' and ({expr}) then 1.0 when outcome = 'cleared' then 0 end),
                count(*) filter (where outcome = 'confirmed_fraud'), count(*) filter (where outcome = 'cleared')
                from cc join f on f.TransactionID = cc.tid where f.ProductCD = '{prod}'"""
            fr, fr_hi, cl, nf, nc = con.execute(q).fetchone()
            po = con.execute(f"select avg(case when {expr} then 1.0 else 0 end) from pop where ProductCD = '{prod}'").fetchone()[0]
            fr, fr_hi, cl, po = fr or 0.0, fr_hi or 0.0, cl or 0.0, po or 0.0
            out[prod][name] = {
                "n_fraud": nf, "n_cleared": nc,
                "p_fraud": round(fr, 4), "p_population": round(po, 4), "LR_pop": round((fr + 0.01) / (po + 0.01), 3),
                "p_fraud_high_score": round(fr_hi, 4), "p_cleared": round(cl, 4), "LR_hi": round((fr_hi + 0.02) / (cl + 0.02), 3),
            }
    out["score_calibration"] = calibrate_score(con)
    out["device_ring"] = fit_device_ring(con)
    WEIGHTS.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def load() -> dict:
    return json.loads(WEIGHTS.read_text(encoding="utf-8"))


BANDS = (0.2, 0.4, 0.6, 0.8)


def band_of(score: float) -> int:
    return sum(score >= b for b in BANDS)


def calibrate_score(con) -> dict:
    """P(fraud | product, identity record present, score band) over July to October.

    The closed cases label essentially all fraud in those months (confirmed-fraud transactions are
    3.4% of all transactions), so the rate of closed-fraud transactions per cell is a calibrated
    reading of what the bank's score means for that kind of transaction. Laplace-smoothed; cells
    with fewer than 20 transactions borrow the product-wide rate for the band.
    """
    rows = con.execute("""
        with lab as (select distinct TransactionID from src.closed_txn where outcome = 'confirmed_fraud')
        select t.ProductCD, t.device_key is not null has_id,
               least(4, (t.risk_score >= 0.2)::int + (t.risk_score >= 0.4)::int + (t.risk_score >= 0.6)::int + (t.risk_score >= 0.8)::int) band,
               count(*) n, count(l.TransactionID) k
        from src.txn t left join lab l using (TransactionID) where t.ts < '2016-10-25' group by all""").fetchall()
    cal: dict = {}
    prod_band: dict = {}
    for prod, has_id, band, n, k in rows:
        pb = prod_band.setdefault(f"{prod}|{band}", [0, 0])
        pb[0] += n
        pb[1] += k
    for prod, has_id, band, n, k in rows:
        if n >= 20:
            p = (k + 1) / (n + 2)
        else:
            tn, tk = prod_band[f"{prod}|{band}"]
            p = (tk + 1) / (tn + 2)
        cal[f"{prod}|{int(bool(has_id))}|{band}"] = {"n": n, "p": round(p, 4)}
    for key, (tn, tk) in prod_band.items():
        prod, band = key.split("|")
        for hid in (0, 1):
            cal.setdefault(f"{prod}|{hid}|{band}", {"n": 0, "p": round((tk + 1) / (tn + 2), 4)})
    return cal


def fit_device_ring(con) -> dict:
    """LR of 'a rare device profile (<= 25 cards ever) paid similar amounts (within 2%) on k other cards within 7 days'.

    Positives: online transactions in confirmed-fraud closed cases. Reference: all other online
    transactions before 25 Oct (overwhelmingly legitimate)."""
    con.execute("""create or replace temp table dv as select device_key, count(distinct card_id) pop from src.txn
                   where device_key is not null and replace(replace(device_key, '|', ''), ' ', '') <> '' group by 1""")
    con.execute("""create or replace temp table on_ as select t.TransactionID, t.card_id, t.ts, t.TransactionAmt amt, t.device_key
                   from src.txn t join dv using (device_key) where dv.pop <= 25""")
    con.execute("""create or replace temp table ring as select a.TransactionID, count(distinct b.card_id) k from on_ a join on_ b
                   on a.device_key = b.device_key and b.card_id <> a.card_id and b.ts between a.ts - interval 7 day and a.ts + interval 7 day
                   and abs(b.amt - a.amt) <= 0.02 * a.amt + 0.5 group by 1""")
    r = con.execute("""with lab as (select distinct TransactionID from src.closed_txn where outcome = 'confirmed_fraud')
        select (l.TransactionID is not null) fraud, count(*) n, count(*) filter (where r.k >= 2) k2, count(*) filter (where r.k >= 3) k3
        from src.txn t left join lab l using (TransactionID) left join ring r using (TransactionID)
        where t.channel = 'online' and t.ts < '2016-10-25' group by 1""").fetchall()
    d = {bool(f): (n, k2, k3) for f, n, k2, k3 in r}
    (nf, f2, f3), (nr, r2, r3) = d[True], d[False]
    return {"LR_2_other_cards": round((f2 / nf) / (r2 / nr), 2), "LR_3_other_cards": round((f3 / nf) / (r3 / nr), 2),
            "p_fraud_given_2": round(f2 / (f2 + r2), 3), "p_fraud_given_3": round(f3 / (f3 + r3), 3), "n_fraud": nf, "n_reference": nr}
