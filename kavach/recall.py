"""Similar closed cases (case memory retrieval).

Fingerprint similarity over the closed cases: pattern hypothesis, channel, product, amount
band, episode size and device novelty, computed from each closed case's own transactions.
On the TigerGraph lane the same candidates can come from TigerVector over the analyst notes.
"""
import math

import pandas as pd

_FP = None


def fingerprints():
    """One row per closed case: outcome, pattern, first-txn product/channel, log amount, n txns, New-device share."""
    global _FP
    if _FP is None:
        from kavach import duckq as d
        _FP = d.q("""select ct.case_id, any_value(ct.outcome) outcome, any_value(ct.pattern) pattern,
                            mode(t.ProductCD) product, mode(t.channel) channel, avg(ln(t.TransactionAmt + 1)) logamt,
                            count(*) n, avg(case when t.id_15 = 'New' then 1.0 else 0 end) new_share,
                            avg(case when t.id_23 like '%ANONYMOUS%' then 1.0 else 0 end) anon_share,
                            min(c.opened_at) opened_at
                     from closed_txn ct join txn t using (TransactionID) join closed c using (case_id) group by ct.case_id""")
    return _FP


def similar(pattern: str, verdict: str, product: str, channel: str, amt: float, n: int, new_dev: bool, anon: bool,
            k: int = 3, exclude=()) -> list[tuple[str, float]]:
    fp = fingerprints()
    want_outcome = "cleared" if verdict == "legitimate" else "confirmed_fraud"
    c = fp[(fp.outcome == want_outcome) & ~fp.case_id.isin(list(exclude))]
    if verdict != "legitimate" and pattern != "none":
        c = c[c.pattern == pattern] if (c.pattern == pattern).any() else c
    score = (
        (c["product"] == product) * 1.0 + (c["channel"] == channel) * 1.0
        - (c.logamt - math.log1p(amt)).abs() * 0.8
        - (c.n.clip(upper=20) - min(n, 20)).abs() * 0.15
        - (c.new_share - float(new_dev)).abs() * 0.5 - (c.anon_share - float(anon)).abs() * 0.5
    )
    top = c.assign(s=score).sort_values(["s", "case_id"], ascending=[False, True]).head(k)
    return [(r.case_id, round(float(r.s), 3)) for r in top.itertuples()]
