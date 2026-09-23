"""Single-card detectors: the card's own sequence, device, region, identity flags, score, and the customer.

Card ids here aggregate many real accounts (customer_id is derived from the issuer field),
so baselines prefer the underlying account, holder_key = card1|addr1|account start day,
and fall back to the card only when the card is small enough to be one person's history.
"""
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from kavach.assess import Signal
from kavach.weights import load as load_weights

SHRINK = 0.5            # fitted LRs are shrunk: signals overlap and alerts are a selected population
THRESHOLDS = (500.0, 1000.0, 2500.0)
AGGREGATED = 300        # a card with more history than this is a pool of accounts, not one cardholder
_W = None


def _w():
    global _W
    if _W is None:
        _W = load_weights()
    return _W


def fitted(name: str, high_score: bool, product: str) -> float:
    w = _w().get(product, {}).get(name)
    if not w:
        return 0.0
    lr = w["LR_hi"] if high_score else w["LR_pop"]
    return round(SHRINK * math.log(max(lr, 1e-3)), 2)


def _ids(df) -> list[str]:
    return [str(int(x)) for x in df.TransactionID]


def valid_holder(hk) -> bool:
    return isinstance(hk, str) and "||" not in hk and not hk.endswith("|")


def baseline(ctx) -> tuple[pd.DataFrame, str]:
    """Prior history of the account behind the flagged txn: its holder_key if known, else the card if small."""
    f, prior = ctx.f, ctx.h[ctx.h.ts < ctx.t0]
    if valid_holder(f.get("holder_key")):
        hp = prior[prior.holder_key == f["holder_key"]]
        if len(hp) >= 3 or len(prior) > AGGREGATED:
            return hp, f"account {f['holder_key']}"
    if len(prior) <= AGGREGATED:
        return prior, f"card {ctx.card_id}"
    return prior.iloc[0:0], "account"


