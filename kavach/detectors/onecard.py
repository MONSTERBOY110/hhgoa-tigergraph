"""Single-card detectors: the card's own sequence, device, region, identity flags, score, and the customer.

Card ids here aggregate many real accounts (customer_id is derived from the issuer field),
so baselines prefer the underlying account, holder_key = card1|addr1|account start day,
and fall back to the card only when the card is small enough to be one person's history.
In January D1 was resampled, so holder_key is unusable for flagged transactions dated 2017-01-01 or later
(docs/round2/january-data-profile.md): those fall back to the card's own prior history, and nothing claims
account continuity.
"""
import math
import sys
from datetime import timedelta

import numpy as np
import pandas as pd

from kavach.assess import Signal
from kavach.detectors.network import _how, generic_device
from kavach.weights import load as load_weights

SHRINK = 0.5            # fitted LRs are shrunk: signals overlap and alerts are a selected population
THRESHOLDS = (500.0, 1000.0, 2500.0)
AGGREGATED = 300        # a card with more history than this is a pool of accounts, not one cardholder
JANUARY = pd.Timestamp("2017-01-01")
UNTRUSTED_CLAMP = (-0.10, 0.30)   # January score: a low score never exonerates, a high score alone never convicts
DISPUTE_W = 0.9         # customer_dispute: 7 of 8 customer reports in the Round 1 review were confirmed fraud (LR about 7,
                        # shrunk by SHRINK 0.5 to about 0.9); a report can be cleared when the purchase fits the profile
SETTLED_DAYS = 7       # the settled profile excludes the account's last 7 days, where an unfolding episode lives (card
                       # testing runs a median of 7.3 days; other episodes 2 to 3 days at the 90th percentile)
DEVICE_BURST_N = 10     # one device profile with this many purchases on the card inside 48 hours is an episode core
# Profile-fit negatives. fit_profile() below measures them on the Round 2 index (5,585 closed cases, first transaction of
# each; population = 20,000 random transactions before 2017) on the account baseline of baseline_frame():
#   LR_hi  = (P(flag | fraud, score > 0.7) + 0.02) / (P(flag | cleared) + 0.02)
#   LR_pop = (P(flag | fraud) + 0.01) / (P(flag | population) + 0.01)
# Result (logs/profile_fit.log): email 1.20 / 1.06, product 1.33 / 1.06, amount within max 1.04 / 1.10, prior proxy
# 0.94 / 1.10, hour 1.03 / 1.20. Taken one at a time they do not separate the old closed records, so the fitted
# weight would be 0. The weights below are therefore STATED, from the bank's own review of the 20 Round 1 alerts:
# its cleared notes give exactly these reasons ("product and email match their history" on three alerts, "amount and
# product fit the cardholder" on two, "the cardholder's other purchases show the same proxy use", "prior late-night
# activity"), and the one wrongly blocked dispute was cleared because "this purchase fits their normal pattern".
# The conjunction email + product + amount (profile_fit) is sized to hold p below 0.70 when the only positive signals
# are a dispute (0.9) and a generic New device (round1-postmortem.md, HHG-004).
PROFILE_FIT = {
    "profile_fit": {"hi": -1.2, "pop": -1.2},        # email, product and amount all fit (group: sequence)
    "email_familiar": {"hi": -0.5, "pop": -0.5},     # group: identity_flags
    "product_familiar": {"hi": -0.6, "pop": -0.6},   # group: sequence
    "amount_within_max": {"hi": -0.4, "pop": -0.4},  # group: sequence
    "proxy_seen_before": {"hi": -0.4, "pop": -0.4},  # group: device (and the positive anonymous_proxy is not emitted)
    "hour_familiar": {"hi": -0.3, "pop": -0.3},      # group: sequence
}
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


