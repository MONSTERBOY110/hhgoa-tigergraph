"""History detectors: the bank's closed cases as memory (holder-level repeat compromise, device precedents)."""
from datetime import timedelta

import pandas as pd

from kavach.assess import Signal


def detect(ctx, fnd) -> list[Signal]:
    sigs: list[Signal] = []
    f = ctx.f
    tid = str(int(f["TransactionID"]))
    hh = ctx.holder_hist
    ref = ctx.refs["holder"]

    # ---- repeat compromise of the same underlying account (holder_key = card1|addr1|account start day) ----
    if hh is not None and len(hh):
        fraud = hh[hh.outcome == "confirmed_fraud"]
        cleared = hh[hh.outcome == "cleared"]
        hk = f.get("holder_key")
        own = fraud[fraud.holder_key == hk]
        if len(own):
            cases = sorted(own.case_id.unique())
            sigs.append(Signal("holder_prior_fraud", "history", 2.5,
                               f"The account behind this transaction (holder {hk}) already appears in {len(cases)} confirmed-fraud closed "
                               f"case(s) ({', '.join(cases[:4])}); in the closed cases, 1,059 alerts on accounts with earlier confirmed fraud "
                               "were fraud and none were cleared", cases[:6] + [tid], ref))
            fnd.similar_cases += cases[:4]
            fnd.linked_fraud = fnd.linked_fraud or False
            pats = own.pattern.value_counts()
            fnd.notes["holder_patterns"] = pats.to_dict()
            win = ctx.h[(ctx.h.ts >= ctx.t0 - timedelta(days=7)) & (ctx.h.ts <= ctx.opened)]
            ep = win[win.holder_key == hk]
            fnd.episode["holder_prior_fraud"] = list(ep.TransactionID)
        elif len(cleared[cleared.holder_key == hk]):
            c = sorted(cleared[cleared.holder_key == hk].case_id.unique())
            sigs.append(Signal("holder_prior_cleared", "history", -0.8, f"The account behind this transaction (holder {hk}) had an earlier "
                               f"alert cleared as a false alarm ({', '.join(c[:3])})", c[:3] + [tid], ref))

    # ---- closed cases involving the same rare device profile ----
    ch = ctx.closed_hits
    if ch is not None and len(ch):
        dev = ch[ch.via.str.contains("device") & (ch.outcome == "confirmed_fraud")]
        if len(dev) and ctx.device:
            cases = list(dev.case_id)
            undoc = dev[dev.pattern == "undocumented"]
            sigs.append(Signal("device_in_confirmed_cases", "history", 2.2 if len(undoc) else 1.4,
                               f"Device profile {ctx.device} appears in {len(cases)} confirmed-fraud closed case(s) "
                               f"({', '.join(cases[:4])})" + (", labelled undocumented by the analysts" if len(undoc) else ""),
                               cases[:6], ctx.refs["closed"]))
            fnd.similar_cases += cases[:4]
            fnd.linked_fraud = True
            if len(undoc):
                fnd.coordinated_undocumented = fnd.coordinated_undocumented or fnd.undocumented_kind == "device_ring"
    return sigs