def detect(ctx, fnd) -> list[Signal]:
    f, h, t0 = ctx.f, ctx.h, ctx.t0
    T = ctx.tools
    sigs: list[Signal] = []
    tid = str(int(f["TransactionID"]))
    hi = float(f["risk_score"]) > 0.7
    prod = f["ProductCD"]
    prior = h[h.ts < t0]
    base, base_name = baseline(ctx)
    aggregated = len(prior) > AGGREGATED
    online_share = (prior.channel == "online").mean() if len(prior) else 0.0
    ref_hist = ctx.refs["history"]

    # ---- score: what the bank's score means for this kind of transaction, calibrated on the closed months ----
    r = float(f["risk_score"])
    if ctx.trigger == "customer_report":
        w = 0.25 if r > 0.7 else 0.2 if r >= 0.4 else -0.3 if r < 0.2 else 0.0
        if w:
            sigs.append(Signal("risk_score", "score", w, f"Model risk score {r:.2f} on the disputed transaction", [tid], ref_hist))
    else:
        from kavach.weights import band_of
        has_id = ctx.device is not None or pd.notna(f.get("id_15"))
        cal = _w()["score_calibration"][f"{prod}|{int(has_id)}|{band_of(r)}"]
        pc = min(0.95, max(0.03, cal["p"]))
        sigs.append(Signal("risk_score_calibrated", "score", round(SHRINK * math.log(pc / (1 - pc)), 2),
                           f"Model risk score {r:.2f}. For product {prod} transactions {'with' if has_id else 'without'} an identity record "
                           f"in this score band, {pc:.0%} were confirmed fraud in July to October ({cal['n']:,} transactions), so the "
                           "score is read at that calibrated rate, not at face value", [tid], "document:weights.json#score_calibration",
                           "document"))

    # ---- customer ----
    if ctx.trigger == "customer_report":
        sigs.append(Signal("customer_dispute", "customer", 1.5, f"Customer {ctx.customer_id} disputes transaction {tid} "
                           f"(${f['amt']:,.2f}); in the bank's closed cases every customer-reported alert was confirmed fraud",
                           [tid, ctx.customer_id], "case_pack:trigger_text", "customer"))

    win = ctx.window(3, 3)
    online_win = win[win.channel == "online"]

    # ---- sequence: sub-threshold burst (amounts parked just under a round limit) ----
    for thr in THRESHOLDS:
        band = online_win[(online_win.amt >= 0.88 * thr) & (online_win.amt < thr)]
        if len(band) >= 3 and tid in _ids(band):
            span = (band.ts.max() - band.ts.min()).total_seconds() / 60
            if span <= 60:
                po = prior[(prior.channel == "online") & (prior.ts < band.ts.min())]
                prior_max = po.amt.max() if len(po) else 0.0
                sigs.append(Signal("sub_threshold_burst", "sequence", 2.6,
                                   f"{len(band)} online purchases of ${band.amt.min():,.2f} to ${band.amt.max():,.2f} within {span:.0f} minutes, "
                                   f"each just under ${thr:,.0f}; before this burst the card's largest online purchase was ${prior_max:,.2f}",
                                   _ids(band), T.ref("card_window", card_id=ctx.card_id, hours=3)))
                fnd.episode["sub_threshold_burst"] = list(band.TransactionID)
                fnd.vote("undocumented", 2.6)
                fnd.undocumented_kind = "sub_threshold"
                fnd.notes["threshold"] = thr
                break

    # ---- sequence: card testing ----
    small = online_win[(online_win.amt < 10) & (online_win.ts <= t0)]
    if len(small) >= 3:
        s = small.sort_values("ts")
        if (s.ts.max() - s.ts.min()) <= timedelta(hours=1):
            later = online_win[(online_win.ts > s.ts.max()) & (online_win.amt > max(20, 5 * s.amt.max()))]
            if len(later):
                ids = _ids(s) + _ids(later)
                sigs.append(Signal("card_testing", "sequence", 2.4, f"{len(s)} online authorizations under $10 within an hour, "
                                   f"then a ${later.amt.iloc[0]:,.2f} purchase", ids, T.ref("card_window", card_id=ctx.card_id, hours=3)))
                fnd.card_testing = True
                fnd.cleared_over_100 = bool((later.amt > 100).any())
                fnd.episode["card_testing"] = list(s.TransactionID) + list(later.TransactionID)
                fnd.vote("card_testing", 2.4)

    # ---- sequence: near-identical online charges in a short burst ----
    if f["channel"] == "online" and "sub_threshold_burst" not in fnd.episode:
        same = win[(win.channel == "online") & (win.ProductCD == prod) & ((win.amt - f["amt"]).abs() <= 0.01 * f["amt"] + 0.2)]
        same = same[(same.ts - t0).abs() <= timedelta(minutes=75)]
        if len(same) >= 3:
            earlier = _prior_same_amount_bursts(base if len(base) else prior, f)
            if earlier >= 2 and not aggregated:
                sigs.append(Signal("repeat_amount_habit", "sequence", -0.9,
                                   f"Repeated same-amount {prod} purchases in quick succession are this cardholder's habit: "
                                   f"{earlier} earlier bursts like this one on {base_name}", _ids(same), ref_hist))
            else:
                sigs.append(Signal("duplicate_amount_burst", "sequence", 1.2,
                                   f"{len(same)} online {prod} charges of about ${f['amt']:,.2f} within "
                                   f"{(same.ts.max() - same.ts.min()).total_seconds() / 60:.0f} minutes",
                                   _ids(same), T.ref("card_window", card_id=ctx.card_id, hours=3)))
                fnd.episode["duplicate_amount_burst"] = list(same.TransactionID)

    # ---- sequence: a sibling transaction on the same account scored very high shortly before ----
    if valid_holder(f.get("holder_key")):
        sib = ctx.window(24, 0)
        sib = sib[(sib.holder_key == f["holder_key"]) & (sib.TransactionID != f["TransactionID"]) & (sib.risk_score >= 0.85)]
        if len(sib):
            s0 = sib.iloc[-1]
            sigs.append(Signal("sibling_high_score", "sequence", 0.9,
                               f"Another transaction on the same account ({int(s0.TransactionID)}, ${s0.amt:,.2f}) scored {s0.risk_score:.2f} "
                               f"{(t0 - s0.ts).total_seconds() / 60:.0f} minutes earlier", _ids(sib) + [tid], ref_hist))
            fnd.episode["sibling_high_score"] = list(sib.TransactionID) + [f["TransactionID"]]

    # ---- sequence: off-profile use ----
    unseen = f.get("prior_same_product", 1) == 0 and f["prior_n"] >= 5
    if f["channel"] == "online" and len(prior) >= 20 and online_share < 0.10:
        sigs.append(Signal("online_on_in_person_card", "sequence", 0.6 if unseen else 0.3,
                           f"Online purchase on a card whose history is {1 - online_share:.0%} in person ({len(prior)} earlier transactions)",
                           [tid], ref_hist))
    if unseen:
        w = fitted("product_unseen", hi, prod)
        if w > 0.1:
            sigs.append(Signal("product_unseen", "sequence", w, f"Product code {prod} never used on this card before", [tid], ref_hist))
    if len(base) >= 5:
        bmax = base.amt.max()
        if f["amt"] > 1.5 * bmax:
            w = fitted("amount_above_card_max", hi, prod)
            if abs(w) > 0.1:
                sigs.append(Signal("amount_above_account_max", "sequence", w,
                                   f"${f['amt']:,.2f} is {f['amt'] / bmax:.1f} times the largest earlier purchase on {base_name}", [tid], ref_hist))

    # ---- legitimate habit on the underlying account ----
    if len(base):
        m = base[(base.ProductCD == prod) & ((base.amt - f["amt"]).abs() <= 0.01 * f["amt"] + 0.05)]
        if f["channel"] == "in_person":
            m = m[m.addr1 == f["addr1"]]
        if len(m) and not fnd.episode:
            x = m.iloc[-1]
            sigs.append(Signal("amount_seen_before", "sequence", -0.8,
                               f"The same amount (${x.amt:,.2f}, product {x.ProductCD}) was charged on {base_name} before, on "
                               f"{pd.Timestamp(x.ts):%Y-%m-%d}", [str(int(x.TransactionID)), tid], ref_hist))
    rec = _recurring(prior, f)
    if rec is not None:
        sigs.append(Signal("recurring_charge", "sequence", -1.6, rec[0], rec[1], T.ref("recurring_check", card_id=ctx.card_id, amt=round(f["amt"], 2))))
        fnd.recurring = True
    bad = ctx.holder_hist
    holder_bad = bad is not None and len(bad) and (bad[(bad.outcome == "confirmed_fraud")].holder_key == f.get("holder_key")).any()
    if base_name.startswith("account") and len(base) >= 3 and not holder_bad:
        sigs.append(Signal("account_continuity", "history", -0.9,
                           f"The flagged transaction continues {base_name}, which has {len(base)} earlier transactions and no "
                           "confirmed-fraud case", _ids(base.tail(3)) + [tid], ctx.refs["holder"]))
    pe = f.get("P_emaildomain")
    if isinstance(pe, str) and not aggregated and len(prior) >= 5:
        emailed = prior[prior.P_emaildomain.notna()]
        n_pe = int((emailed.P_emaildomain == pe).sum())
        share = n_pe / max(len(emailed), 1)
        common = pe in ("gmail.com", "yahoo.com", "hotmail.com", "anonymous.com", "aol.com")
        if n_pe >= 3 and ((not common and share >= 0.2) or (share >= 0.6 and n_pe >= 5)):
            sigs.append(Signal("email_familiar", "identity_flags", -0.4, f"Purchaser email domain {pe} is this card's usual one "
                               f"({n_pe} earlier transactions, {share:.0%} of those with an email)", [tid], ref_hist))

    # ---- device ----
    if f["channel"] == "online" and ctx.device:
        on_prior = prior[prior.channel == "online"]
        if f.get("id_15") == "New":
            w = fitted("device_new_for_account", hi, prod)
            new_share = (on_prior.id_15 == "New").mean() if len(on_prior) else 0.0
            sigs.append(Signal("device_new_for_account", "device", w,
                               f"Identity record marks the device ({ctx.device}) as New for this account"
                               + (f"; {new_share:.0%} of the card's earlier online purchases were also from New devices" if len(on_prior) >= 3 else "")
                               + " (in the closed cases a New device is not more common in fraud than in normal traffic for this product)" * (w <= 0),
                               [tid], ref_hist))
        if f.get("prior_same_device", 0) >= 1 and ctx.device_pop <= 300:
            fnd.notes["device_known"] = int(f["prior_same_device"])
        px = f.get("id_23")
        if isinstance(px, str) and "ANONYMOUS" in px:
            w = max(fitted("proxy", hi, prod), 0.2)
            sigs.append(Signal("anonymous_proxy", "device", w, "Connection through an anonymous proxy (id_23)", [tid], ref_hist))
        if ctx.device_pop > 300:
            fnd.notes["generic_device"] = ctx.device_pop

    # ---- region (card present) ----
    if f["channel"] == "in_person" and pd.notna(f.get("addr1")):
        n_here = int(f.get("prior_same_region") or 0)
        if n_here >= 5 and not aggregated:
            sigs.append(Signal("region_familiar", "region", -1.0, f"Billing region {int(f['addr1'])} is familiar: {n_here} earlier "
                               "transactions on this card there", [tid], ref_hist))
        elif len(prior) >= 20 and n_here == 0:
            home = prior.addr1.mode().iloc[0] if prior.addr1.notna().any() else None
            near = ctx.window(24, 24)
            home_active = near[(near.addr1 == home) & (near.channel == "in_person")] if home is not None else near.iloc[0:0]
            if len(home_active):
                sigs.append(Signal("out_of_region_clone", "region", 1.2, f"Card-present purchase in region {int(f['addr1'])}, where the card has "
                                   f"no history, while in-person activity continues in home region {int(home)} within 24 hours",
                                   [tid] + _ids(home_active.head(3)), ctx.refs.get("region", ref_hist)))
                fnd.vote("out_of_region_use", 1.2)
            else:
                trip = ctx.window(0, 96)
                trip = trip[(trip.addr1 == f["addr1"]) & (trip.channel == "in_person")]
                if trip.ts.dt.date.nunique() >= 2:
                    sigs.append(Signal("trip_pattern", "region", -1.0, f"Several days of purchases in region {int(f['addr1'])} with home "
                                       "activity paused: a trip, not a clone", _ids(trip.head(4)), ctx.refs.get("region", ref_hist)))

    # ---- identity flags (fitted per product from the closed cases) ----
    idsig = []
    for name, cond, label in (("m5_true", f.get("M5") == "T", "match flag M5 = T"),
                              ("m6_false", f.get("M6") == "F", "match flag M6 = F"),
                              ("m4_m2", f.get("M4") == "M2", "match flag M4 = M2"),
                              ("dist1_far", pd.notna(f.get("dist1")) and f["dist1"] > 100, f"distance dist1 = {f.get('dist1')}")):
        if cond:
            idsig.append((fitted(name, hi, prod), label))
    if idsig:
        w = max(x for x, _ in idsig)
        if abs(w) >= 0.15:
            sigs.append(Signal("identity_flags", "identity_flags", w, f"Vesta match and distance features: {', '.join(l for _, l in idsig)} "
                               f"(unnamed model features, weighted by how they separated fraud from cleared alerts for product {prod} "
                               "in the closed cases)", [tid], ref_hist))
    for col in ("C1", "C2") if f["channel"] == "online" else ():
        v = f.get(col)
        if pd.notna(v) and v >= 20:
            med = base[col].median() if len(base) and base[col].notna().any() else (prior[col].median() if len(prior) else 0)
            if v >= 4 * max(med or 0, 1):
                sigs.append(Signal("count_features_high", "identity_flags", 0.7,
                                   f"Vesta count {col} = {int(v)} on this transaction against a median of {med or 0:.0f} on {base_name} "
                                   "(unnamed count features; on product C, C2 of 30 or more was about five times as often fraud in the closed months)",
                                   [tid], ref_hist))
                break
    return sigs