def baseline_frame(prior: pd.DataFrame, f: dict, card_id: str) -> tuple[pd.DataFrame, str]:
    """Prior history of the account behind f: its holder_key when usable (before 2017), else the card if small.
    January (holder_key unusable): the card's own history; on a pooled card, the part of it in the flagged
    transaction's billing region (online without a region: with its purchaser email domain), at least 5 rows."""
    january = pd.Timestamp(f["ts"]) >= JANUARY
    if not january and valid_holder(f.get("holder_key")):
        hp = prior[prior.holder_key == f["holder_key"]]
        if len(hp) >= 3 or len(prior) > AGGREGATED:
            return hp, f"account {f['holder_key']}"
    if len(prior) <= AGGREGATED:
        return prior, f"card {card_id}"
    if january:
        a, pe = f.get("addr1"), f.get("P_emaildomain")
        if pd.notna(a):
            g, name = prior[prior.addr1 == a], f"card {card_id} in its region {int(a)}"
        elif isinstance(pe, str):
            g, name = prior[prior.P_emaildomain == pe], f"card {card_id} with email {pe}"
        else:
            g, name = prior.iloc[0:0], "account"
        if len(g) >= 5:
            return g, name
    return prior.iloc[0:0], "account"


def baseline(ctx) -> tuple[pd.DataFrame, str]:
    return baseline_frame(ctx.h[ctx.h.ts < ctx.t0], ctx.f, ctx.card_id)


def _hour_share(base: pd.DataFrame, ts) -> float:
    if len(base) < 10:
        return 0.0
    hrs = pd.to_datetime(base.ts).dt.hour.values
    d = np.abs(hrs - pd.Timestamp(ts).hour)
    return float((np.minimum(d, 24 - d) <= 1).mean())


def profile_flags(base: pd.DataFrame, f: dict) -> dict:
    """Does the flagged purchase fit the account's own profile? Pure function of the baseline (used at runtime and in
    the fit, so both see the same definitions)."""
    pe = f.get("P_emaildomain")
    px = f.get("id_23")
    anon = isinstance(px, str) and "ANONYMOUS" in px
    n_pe = int((base.P_emaildomain == pe).sum()) if isinstance(pe, str) and len(base) else 0
    return {
        "email_familiar": n_pe >= 3,
        "product_familiar": int((base.ProductCD == f["ProductCD"]).sum()) >= 5 if len(base) else False,
        "amount_within_max": bool(len(base) >= 5 and f["amt"] <= base.amt.max()),
        "proxy_seen_before": bool(anon and len(base) and base.id_23.fillna("").astype(str).str.contains("ANONYMOUS").any()),
        "hour_familiar": _hour_share(base, f["ts"]) >= 0.10,
        "_n_pe": n_pe,
    }


