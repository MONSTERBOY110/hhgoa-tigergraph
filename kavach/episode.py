"""Episode builder: affected transactions, first suspicious transaction, connected entities, exposure, pattern."""
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

# which detector's episode wins when several fired, most specific first
PRIORITY = ("sub_threshold_burst", "card_testing", "device_ring", "holder_prior_fraud", "duplicate_amount_burst", "sibling_high_score")


@dataclass
class Episode:
    txn_ids: list = field(default_factory=list)       # strings, ordered by time
    first: str = ""
    exposure: float = 0.0
    connected_cards: list = field(default_factory=list)
    connected_devices: list = field(default_factory=list)
    source: str = ""
    rows: pd.DataFrame = None


def build(ctx, fnd, verdict: str) -> Episode:
    if verdict == "legitimate":
        return Episode()
    h = ctx.h
    ids, source = None, "flagged"
    for k in PRIORITY:
        if fnd.episode.get(k):
            ids, source = set(int(x) for x in fnd.episode[k]), k
            break
    if ids is None:
        ids = {int(ctx.f["TransactionID"])}
        if ctx.device and ctx.device_pop <= 100:
            near = h[(h.device_key == ctx.device) & ((h.ts - ctx.t0).abs() <= timedelta(hours=48)) & (h.ts <= ctx.opened)]
            ids |= set(int(x) for x in near.TransactionID)
    ids.add(int(ctx.f["TransactionID"]))
    rows = h[h.TransactionID.isin(ids)].sort_values(["ts", "TransactionID"])
    ep = Episode(txn_ids=[str(int(x)) for x in rows.TransactionID], source=source, rows=rows)
    ep.first = ep.txn_ids[0]
    ep.exposure = round(float(rows.amt.abs().sum()), 2)
    ep.connected_cards = sorted(fnd.connected_cards)
    ep.connected_devices = list(dict.fromkeys(fnd.connected_devices))
    return ep


def pattern_for(ctx, fnd, verdict: str) -> str:
    """Map the evidence to the README's pattern enum (rules mirror how the closed cases are labelled)."""
    if verdict == "legitimate":
        return "none"
    if fnd.coordinated_undocumented and fnd.pattern_votes.get("undocumented"):
        return "undocumented"
    if fnd.card_testing:
        return "card_testing"
    f = ctx.f
    if f["channel"] == "in_person":
        if fnd.pattern_votes.get("out_of_region_use"):
            return "out_of_region_use"
        prior = ctx.h[ctx.h.ts < ctx.t0]
        share = (prior.addr1 == f["addr1"]).mean() if len(prior) and pd.notna(f.get("addr1")) else 0.0
        # card-present fraud in a region that is a small share of the card's history reads as a clone;
        # in the card's main region, with the account itself compromised, as account takeover
        return "out_of_region_use" if share < 0.25 else "account_takeover"
    if f.get("id_15") == "New":
        return "card_not_present_new_device"
    return "card_not_present_fraud"


def activity_dates(ep: Episode) -> list[str]:
    if ep.rows is None or ep.rows.empty:
        return []
    return [f"{ep.rows.ts.min():%Y-%m-%d}", f"{ep.rows.ts.max():%Y-%m-%d}"]