def _prior_same_amount_bursts(prior: pd.DataFrame, f) -> int:
    """How many times before did this account make 3+ same-product charges within 1% of each other inside 75 minutes."""
    p = prior[(prior.channel == "online") & (prior.ProductCD == f["ProductCD"])].sort_values("ts")
    if len(p) < 3:
        return 0
    n, i, ts, amt = 0, 0, p.ts.values, p.amt.values
    while i < len(p) - 2:
        j = i
        while j + 1 < len(p) and (ts[j + 1] - ts[i]) <= np.timedelta64(75, "m") and abs(amt[j + 1] - amt[i]) <= 0.01 * amt[i] + 0.2:
            j += 1
        if j - i >= 2:
            n += 1
            i = j + 1
        else:
            i += 1
    return n


def _recurring(prior: pd.DataFrame, f):
    """Same account, product and amount (within 2%) at a weekly or monthly cadence."""
    hk = f.get("holder_key")
    if not valid_holder(hk):
        return None
    p = prior[(prior.holder_key == hk) & (prior.ProductCD == f["ProductCD"]) & ((prior.amt - f["amt"]).abs() <= 0.02 * f["amt"])]
    if len(p) < 2:
        return None
    ts = list(p.ts) + [f["ts"]]
    gaps = np.diff([pd.Timestamp(t).value / 86400e9 for t in ts])[-4:]
    weekly = all(5.5 <= g <= 8.5 for g in gaps)
    monthly = all(26 <= g <= 34 for g in gaps)
    if not (weekly or monthly):
        return None
    cadence = "weekly" if weekly else "monthly"
    return (f"Recurring {cadence} charge: {len(p)} earlier {f['ProductCD']} charges of about ${f['amt']:,.2f} on the same account "
            f"(holder {hk}) at {cadence} intervals", [str(int(x)) for x in p.TransactionID.tail(4)] + [str(int(f["TransactionID"]))])