def detect(ctx, fnd) -> list[Signal]:
    f, h, t0 = ctx.f, ctx.h, ctx.t0
    T = ctx.tools
    sigs: list[Signal] = []
    tid = str(int(f["TransactionID"]))
    january = pd.Timestamp(t0) >= JANUARY
    hi = float(f["risk_score"]) > 0.7
    prod = f["ProductCD"]
    prior = h[h.ts < t0]
    base, base_name = baseline(ctx)
    settled = base[base.ts < pd.Timestamp(t0) - timedelta(days=SETTLED_DAYS)]
    aggregated = len(prior) > AGGREGATED
    online_share = (prior.channel == "online").mean() if len(prior) else 0.0
    ref_hist = ctx.refs["history"]
    how_hist = _how("card_history", card_id=ctx.card_id)
    how_win = lambda hb, ha: _how("card_window", card_id=ctx.card_id, t0=t0, hours_before=hb, hours_after=ha)

    # ---- score: read from the transaction row, calibrated on the closed months; untrusted in January ----
    r = float(f["risk_score"])
    pack = ctx.case.get("risk_score")
    try:
        pack = float(pack) if pack not in (None, "") else None
    except (TypeError, ValueError):
        pack = None
    mismatch = pack is not None and abs(pack - r) > 0.05
    if mismatch and hasattr(ctx, "dq_notes"):
        ctx.dq_notes.append({"flag": "alert_score_mismatch", "record_ids": [tid, ctx.case.get("case_id", "")],
                             "detail": f"The alert states a risk score of {pack:.2f}, but transaction {tid} carries {r:.2f}; the "
                                       "transaction row is used and the alert's figure is treated as an unverified claim"})
    said = f" (the alert text states {pack:.2f}; the transaction row value is used)" if mismatch else ""
    how_txn = _how("txn_detail", txn_id=int(tid))
    if ctx.trigger == "customer_report":
        w = 0.25 if r > 0.7 else 0.2 if r >= 0.4 else -0.3 if r < 0.2 else 0.0
        if january:
            w = min(max(w, UNTRUSTED_CLAMP[0]), UNTRUSTED_CLAMP[1])
        if w:
            sigs.append(Signal("risk_score", "score", w, f"Model risk score {r:.2f} on the disputed transaction{said}"
                               + (" (January score, weight clamped: the bank's model was not retrained for January)" if january else ""),
                               [tid], ref_hist, how=how_txn))
    else:
        from kavach.weights import band_of
        has_id = ctx.device is not None or pd.notna(f.get("id_15"))
        cal = _w()["score_calibration"][f"{prod}|{int(has_id)}|{band_of(r)}"]
        pc = min(0.95, max(0.03, cal["p"]))
        llr = round(SHRINK * math.log(pc / (1 - pc)), 2)
        if january:
            sigs.append(Signal("risk_score_untrusted", "score", round(min(max(llr, UNTRUSTED_CLAMP[0]), UNTRUSTED_CLAMP[1]), 2),
                               f"Model risk score {r:.2f}{said}. The bank's model was not retrained for January (two transactions "
                               "above 0.7 in the whole month, against 2.4 to 2.7 percent a month before), so the score is read as "
                               f"near-uninformative: its weight is clamped to [{UNTRUSTED_CLAMP[0]:+.2f}, {UNTRUSTED_CLAMP[1]:+.2f}] "
                               "(a low score is not evidence of innocence, a high score alone cannot carry a verdict)",
                               [tid], "document:weights.json#score_calibration", "document", how=how_txn))
        else:
            sigs.append(Signal("risk_score_calibrated", "score", llr,
                               f"Model risk score {r:.2f}{said}. For product {prod} transactions {'with' if has_id else 'without'} an "
                               f"identity record in this score band, {pc:.0%} were confirmed fraud in July to October ({cal['n']:,} "
                               "transactions), so the score is read at that calibrated rate, not at face value", [tid],
                               "document:weights.json#score_calibration", "document", how=how_txn))

    # ---- customer ----
    if ctx.trigger == "customer_report":
        sigs.append(Signal("customer_dispute", "customer", DISPUTE_W, f"Customer {ctx.customer_id} disputes transaction {tid} "
                           f"(${f['amt']:,.2f}); a dispute is strong but not conclusive evidence (in the Round 1 review 7 of 8 "
                           "customer reports were confirmed fraud, and one was cleared because the purchase fitted the cardholder)",
                           [tid, ctx.customer_id], "case_pack:trigger_text", "customer", how=how_txn))

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
                                   _ids(band), T.ref("card_window", card_id=ctx.card_id, hours=3), how=how_win(3, 3)))
                fnd.episode["sub_threshold_burst"] = list(band.TransactionID)
                fnd.vote("undocumented", 2.6)
                fnd.undocumented_kind = "sub_threshold"
                fnd.notes["threshold"] = thr
                break

    # ---- sequence: card testing (R5: three small authorizations within an hour, then a larger purchase) ----
    small = online_win[(online_win.amt < 10) & (online_win.ts <= t0)]
    if len(small) >= 3:
        s = small.sort_values("ts")
        if (s.ts.max() - s.ts.min()) <= timedelta(hours=1):
            later = online_win[(online_win.ts > s.ts.max()) & (online_win.amt > max(20, 5 * s.amt.max()))]
            if len(later):
                ids = _ids(s) + _ids(later)
                sigs.append(Signal("card_testing", "sequence", 2.4, f"{len(s)} online authorizations under $10 within an hour, "
                                   f"then a ${later.amt.iloc[0]:,.2f} purchase", ids, T.ref("card_window", card_id=ctx.card_id, hours=3),
                                   how=how_win(3, 3)))
                fnd.card_testing = True
                fnd.cleared_over_100 = bool((later.amt > 100).any())
                fnd.episode["card_testing"] = list(s.TransactionID) + list(later.TransactionID)
                fnd.vote("card_testing", 2.4)

    # ---- sequence: testing ladder over days and per-device bursts (the bank's card_testing episodes run a median of
    #      7.3 days, 17 transactions, 2.5 small authorizations; round1-postmortem.md HHG-008 and HHG-011) ----
    end = max(pd.Timestamp(ctx.opened), pd.Timestamp(t0))
    w30 = h[(h.ts >= t0 - timedelta(days=30)) & (h.ts <= end) & (h.channel == "online")]
    core = None
    for dev, g in w30[w30.device_key.notna()].groupby("device_key"):
        if generic_device(dev, 0) or len(g) < DEVICE_BURST_N:
            continue
        ts = g.ts.sort_values().values
        k = int(max((np.searchsorted(ts, x + np.timedelta64(48, "h"), side="right") - i) for i, x in enumerate(ts)))
        if k >= DEVICE_BURST_N and (core is None or k > core[1]):
            core = (dev, k, g)
    if core is not None:
        dev, k, g = core
        sigs.append(Signal("device_burst", "sequence", 1.0, f"One device profile ({dev}) made {k} online purchases on this card "
                           f"within 48 hours ({len(g)} in the 30 days before the alert), the core of a scripted run",
                           _ids(g.head(10)), T.ref("card_window", card_id=ctx.card_id, days=30), how=how_hist))
        fnd.episode["testing_ladder"] = list(g.TransactionID)
        if ((g.amt < 10) & (g.ts < g.loc[g.amt.idxmax(), "ts"])).any():
            fnd.notes["testing_ladder"] = list(g.TransactionID)
            fnd.vote("card_testing", 1.0)
    elif not aggregated and _card_had_fraud(ctx):
        w7 = w30[w30.ts >= t0 - timedelta(days=7)].sort_values("ts")
        sm = w7[w7.amt < 10]
        if len(w7) >= 5 and len(sm):
            bigger = w7[(w7.ts > sm.ts.min()) & (w7.amt > max(20, 5 * sm.amt.max()))]
            if len(bigger):
                run = w7[w7.ts >= sm.ts.min()]
                sigs.append(Signal("testing_ladder", "sequence", 0.8,
                                   f"{len(sm)} online authorization(s) under $10 followed by {len(bigger)} larger online purchase(s) "
                                   f"within 7 days ({len(run)} transactions) on a card with earlier confirmed fraud: a testing ladder "
                                   "spread over days", _ids(run.head(12)), T.ref("card_window", card_id=ctx.card_id, days=7),
                                   how=how_hist))
                fnd.notes["testing_ladder"] = list(run.TransactionID)
                fnd.episode["testing_ladder"] = list(run.TransactionID)
                fnd.cleared_over_100 = fnd.cleared_over_100 or bool((bigger.amt > 100).any())
                fnd.vote("card_testing", 0.8)

    # ---- sequence: near-identical online charges in a short burst ----
    if f["channel"] == "online" and "sub_threshold_burst" not in fnd.episode:
        same = win[(win.channel == "online") & (win.ProductCD == prod) & ((win.amt - f["amt"]).abs() <= 0.01 * f["amt"] + 0.2)]
        same = same[(same.ts - t0).abs() <= timedelta(minutes=75)]
        if len(same) >= 3:
            earlier = _prior_same_amount_bursts(base if len(base) else prior, f)
            if earlier >= 2 and not aggregated:
                sigs.append(Signal("repeat_amount_habit", "sequence", -0.9,
                                   f"Repeated same-amount {prod} purchases in quick succession are this cardholder's habit: "
                                   f"{earlier} earlier bursts like this one on {base_name}", _ids(same), ref_hist, how=how_hist))
            else:
                sigs.append(Signal("duplicate_amount_burst", "sequence", 1.2,
                                   f"{len(same)} online {prod} charges of about ${f['amt']:,.2f} within "
                                   f"{(same.ts.max() - same.ts.min()).total_seconds() / 60:.0f} minutes",
                                   _ids(same), T.ref("card_window", card_id=ctx.card_id, hours=3), how=how_win(3, 3)))
                fnd.episode["duplicate_amount_burst"] = list(same.TransactionID)

    # ---- sequence: a sibling transaction on the same account scored very high shortly before (not in January:
    #      the account key and the score are both unusable there) ----
    if valid_holder(f.get("holder_key")) and not january:
        sib = ctx.window(24, 0)
        sib = sib[(sib.holder_key == f["holder_key"]) & (sib.TransactionID != f["TransactionID"]) & (sib.risk_score >= 0.85)]
        if len(sib):
            s0 = sib.iloc[-1]
            sigs.append(Signal("sibling_high_score", "sequence", 0.9,
                               f"Another transaction on the same account ({int(s0.TransactionID)}, ${s0.amt:,.2f}) scored {s0.risk_score:.2f} "
                               f"{(t0 - s0.ts).total_seconds() / 60:.0f} minutes earlier", _ids(sib) + [tid], ref_hist, how=how_hist))
            fnd.episode["sibling_high_score"] = list(sib.TransactionID) + [f["TransactionID"]]

    # ---- sequence: off-profile use ----
    unseen = f.get("prior_same_product", 1) == 0 and f["prior_n"] >= 5
    if f["channel"] == "online" and len(prior) >= 20 and online_share < 0.10:
        sigs.append(Signal("online_on_in_person_card", "sequence", 0.6 if unseen else 0.3,
                           f"Online purchase on a card whose history is {1 - online_share:.0%} in person ({len(prior)} earlier transactions)",
                           [tid], ref_hist, how=how_hist))
    if unseen:
        w = fitted("product_unseen", hi, prod)
        if w > 0.1:
            sigs.append(Signal("product_unseen", "sequence", w, f"Product code {prod} never used on this card before", [tid], ref_hist,
                               how=how_hist))
    if len(settled) >= 5:
        bmax = settled.amt.max()
        if f["amt"] > 1.5 * bmax:
            w = fitted("amount_above_card_max", hi, prod)
            if abs(w) > 0.1:
                sigs.append(Signal("amount_above_account_max", "sequence", w,
                                   f"${f['amt']:,.2f} is {f['amt'] / bmax:.1f} times the largest earlier purchase on {base_name}", [tid], ref_hist,
                                   how=how_hist))

    # ---- legitimate habit on the underlying account ----
    if len(settled):
        m = settled[(settled.ProductCD == prod) & ((settled.amt - f["amt"]).abs() <= 0.01 * f["amt"] + 0.05)]
        if f["channel"] == "in_person":
            m = m[m.addr1 == f["addr1"]]
        if len(m) and not fnd.episode:
            x = m.iloc[-1]
            sigs.append(Signal("amount_seen_before", "sequence", -0.8,
                               f"The same amount (${x.amt:,.2f}, product {x.ProductCD}) was charged on {base_name} before, on "
                               f"{pd.Timestamp(x.ts):%Y-%m-%d}", [str(int(x.TransactionID)), tid], ref_hist, how=how_hist))
    rec = _recurring(prior, f, aggregated)
    if rec is not None:
        sigs.append(Signal("recurring_charge", "sequence", -1.6, rec[0], rec[1], T.ref("recurring_check", card_id=ctx.card_id, amt=round(f["amt"], 2)),
                           how=_how("recurring_check", card_id=ctx.card_id, amt=float(f["amt"]), product_cd=prod)))
        fnd.recurring = True
    bad = ctx.holder_hist
    holder_bad = bad is not None and len(bad) and (bad[(bad.outcome == "confirmed_fraud")].holder_key == f.get("holder_key")).any()
    if base_name.startswith("account") and len(base) >= 3 and not holder_bad and not january:
        sigs.append(Signal("account_continuity", "history", -0.9,
                           f"The flagged transaction continues {base_name}, which has {len(base)} earlier transactions and no "
                           "confirmed-fraud case", _ids(base.tail(3)) + [tid], ctx.refs["holder"],
                           how=how_hist))

    # ---- profile fit: the purchase matches the account's own history (round1-postmortem.md section 6, items 2 and 3) ----
    pf = profile_flags(settled, f) if len(settled) >= 5 else {}
    base_for_fit = settled
    tier = "hi" if hi else "pop"
    fits_all = bool(pf.get("email_familiar") and pf.get("product_familiar") and pf.get("amount_within_max"))
    if fits_all:
        sigs.append(Signal("profile_fit", "sequence", PROFILE_FIT["profile_fit"][tier],
                           f"Profile fit: product {prod}, purchaser email {f.get('P_emaildomain')} and the amount ${f['amt']:,.2f} all "
                           f"match {base_name} before the last {SETTLED_DAYS} days ({int((base_for_fit.ProductCD == prod).sum())} earlier "
                           f"{prod} purchases, {pf['_n_pe']} with this email, largest earlier purchase ${base_for_fit.amt.max():,.2f})",
                           [tid], ref_hist, how=how_hist))
    elif pf.get("email_familiar"):
        sigs.append(Signal("email_familiar", "identity_flags", PROFILE_FIT["email_familiar"][tier],
                           f"Purchaser email domain {f.get('P_emaildomain')} is this account's own: {pf['_n_pe']} earlier transactions "
                           f"on {base_name} used it", [tid], ref_hist, how=how_hist))
    seq_fit = []
    if pf.get("product_familiar") and not fits_all:
        seq_fit.append(("product_familiar", f"product {prod} is this account's usual product ({int((base_for_fit.ProductCD == prod).sum())} "
                                            f"earlier purchases on {base_name})"))
    if pf.get("amount_within_max") and not fits_all:
        seq_fit.append(("amount_within_max", f"${f['amt']:,.2f} is within the account's earlier range (largest "
                                             f"${base_for_fit.amt.max():,.2f} over {len(base_for_fit)} purchases)"))
    if pf.get("hour_familiar"):
        seq_fit.append(("hour_familiar", f"the purchase hour ({pd.Timestamp(f['ts']):%H}:00) is one the account already uses "
                                         f"({_hour_share(base_for_fit, f['ts']):.0%} of its earlier purchases within an hour of it)"))
    for name, claim in seq_fit:
        w = PROFILE_FIT[name][tier]
        if w < 0:
            sigs.append(Signal(name, "sequence", w, f"Profile fit: {claim}", [tid], ref_hist, how=how_hist))
    proxy_known = bool(pf.get("proxy_seen_before"))
    if proxy_known:
        sigs.append(Signal("proxy_seen_before", "device", PROFILE_FIT["proxy_seen_before"][tier],
                           f"The anonymous proxy on this purchase is not new: purchases on {base_name} more than {SETTLED_DAYS} days "
                           "earlier came through one too",
                           [tid], ref_hist, how=how_hist))

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
                               [tid], ref_hist, how=how_hist))
        n_dev = int((prior[prior.ts < pd.Timestamp(t0) - timedelta(days=SETTLED_DAYS)].device_key == ctx.device).sum())
        if n_dev >= 1 and ctx.device_pop <= 300 and not generic_device(ctx.device, ctx.device_pop):
            fnd.notes["device_known"] = n_dev
        px = f.get("id_23")
        if isinstance(px, str) and "ANONYMOUS" in px and not proxy_known:
            w = max(fitted("proxy", hi, prod), 0.2)
            sigs.append(Signal("anonymous_proxy", "device", w, "Connection through an anonymous proxy (id_23), not seen before on "
                               "this account", [tid], ref_hist, how=how_txn))
        if ctx.device_pop > 300:
            fnd.notes["generic_device"] = ctx.device_pop

    # ---- region (card present) ----
    if f["channel"] == "in_person" and pd.notna(f.get("addr1")):
        n_here = int(f.get("prior_same_region") or 0)
        ip_prior = prior[prior.channel == "in_person"]
        share_here = n_here / max(len(ip_prior), 1)
        # a pooled card's region counts only by share (January has no account key to narrow the pool)
        if n_here >= 5 and (not aggregated or (january and share_here >= 0.10)):
            sigs.append(Signal("region_familiar", "region", -1.0, f"Billing region {int(f['addr1'])} is familiar: {n_here} earlier "
                               f"transactions on this card there ({share_here:.0%} of its in-person history)", [tid], ref_hist,
                               how=how_hist))
        elif len(prior) >= 20 and n_here == 0:
            home = prior.addr1.mode().iloc[0] if prior.addr1.notna().any() else None
            near = ctx.window(24, 24)
            home_active = near[(near.addr1 == home) & (near.channel == "in_person")] if home is not None else near.iloc[0:0]
            if len(home_active):
                sigs.append(Signal("out_of_region_clone", "region", 1.2, f"Card-present purchase in region {int(f['addr1'])}, where the card has "
                                   f"no history, while in-person activity continues in home region {int(home)} within 24 hours",
                                   [tid] + _ids(home_active.head(3)), ctx.refs.get("region", ref_hist), how=how_win(24, 24)))
                fnd.vote("out_of_region_use", 1.2)
            else:
                trip = ctx.window(0, 96)
                trip = trip[(trip.addr1 == f["addr1"]) & (trip.channel == "in_person")]
                if trip.ts.dt.date.nunique() >= 2:
                    sigs.append(Signal("trip_pattern", "region", -1.0, f"Several days of purchases in region {int(f['addr1'])} with home "
                                       "activity paused: a trip, not a clone", _ids(trip.head(4)), ctx.refs.get("region", ref_hist),
                                       how=how_win(0, 96)))

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
                               "in the closed cases)", [tid], ref_hist, how=how_txn))
    for col in ("C1", "C2") if f["channel"] == "online" else ():
        v = f.get(col)
        if pd.notna(v) and v >= 20:
            med = base[col].median() if len(base) and base[col].notna().any() else (prior[col].median() if len(prior) else 0)
            if v >= 4 * max(med or 0, 1):
                sigs.append(Signal("count_features_high", "identity_flags", 0.7,
                                   f"Vesta count {col} = {int(v)} on this transaction against a median of {med or 0:.0f} on {base_name} "
                                   "(unnamed count features; on product C, C2 of 30 or more was about five times as often fraud in the closed months)",
                                   [tid], ref_hist, how=how_hist))
                break
    return sigs


