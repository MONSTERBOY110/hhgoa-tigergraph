"""History detectors: the bank's closed cases as memory (account-level repeat compromise, device precedents).

holder_key (card1|addr1|account start day) identifies the underlying account in July to December. In January
D1 was resampled, so 92.5 percent of January rows land on a holder_key never seen before
(docs/round2/january-data-profile.md). For flagged transactions dated 2017-01-01 or later the account-level
history therefore falls back to the card itself (card_prior_fraud).
"""
from datetime import timedelta

import pandas as pd

from kavach.assess import Signal
from kavach.detectors.network import _how, generic_device, promote_rings

JANUARY = pd.Timestamp("2017-01-01")
AGGREGATED = 300         # same definition as onecard.AGGREGATED: more history than this is a pool of accounts
# Card-level precedent, fitted on the 5,585 closed cases (first transaction; earlier confirmed-fraud case on the same
# card opened at least a day before): small cards fraud 1,146/2,296 vs cleared 84/657 -> LR 3.9 -> 0.5*ln = 0.68;
# pooled cards (> 300 txns) fraud 2,083/2,379 vs cleared 171/253 -> LR 1.30 -> 0.13.
CARD_PRIOR_W = {"small": 0.68, "pooled": 0.13}


def holder_broken(ctx) -> bool:
    return pd.Timestamp(ctx.t0) >= JANUARY


def detect(ctx, fnd) -> list[Signal]:
    sigs: list[Signal] = []
    f = ctx.f
    tid = str(int(f["TransactionID"]))
    hh = ctx.holder_hist
    ref = ctx.refs["holder"]
    hk = f.get("holder_key")

    # ---- repeat compromise of the same underlying account (holder_key) ----
    if hh is not None and len(hh) and not holder_broken(ctx):
        fraud = hh[hh.outcome == "confirmed_fraud"]
        cleared = hh[hh.outcome == "cleared"]
        own = fraud[fraud.holder_key == hk]
        how = _how("holder_fraud_history", holder_keys=[hk], before=ctx.opened)
        if len(own):
            cases = sorted(own.case_id.unique())
            sigs.append(Signal("holder_prior_fraud", "history", 2.5,
                               f"The account behind this transaction (holder {hk}) already appears in {len(cases)} confirmed-fraud closed "
                               f"case(s) ({', '.join(cases[:4])}); in the closed cases, 1,059 alerts on accounts with earlier confirmed fraud "
                               "were fraud and none were cleared", cases[:6], ref, how=how))
            fnd.similar_cases += cases[:4]
            fnd.notes["holder_patterns"] = own.pattern.value_counts().to_dict()
            fnd.notes["prior_fraud_account"] = True
            win = ctx.h[(ctx.h.ts >= ctx.t0 - timedelta(days=7)) & (ctx.h.ts <= ctx.opened)]
            fnd.episode["holder_prior_fraud"] = list(win[win.holder_key == hk].TransactionID)
            # other accounts on the card that themselves carry confirmed fraud (several compromised accounts at once)
            bad = set(fraud.holder_key) - {hk}
            if bad:
                fnd.notes["fraud_holders"] = sorted(bad | {hk})
        elif len(cleared[cleared.holder_key == hk]):
            c = sorted(cleared[cleared.holder_key == hk].case_id.unique())
            sigs.append(Signal("holder_prior_cleared", "history", -0.8, f"The account behind this transaction (holder {hk}) had an earlier "
                               f"alert cleared as a false alarm ({', '.join(c[:3])})", c[:3], ref, how=how))

    ch = ctx.closed_hits
    if ch is not None and len(ch):
        ch = ch[pd.to_datetime(ch.opened_at) < pd.Timestamp(ctx.opened)]

    # ---- January: the account key is broken, so precedent is read at card level ----
    if holder_broken(ctx) and ch is not None and len(ch):
        own = ch[ch.via.str.contains("card") & (ch.card_id == ctx.card_id) & (ch.outcome == "confirmed_fraud")]
        if len(own):
            prior_n = int((ctx.h.ts < ctx.t0).sum())
            kind = "pooled" if prior_n > AGGREGATED else "small"
            cases = sorted(own.case_id.unique())
            sigs.append(Signal("card_prior_fraud", "history", CARD_PRIOR_W[kind],
                               f"Card {ctx.card_id} already has {len(cases)} confirmed-fraud closed case(s) ({', '.join(cases[:4])}). "
                               "The account key is not usable in January, so the precedent is read at card level, where it is weaker: "
                               + ("on cards of this size an earlier confirmed case made a new alert about 3.9 times as likely to be fraud"
                                  if kind == "small" else
                                  "this card pools many accounts, and an earlier confirmed case barely separates fraud from cleared alerts"),
                               cases[:6], ctx.refs["closed"], how=_how("closed_cases_for", card_ids=[ctx.card_id])))
            fnd.similar_cases += cases[:4]
            if kind == "small":
                fnd.notes["prior_fraud_account"] = True

    # ---- closed cases involving the same rare device profile (never a placeholder or family-level profile) ----
    if ch is not None and len(ch) and ctx.device and not generic_device(ctx.device, ctx.device_pop):
        dev = ch[ch.via.str.contains("device") & (ch.outcome == "confirmed_fraud")]
        if len(dev):
            cases = list(dev.case_id)
            undoc = dev[dev.pattern == "undocumented"]
            sigs.append(Signal("device_in_confirmed_cases", "history", 2.2 if len(undoc) else 1.4,
                               f"Device profile {ctx.device} appears in {len(cases)} confirmed-fraud closed case(s) "
                               f"({', '.join(cases[:4])})" + (", labelled undocumented by the analysts" if len(undoc) else ""),
                               cases[:6], ctx.refs["closed"], how=_how("closed_cases_for", device_keys=[ctx.device])))
            fnd.similar_cases += cases[:4]
            fnd.linked_fraud = True
            if len(undoc):
                fnd.coordinated_undocumented = fnd.coordinated_undocumented or fnd.undocumented_kind == "device_ring"
    # a ring whose shared element has closed-case precedent, or a coordinated pattern, lists its cards as connected
    promote_rings(fnd)
    return sigs