def _card_had_fraud(ctx) -> bool:
    ch = getattr(ctx, "closed_hits", None)
    if ch is None or not len(ch):
        return False
    ch = ch[(ch.card_id == ctx.card_id) & (ch.outcome == "confirmed_fraud")]
    return bool(len(ch[pd.to_datetime(ch.opened_at) < pd.Timestamp(ctx.opened)]))


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


def _recurring(prior: pd.DataFrame, f, aggregated: bool = False):
    """Same account, product and amount (within 2%) at a weekly or monthly cadence. The account is the holder_key
    before 2017; in January (key unusable) it is the card itself when the card is small enough to be one person."""
    hk = f.get("holder_key")
    if pd.Timestamp(f["ts"]) < JANUARY and valid_holder(hk):
        acct, who = prior[prior.holder_key == hk], f"holder {hk}"
    elif not aggregated:
        acct, who = prior, f"card {f.get('card_id', '')}".strip()
    else:
        return None
    p = acct[(acct.ProductCD == f["ProductCD"]) & ((acct.amt - f["amt"]).abs() <= 0.02 * f["amt"])]
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
            f"({who}) at {cadence} intervals", [str(int(x)) for x in p.TransactionID.tail(4)] + [str(int(f["TransactionID"]))])


# ---------------------------------------------------------------- fitting the profile-fit weights
def fit_profile(duck_path=None, n_pop: int = 20000, seed: int = 7) -> dict:
    """Likelihood ratios of the profile-fit flags on the closed cases (see PROFILE_FIT)."""
    import duckdb
    from kavach.config import DUCK_PATH
    con = duckdb.connect(str(duck_path or DUCK_PATH), read_only=True)
    t = con.execute("""select TransactionID, card_id, ts, TransactionAmt amt, ProductCD, channel, P_emaildomain, id_23, holder_key,
                       risk_score from txn where ts < '2017-01-01' order by card_id, ts, TransactionID""").df()
    cc = con.execute("""select outcome, coalesce(try_cast(try_cast(first_fraud_txn_id as double) as bigint),
                        cast(split_part(txn_ids, '|', 1) as bigint)) tid from closed where txn_ids is not null and txn_ids <> ''
                        or first_fraud_txn_id is not null""").df()
    con.close()
    by_card = {k: g.reset_index(drop=True) for k, g in t.groupby("card_id", sort=False)}
    pos = t.set_index("TransactionID")
    pop = t.sample(n_pop, random_state=seed)
    targets = [("fraud" if o == "confirmed_fraud" else "cleared", int(x)) for o, x in zip(cc.outcome, cc.tid) if pd.notna(x)]
    targets += [("pop", int(x)) for x in pop.TransactionID]
    names = [k for k in PROFILE_FIT if k != "profile_fit"]
    rows = []
    for grp, x in targets:
        if x not in pos.index:
            continue
        f = pos.loc[x]
        if isinstance(f, pd.DataFrame):
            f = f.iloc[0]
        f = f.to_dict()
        f["TransactionID"] = x
        h = by_card[f["card_id"]]
        prior = h[h.ts < f["ts"]]
        base, _ = baseline_frame(prior, f, f["card_id"])
        base = base[base.ts < pd.Timestamp(f["ts"]) - timedelta(days=SETTLED_DAYS)]
        fl = profile_flags(base, f) if len(base) >= 5 else {k: False for k in names}
        rows.append({"grp": grp, "hi": f["risk_score"] > 0.7, **{k: bool(fl[k]) for k in names}})
    d = pd.DataFrame(rows)
    out = {}
    fr, fh, cl, po = d[d.grp == "fraud"], d[(d.grp == "fraud") & d.hi], d[d.grp == "cleared"], d[d.grp == "pop"]
    for k in names:
        lr_hi = (fh[k].mean() + 0.02) / (cl[k].mean() + 0.02)
        lr_pop = (fr[k].mean() + 0.01) / (po[k].mean() + 0.01)
        cap = lambda lr: round(min(0.0, max(-1.2, SHRINK * math.log(lr))), 2)
        out[k] = {"hi": cap(lr_hi), "pop": cap(lr_pop), "LR_hi": round(lr_hi, 3), "LR_pop": round(lr_pop, 3),
                  "p_fraud": round(fr[k].mean(), 4), "p_fraud_hi": round(fh[k].mean(), 4), "p_cleared": round(cl[k].mean(), 4),
                  "p_population": round(po[k].mean(), 4)}
    out["_n"] = {"fraud": len(fr), "fraud_hi": len(fh), "cleared": len(cl), "population": len(po)}
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(fit_profile(sys.argv[1] if len(sys.argv) > 1 else None), indent=1))
