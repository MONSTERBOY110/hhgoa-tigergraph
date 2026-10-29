"""Typology detectors (Round 2, challenge 1): common origin, dormant wake, micro-charges, recipient fan-in.

Each detector is split in two so the runtime and the backtest run the same code:
  *_fetch(T, anchor, params)  -> facts       only ToolRegistry calls (T.call), so both lanes and the trace see them
  *_rule(facts, anchor, params) -> hit|None  pure pandas over the fetched frames
The backtest (kavach/backtest.py) fetches once per anchor with the widest grid values and evaluates every grid point
on the same facts. Thresholds and the runtime LLR come from detectors/backtests/<name>.json (written by the backtest):
LLR = min(2.0, 0.5 * ln(lift)) when the rule is validated (lift >= 3, support >= 15, cleared hits <= 3), otherwise a weak
signal capped at 0.5. A detector without a backtest file emits nothing. No case ids, no transaction ids, no hand weights.

Anchor = one transaction (dict with TransactionID, card_id, ts, amt, ProductCD, channel and the entity columns);
at runtime it is the flagged transaction of the case.
"""
import json
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from kavach.assess import Signal
from kavach.config import ROOT
from kavach.duckq_ext import ENTITY_FIELDS, entity_value

BACKTEST_DIR = ROOT / "detectors" / "backtests"
NAMES = ("common_origin", "dormant_wake", "micro_charges", "micro_charges_peers", "recipient_fanin",
         "proxy_new_burst", "proxy_new_burst_ring", "amount_ladder", "amount_ladder_ring")
VALIDATED = {"min_lift": 3.0, "min_support": 15, "max_cleared": 3}
WEAK_CAP = 0.5
MAX_ENTITIES = 3          # common_origin: neighbour lookups per case (each costs two tool calls; the harness bounds a case at 80)
MAX_LLR = 2.0

# Default parameters (used by the backtest as the grid centre; the runtime uses the backtest's chosen values)
DEFAULTS = {
    "common_origin": {"days": 14, "min_cards": 3, "max_share": 0.02, "min_frac": 0.3, "rarity_days": 30, "use_device_new": False,
                      "fields": ("R_emaildomain", "P_emaildomain", "addr1", "product_band", "access_signature", "holder_key")},
    "dormant_wake": {"gap_days": 45, "min_online": 2, "burst_hours": 48, "above_max": 1.0, "min_prior": 3},
    "micro_charges": {"max_amt": 5.0, "min_n": 5, "days": 7, "big_amt": 20.0, "habit_max": 0},
    "micro_charges_peers": {"max_amt": 5.0, "min_cards": 5, "days": 7, "tol": 0.001},
    "recipient_fanin": {"min_cards": 5, "days": 14, "ratio": 5.0, "template": False, "fields": ("R_emaildomain",)},
    "proxy_new_burst": {"min_n": 2, "min_amt": 200.0, "hours": 72, "require_unseen": False},
    "proxy_new_burst_ring": {"min_n": 2, "min_amt": 200.0, "hours": 72, "require_unseen": False, "min_other": 3, "days": 14},
    "amount_ladder": {"min_n": 4, "min_steps": 3, "ratio": 1.5, "min_max": 1000.0, "days": 14},
    "amount_ladder_ring": {"min_n": 4, "min_steps": 3, "ratio": 1.5, "min_max": 1000.0, "days": 14, "min_other": 3, "ring_days": 14},
}


# ----------------------------------------------------------------------------------------------- helpers
def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and str(v) not in ("", "nan", "None")


def _tid(x) -> str:
    return str(int(x))


def _day_end(ts) -> pd.Timestamp:
    """End of the anchor's calendar day: window ends are day-aligned so calls repeat (and cache) across anchors."""
    return pd.Timestamp(ts).normalize() + pd.Timedelta(days=1)


def valid_holder(hk) -> bool:
    return isinstance(hk, str) and "||" not in hk and not hk.endswith("|")


def lift_to_llr(bt: dict) -> float:
    """Runtime weight from a backtest record (no hand numbers)."""
    lift = bt.get("lift")
    if not lift or lift <= 0:
        return 0.0
    llr = 0.5 * math.log(lift)
    if bt.get("status") == "validated":
        return round(max(0.0, min(MAX_LLR, llr)), 2)
    return round(max(0.0, min(WEAK_CAP, llr)), 2)


def load_backtests(directory=None) -> dict:
    """name -> {"params", "llr", "status", "lift", ...} for every backtest file present."""
    d = directory or BACKTEST_DIR
    out = {}
    for n in NAMES:
        p = d / f"{n}.json"
        if p.exists():
            bt = json.loads(p.read_text(encoding="utf-8"))
            chosen = bt.get("chosen") or {}
            params = dict(DEFAULTS[n])
            params.update(chosen.get("params") or {})
            for k in ("fields",):
                if k in params and isinstance(params[k], list):
                    params[k] = tuple(params[k])
            out[n] = {"params": params, "status": chosen.get("status", "weak"), "lift": chosen.get("lift"),
                      "support": chosen.get("support"), "cleared_hits": chosen.get("cleared_hits"),
                      "llr": chosen.get("llr", lift_to_llr(chosen))}
    return out


def anchor_from_ctx(ctx) -> dict:
    a = dict(ctx.f)
    a["card_id"] = ctx.card_id
    a["ts"] = pd.Timestamp(a["ts"])
    return a


# ----------------------------------------------------------------------------------------------- dormant_wake
def dormant_fetch(T, anchor, params=None) -> dict:
    return {"h": T.call("card_history", card_id=anchor["card_id"])}


def _dormant_account(facts, anchor):
    """(label, account rows sorted by time, ts array) for the anchor; cached per holder key in facts."""
    h = facts["h"]
    t0 = pd.Timestamp(anchor["ts"])
    hk = anchor.get("holder_key")
    c = facts.setdefault("_c", {})
    if "card" not in c:
        hs = h.sort_values(["ts", "TransactionID"]).reset_index(drop=True)
        c["card"] = (f"card {anchor['card_id']}", hs, hs.ts.values)
    if valid_holder(hk):
        if ("hk", hk) not in c:
            hs = c["card"][1]
            hh = hs[hs.holder_key == hk].reset_index(drop=True)
            c[("hk", hk)] = (f"account {hk}", hh, hh.ts.values)
        acct = c[("hk", hk)]
        if (acct[2] < np.datetime64(t0.to_datetime64())).sum() >= 3:
            return acct
    return c["card"]


def dormant_rule(facts, anchor, p) -> dict | None:
    """Account silent for >= gap_days, then >= min_online online txns within burst_hours of the first txn after the gap,
    or one of them above the account's prior maximum times above_max (0 disables that branch). The anchor must sit in
    the burst. Account = the holder_key when it has >= 3 earlier rows on the card (January D1 is resampled, so a fresh
    holder_key carries no history), else the card."""
    h = facts["h"]
    if h is None or not len(h):
        return None
    label, acct, ts = _dormant_account(facts, anchor)
    t0n = np.datetime64(pd.Timestamp(anchor["ts"]).to_datetime64())
    hrs = np.timedelta64(int(p["burst_hours"] * 3600), "s")
    gap = np.timedelta64(int(p["gap_days"] * 86400), "s")
    lo, hi = np.searchsorted(ts, t0n - hrs, "left"), np.searchsorted(ts, t0n, "right")
    wake = None
    for i in range(max(lo, max(1, p["min_prior"])), hi):
        if ts[i] - ts[i - 1] >= gap:
            wake = i
            break
    if wake is None:
        return None
    w_ts = ts[wake]
    end = np.searchsorted(ts, w_ts + hrs, "right")
    burst = acct.iloc[wake:end]
    if int(anchor["TransactionID"]) not in set(int(x) for x in burst.TransactionID):
        return None
    prior_max = float(acct.amt.values[:wake].max())
    online = burst[burst.channel == "online"]
    by_online = len(online) >= p["min_online"]
    above = burst[burst.amt > prior_max * p["above_max"]] if p["above_max"] else burst.iloc[0:0]
    if not by_online and not len(above):
        return None
    gap_days = float((w_ts - ts[wake - 1]) / np.timedelta64(1, "D"))
    return {"account": label, "gap_days": round(gap_days, 1), "wake_txn": _tid(acct.TransactionID.iloc[wake]),
            "last_before": _tid(acct.TransactionID.iloc[wake - 1]), "burst_ids": [_tid(x) for x in burst.TransactionID],
            "n_online": int(len(online)), "prior_max": round(prior_max, 2), "max_amt": round(float(burst.amt.max()), 2),
            "by": "online_burst" if by_online else "above_prior_max"}


# ----------------------------------------------------------------------------------------------- micro_charges
def micro_fetch(T, anchor, params=None) -> dict:
    return {"h": T.call("card_history", card_id=anchor["card_id"])}


def _micro_prep(facts, max_amt, big_amt):
    """Sorted micro-charge and large-purchase arrays for one card (cached in facts per threshold pair)."""
    c = facts.setdefault("_c", {})
    key = ("micro", max_amt, big_amt)
    if key not in c:
        h = facts["h"]
        on = h[h.channel == "online"].sort_values(["ts", "TransactionID"])
        micro = on[on.amt < max_amt].reset_index(drop=True)
        big = on[on.amt >= big_amt]
        c[key] = (micro, micro.ts.values, big.ts.values)
    return c[key]


def micro_rule(facts, anchor, p) -> dict | None:
    """>= min_n online charges under max_amt inside one `days`-long span that contains the anchor (itself a micro charge),
    with no larger purchase (>= big_amt) on the card within an hour after any of them (that shape is R5 card testing)
    and none on the same identified device inside the span; the card's 90 days before the span hold at most habit_max
    micro charges (habitual micro-purchasers are not testing)."""
    if anchor["channel"] != "online" or float(anchor["amt"]) >= p["max_amt"]:
        return None
    micro, mts, bts = _micro_prep(facts, p["max_amt"], p["big_amt"])
    if len(mts) < p["min_n"]:
        return None
    t0 = np.datetime64(pd.Timestamp(anchor["ts"]).to_datetime64())
    D = np.timedelta64(int(p["days"] * 86400), "s")
    lo, hi = np.searchsorted(mts, t0 - D, "left"), np.searchsorted(mts, t0, "right")
    if hi <= lo:
        return None
    starts = np.arange(lo, hi)
    ends = np.searchsorted(mts, mts[starts] + D, "right")
    k = int(np.argmax(ends - starts))
    i, j = int(starts[k]), int(ends[k])
    if j - i < p["min_n"]:
        return None
    span_ts = mts[i:j]
    hour = np.timedelta64(3600, "s")
    if len(bts) and (np.searchsorted(bts, span_ts + hour, "right") > np.searchsorted(bts, span_ts, "right")).any():
        return None
    best = micro.iloc[i:j]
    s0, e0 = best.ts.iloc[0], best.ts.iloc[0] + pd.Timedelta(days=p["days"])
    devs = {d for d in best.device_key if _ok(d) and str(d).replace("|", "").strip()}
    if devs:
        h = facts["h"]
        if len(h[(h.ts >= s0) & (h.ts <= e0) & h.device_key.isin(devs) & (h.amt >= p["big_amt"])]):
            return None
    n_habit = i - int(np.searchsorted(mts, mts[i] - np.timedelta64(90 * 86400, "s"), "left"))
    if p["habit_max"] is not None and n_habit > p["habit_max"]:
        return None
    return {"ids": [_tid(x) for x in best.TransactionID], "n": int(j - i), "span_days": round((best.ts.iloc[-1] - s0).total_seconds() / 86400, 1),
            "amt_min": round(float(best.amt.min()), 2), "amt_max": round(float(best.amt.max()), 2), "prior_micro_90d": int(n_habit)}


def micro_peers_fetch(T, anchor, params) -> dict:
    if anchor["channel"] != "online" or float(anchor["amt"]) >= params["max_amt"]:
        return {"peers": None}
    return {"peers": T.call("amount_peers", product_cd=anchor["ProductCD"], amt=round(float(anchor["amt"]), 2), t0=anchor["ts"],
                            days=params["days"], tol=params["tol"])}


def micro_peers_rule(facts, anchor, p) -> dict | None:
    """The same micro amount (to the cent) on >= min_cards cards (this one included) within `days` of the anchor."""
    peers = facts.get("peers")
    if peers is None or not len(peers) or float(anchor["amt"]) >= p["max_amt"]:
        return None
    t0 = pd.Timestamp(anchor["ts"])
    m = peers[((peers.amt - float(anchor["amt"])).abs() < 0.005) & ((peers.ts - t0).abs() <= pd.Timedelta(days=p["days"]))]
    cards = sorted(set(m.card_id) | {anchor["card_id"]})
    if len(cards) < p["min_cards"]:
        return None
    others = m[m.card_id != anchor["card_id"]]
    return {"cards": [c for c in cards if c != anchor["card_id"]], "ids": [_tid(x) for x in others.TransactionID][:20],
            "amt": round(float(anchor["amt"]), 2)}


# ----------------------------------------------------------------------------------------------- recipient_fanin
def fanin_fetch(T, anchor, params) -> dict:
    """entity_baseline for each email field the anchor carries at every window length in params['days_list']; the neighbour
    rows (entity_neighbors) only where the baseline already allows a fire at the loosest grid point."""
    base, nb = {}, {}
    t_end = _day_end(anchor["ts"])
    k_min, ratio = params.get("k_min", params["min_cards"]), params.get("ratio_min", params["ratio"])
    for field in params["fields"]:
        v = anchor.get(field)
        if not _ok(v):
            continue
        for d in params["days_list"]:
            b = T.call("entity_baseline", entity_field=field, value=str(v), t0=t_end, days=d)
            base[(field, d)] = b
            if b["baseline_buckets"] >= 2 and b["cards_window"] >= k_min and b["baseline_per_window"] * ratio < b["cards_window"]:
                nb[(field, d)] = T.call("entity_neighbors", entity_field=field, value=str(v), t_from=t_end - pd.Timedelta(days=d), t_to=t_end)
    return {"base": base, "nb": nb, "t_end": t_end}


def fanin_rule(facts, anchor, p) -> dict | None:
    """The anchor's recipient (or purchaser) domain was paid by >= min_cards distinct cards in the `days` up to the end of the
    anchor's day, and its own history (mean distinct cards per `days` over the previous 90 days, on >= 2 complete windows)
    is below 1/ratio of that. With template=True, >= min_cards of those cards also paid within 5% of the anchor amount."""
    for field in p["fields"]:
        b = facts["base"].get((field, p["days"]))
        if not b or b["baseline_buckets"] < 2 or b["cards_window"] < p["min_cards"]:
            continue
        if b["baseline_per_window"] * p["ratio"] >= b["cards_window"]:
            continue
        nb = facts["nb"].get((field, p["days"]))
        if nb is None:
            continue
        if p.get("template"):
            close = nb[(nb.amt - float(anchor["amt"])).abs() <= 0.05 * float(anchor["amt"])]
            if close.card_id.nunique() < p["min_cards"]:
                continue
        return {"field": field, "value": b["value"], "cards_window": b["cards_window"], "baseline": b["baseline_per_window"],
                "days": p["days"], "t_end": str(facts["t_end"]), "cards": [c for c in dict.fromkeys(nb.card_id) if c != anchor["card_id"]],
                "ids": [_tid(x) for x in nb.TransactionID]}
    return None


# ----------------------------------------------------------------------------------------------- common_origin
def _strong(df, use_device_new: bool = False) -> pd.Series:
    """Loss-like for this card: a product it never used, an amount above every earlier one (on the card, or for that
    product code), or its first proxied connection (optionally also a device the identity record marks New, which is
    common on its own)."""
    m = df.product_unseen.fillna(False).astype(bool) | df.amt_above_prior_max.fillna(False).astype(bool) |         df.amt_above_product_max.fillna(False).astype(bool) | df.proxy_unseen.fillna(False).astype(bool)
    return m | df.device_new.fillna(False).astype(bool) if use_device_new else m


def origin_fetch(T, anchor, params) -> dict:
    """The card's own atypical window, then for every entity it touched around the start of that run: rarity (one
    entity_shares call per field over `rarity_days`), and for the rarest eligible ones (at most max_entities) the neighbour
    rows and the neighbours' atypical windows. params['days'] is the widest grid value."""
    d_max = params["days"]
    t_end = _day_end(anchor["ts"])
    t0 = pd.Timestamp(anchor["ts"])
    own = T.call("card_atypical_window", card_id=anchor["card_id"], t_from=t_end - pd.Timedelta(days=2 * d_max), t_to=t_end)
    facts = {"own": own, "t_end": t_end, "ents": {}}
    run = own[(own.ts >= t0 - pd.Timedelta(days=d_max)) & (own.ts <= t0)]
    run = run[_strong(run, params.get("use_device_new", False))]
    if not len(run):
        return facts
    s = run.ts.min()
    touch = own[(own.ts >= s - pd.Timedelta(days=d_max)) & (own.ts <= t0)]
    if "DeviceType" not in touch:
        touch = touch.assign(DeviceType=None)
    vals = {}
    for r in touch.to_dict("records"):
        for f in params["fields"]:
            v = entity_value(f, r)
            if v is not None:
                vals.setdefault((f, v), r["ts"])
    for f in params["fields"]:
        fv = sorted({v for (ff, v) in vals if ff == f})
        if not fv:
            continue
        sh = T.call("entity_shares", entity_field=f, values=fv, t0=t_end, days=params["rarity_days"])
        for r in sh.itertuples():
            facts["ents"][(f, r.value)] = {"base": {"cards_window": int(r.cards), "share_window": float(r.share)}}
    # neighbour rows only for the rarest entities that could still reach min_cards other cards (bounded tool budget);
    # the backtest passes several screens ("variants") and fetches the union, so every grid point sees its own top entities
    variants = params.get("variants") or [(params["fields"], params["max_share_fetch"], params["min_cards_fetch"])]
    pick = []
    for fields, max_share, min_cards in variants:
        elig = sorted([(e["base"]["share_window"], k) for k, e in facts["ents"].items()
                       if k[0] in fields and e["base"]["share_window"] <= max_share and e["base"]["cards_window"] >= min_cards + 1])
        pick += [k for _, k in elig[:params.get("max_entities", MAX_ENTITIES)] if k not in pick]
    for (f, v) in pick:
        ent = facts["ents"][(f, v)]
        nb = T.call("entity_neighbors", entity_field=f, value=v, t_from=t_end - pd.Timedelta(days=2 * d_max), t_to=t_end)
        others = sorted(set(nb.card_id) - {anchor["card_id"]})
        ent["nb"] = nb
        ent["atyp"] = (T.call("card_atypical_window", card_id=others, t_from=t_end - pd.Timedelta(days=2 * d_max), t_to=t_end)
                       if others else None)
    return facts


def _own_run(facts, anchor, days, use_dev):
    """(start of the card's loss-like run, entity values the card touched from `days` before that start to the anchor,
    run txn ids) for one window length; cached in facts."""
    key = ("own", days, use_dev)
    c = facts.setdefault("_c", {})
    if key not in c:
        own, t0, d = facts["own"], pd.Timestamp(anchor["ts"]), pd.Timedelta(days=days)
        run = own[(own.ts >= t0 - d) & (own.ts <= t0)]
        run = run[_strong(run, use_dev)]
        if not len(run):
            c[key] = None
        else:
            s = run.ts.min()
            mine = own[(own.ts >= s - d) & (own.ts <= t0)]
            if "DeviceType" not in mine:
                mine = mine.assign(DeviceType=None)
            vals = {(f, entity_value(f, r)) for r in mine.to_dict("records") for f in ENTITY_FIELDS}
            c[key] = (s, vals, [_tid(x) for x in run.TransactionID])
    return c[key]


def _entity_matches(facts, anchor, f, v, days, use_dev):
    """Other cards that touched (f, v) inside the 2*days window and started loss-like activity within `days` after their
    first touch: (n cards that touched, matched cards, their first-touch txn ids, their first loss-like txn ids); cached."""
    key = ("ent", f, v, days, use_dev)
    c = facts.setdefault("_c", {})
    if key not in c:
        ent = facts["ents"][(f, v)]
        d = pd.Timedelta(days=days)
        w0 = facts["t_end"] - 2 * d
        nb = ent["nb"]
        nb = nb[(nb.ts >= w0) & (nb.card_id != anchor["card_id"])]
        if not len(nb):
            c[key] = (0, [], [], [])
        else:
            first = nb.sort_values(["ts", "TransactionID"]).groupby("card_id").head(1)[["card_id", "ts", "TransactionID"]]
            first = first.rename(columns={"ts": "e_ts", "TransactionID": "origin_txn"}).sort_values("e_ts")
            at = ent["atyp"]
            matched = first.iloc[0:0]
            if at is not None and len(at):
                at = at[(at.ts >= w0) & _strong(at, use_dev)][["card_id", "ts", "TransactionID"]].sort_values("ts")
                if len(at):
                    m = pd.merge_asof(first, at, left_on="e_ts", right_on="ts", by="card_id", direction="forward", tolerance=d)
                    matched = m[m.ts.notna()]
            c[key] = (int(len(first)), sorted(matched.card_id), [_tid(x) for x in matched.origin_txn],
                      [_tid(x) for x in matched.TransactionID] if len(matched) else [])
    return c[key]


def origin_rule(facts, anchor, p) -> dict | None:
    """The card's loss-like run (a product never used before, an amount above every earlier one or its first proxied
    connection, starting within `days` before the anchor) touched a rare entity E (on <= max_share of the active cards over
    `rarity_days`) that >= min_cards other cards also touched in the 2*days window, each of whom started its own loss-like
    activity within `days` after its first touch of E; and those cards are >= min_frac of every card that touched E."""
    own = facts["own"]
    if own is None or not len(own):
        return None
    use_dev = p.get("use_device_new", False)
    run = _own_run(facts, anchor, p["days"], use_dev)
    if run is None:
        return None
    s, mine, run_ids = run
    # same screen as the fetch at runtime: the rarest eligible entities, at most MAX_ENTITIES of them
    elig = sorted([(e["base"]["share_window"], k) for k, e in facts["ents"].items() if k[0] in p["fields"]
                   and e["base"]["share_window"] <= p["max_share"] and e["base"]["cards_window"] >= p["min_cards"] + 1])
    best = None
    for share, (f, v) in elig[:p.get("max_entities", MAX_ENTITIES)]:
        if "nb" not in facts["ents"][(f, v)] or (f, v) not in mine:
            continue
        n_touch, cards, origin_ids, loss_ids = _entity_matches(facts, anchor, f, v, p["days"], use_dev)
        k, frac = len(cards), len(cards) / max(n_touch, 1)
        if k < p["min_cards"] or frac < p["min_frac"]:
            continue
        key = (frac, k, -share)
        if best is None or key > best["key"]:
            nb = facts["ents"][(f, v)]["nb"]
            own_touch = nb[(nb.card_id == anchor["card_id"]) & (nb.ts >= facts["t_end"] - 2 * pd.Timedelta(days=p["days"]))]
            best = {"key": key, "field": f, "value": v, "k": k, "frac": round(frac, 2), "n_touch": n_touch, "share": share,
                    "cards": cards, "origin_ids": origin_ids, "loss_ids": loss_ids, "run_start": str(s), "own_run_ids": run_ids,
                    "own_touch_ids": [_tid(x) for x in own_touch.TransactionID]}
    if best:
        best.pop("key")
    return best


# ----------------------------------------------------------------------------------------------- proxy_new_burst
ANON = "IP_PROXY:ANONYMOUS"


def _is_new_anon(df) -> pd.Series:
    return (df.id_15.astype("string") == "New").fillna(False) & df.id_23.astype("string").str.contains("ANONYMOUS", regex=False).fillna(False)


def _burst_in(rows: pd.DataFrame, t0, hours: float, min_n: int, must=None):
    """Largest set of rows inside one `hours` span that contains t0 (and the row `must`, when given); None below min_n."""
    if len(rows) < min_n:
        return None
    ts = rows.ts.values
    t0n = np.datetime64(pd.Timestamp(t0).to_datetime64())
    H = np.timedelta64(int(hours * 3600), "s")
    lo, hi = np.searchsorted(ts, t0n - H, "left"), np.searchsorted(ts, t0n, "right")
    best = None
    for i in range(lo, hi):
        j = int(np.searchsorted(ts, ts[i] + H, "right"))
        if j - i >= min_n and (best is None or j - i > best[1] - best[0]):
            span = rows.iloc[i:j]
            if must is None or must in set(int(x) for x in span.TransactionID):
                best = (i, j)
    return None if best is None else rows.iloc[best[0]:best[1]]


def proxy_burst_rule(facts, anchor, p) -> dict | None:
    """>= min_n online purchases of the anchor's product, each >= min_amt, within `hours`, every one from a device the
    identity record marks New behind an anonymous proxy; with require_unseen, the card never used that product before
    the burst (and has >= 3 earlier transactions)."""
    if anchor["channel"] != "online" or float(anchor["amt"]) < p["min_amt"]:
        return None
    if anchor.get("id_15") != "New" or "ANONYMOUS" not in str(anchor.get("id_23") or ""):
        return None
    c = facts.setdefault("_c", {})
    key = ("pnb", anchor["ProductCD"], p["min_amt"])
    if key not in c:
        h = facts["h"]
        r = h[(h.channel == "online") & (h.ProductCD == anchor["ProductCD"]) & (h.amt >= p["min_amt"])]
        c[key] = r[_is_new_anon(r)].sort_values(["ts", "TransactionID"]).reset_index(drop=True)
    burst = _burst_in(c[key], anchor["ts"], p["hours"], p["min_n"], must=int(anchor["TransactionID"]))
    if burst is None:
        return None
    if p.get("require_unseen"):
        h = facts["h"]
        before = h[h.ts < burst.ts.iloc[0]]
        if len(before) < 3 or (before.ProductCD == anchor["ProductCD"]).any():
            return None
    devs = sorted({d for d in burst.device_key if _ok(d) and str(d).replace("|", "").strip()})
    dt = anchor.get("DeviceType")
    return {"ids": [_tid(x) for x in burst.TransactionID], "n": int(len(burst)), "product": anchor["ProductCD"],
            "amt_min": round(float(burst.amt.min()), 2), "amt_max": round(float(burst.amt.max()), 2),
            "span_hours": round((burst.ts.iloc[-1] - burst.ts.iloc[0]).total_seconds() / 3600, 1), "devices": devs,
            "signature": f"{anchor['ProductCD']}|{ANON}|New|{dt if _ok(dt) else ''}"}


def proxy_ring_fetch(T, anchor, params, h=None) -> dict:
    """The case card's history (single-card rule) and, when the burst fires for any single-card variant, every transaction
    with the same access signature within ring days of the anchor."""
    facts = {"h": T.call("card_history", card_id=anchor["card_id"]) if h is None else h, "ring": {}}
    t_end = _day_end(anchor["ts"])
    for sp in params["single_variants"]:
        hit = proxy_burst_rule(facts, anchor, sp)
        if hit and hit["signature"] not in facts["ring"]:
            facts["ring"][hit["signature"]] = T.call("entity_neighbors", entity_field="access_signature", value=hit["signature"],
                                                     t_from=t_end - pd.Timedelta(days=params["days"] + 1),
                                                     t_to=t_end + pd.Timedelta(days=params["days"]))
    return facts


def proxy_ring_rule(facts, anchor, p) -> dict | None:
    """The single-card burst, repeated as the same template (product, New device behind an anonymous proxy, >= min_amt,
    >= min_n within `hours`) on >= min_other other cards within `days` of the anchor, none sharing a device with this card."""
    hit = proxy_burst_rule(facts, anchor, p)
    if not hit or hit["signature"] not in facts["ring"]:
        return None
    nb = facts["ring"][hit["signature"]]
    t0 = pd.Timestamp(anchor["ts"])
    nb = nb[(nb.card_id != anchor["card_id"]) & (nb.channel == "online") & (nb.amt >= p["min_amt"])
            & ((nb.ts - t0).abs() <= pd.Timedelta(days=p["days"]))].sort_values(["ts", "TransactionID"])
    own_dev = set(hit["devices"])
    cards, ids = [], []
    for c, g in nb.groupby("card_id"):
        if own_dev & {d for d in g.device_key if _ok(d)}:
            continue
        for t in g.ts:
            b = _burst_in(g, t, p["hours"], p["min_n"])
            if b is not None:
                cards.append(c)
                ids += [_tid(x) for x in b.TransactionID]
                break
    if len(cards) < p["min_other"]:
        return None
    return {**hit, "cards": sorted(cards), "ring_ids": ids, "n_cards": len(cards)}


# ----------------------------------------------------------------------------------------------- amount_ladder
def _ladders(rows: pd.DataFrame, p) -> list:
    """Row position spans (start, end inclusive) of every ladder; see _ladder_spans."""
    return [rows.iloc[a:b + 1] for a, b in _ladder_spans(rows, p)]


def _ladder_spans(rows: pd.DataFrame, p) -> list:
    """Every ladder in time-ordered purchases (one pass, O(n)): a maximal run of steps each >= ratio times the previous,
    trimmed from the left until its stepping part fits in `days` (keeping >= min_steps steps), then the purchases that stay
    at its top (within 1%, a cap) within `days` of reaching it; kept when it has >= min_n purchases and a top >= min_max."""
    out = []
    if len(rows) < p["min_n"]:
        return out
    a = rows.amt.values
    ts = rows.ts.values
    D = np.timedelta64(int(p["days"] * 86400), "s")
    i, n = 0, len(a)
    while i < n - 1:
        j = i
        while j + 1 < n and a[j] > 0 and a[j + 1] >= p["ratio"] * a[j]:
            j += 1
        if j - i < p["min_steps"]:
            i = j + 1
            continue
        s0 = i
        while ts[j] - ts[s0] > D and j - s0 > p["min_steps"]:
            s0 += 1
        k = j
        if ts[j] - ts[s0] <= D:
            while k + 1 < n and a[k + 1] >= 0.99 * a[j] and ts[k + 1] - ts[j] <= D:
                k += 1
            if k + 1 - s0 >= p["min_n"] and float(a[j]) >= p["min_max"]:
                out.append((s0, k))
        i = k if k > j else j + 1
    return out


def _find_ladder(rows: pd.DataFrame, p, must=None):
    """The first ladder in `rows` (or the one containing transaction `must`), else None."""
    for lad in _ladders(rows, p):
        if must is None or must in set(int(x) for x in lad.TransactionID):
            return lad
    return None


def ladder_rule(facts, anchor, p) -> dict | None:
    """The anchor sits in an amount ladder on its card: online purchases of one product code stepping up by >= ratio at
    least min_steps times in a row, up to a top of >= min_max (often repeated at a cap), within `days` (ladders are found
    once over the card's whole same-product online sequence, then looked up by transaction)."""
    if anchor["channel"] != "online":
        return None
    c = facts.setdefault("_c", {})
    key = ("ladder", anchor["ProductCD"], p["min_n"], p["min_steps"], p["ratio"], p["min_max"], p["days"])
    if key not in c:
        h = facts["h"]
        rows = h[(h.channel == "online") & (h.ProductCD == anchor["ProductCD"])].sort_values(["ts", "TransactionID"]).reset_index(drop=True)
        tids = rows.TransactionID.values
        idx = {}
        for a0, b0 in _ladder_spans(rows, p):
            for t in tids[a0:b0 + 1]:
                idx.setdefault(int(t), (a0, b0))
        c[key] = (rows, idx)
    rows, idx = c[key]
    span = idx.get(int(anchor["TransactionID"]))
    if span is None:
        return None
    lad = rows.iloc[span[0]:span[1] + 1]
    steps = [round(float(y / x), 2) for x, y in zip(lad.amt.values[:-1], lad.amt.values[1:])]
    return {"ids": [_tid(x) for x in lad.TransactionID], "n": int(len(lad)), "product": anchor["ProductCD"],
            "cap": round(float(lad.amt.max()), 2), "first_amt": round(float(lad.amt.iloc[0]), 2), "steps": steps,
            "span_days": round((lad.ts.iloc[-1] - lad.ts.iloc[0]).total_seconds() / 86400, 1)}


def ladder_ring_fetch(T, anchor, params, h=None) -> dict:
    """Card history, and for every single-card variant that fires: the other cards' online purchases at the same top
    amount within ring_days (amount_peers), then those cards' purchases around them (card_atypical_window, at most 12 cards)."""
    facts = {"h": T.call("card_history", card_id=anchor["card_id"]) if h is None else h, "peers": {}}
    for sp in params["single_variants"]:
        hit = ladder_rule(facts, anchor, sp)
        if not hit or (hit["product"], hit["cap"]) in facts["peers"]:
            continue
        peers = T.call("amount_peers", product_cd=hit["product"], amt=hit["cap"], t0=anchor["ts"], days=params["ring_days"], tol=0.001)
        others = [c for c in dict.fromkeys(peers.card_id) if c != anchor["card_id"]][:12]
        t_end = _day_end(anchor["ts"])
        rows = (T.call("card_atypical_window", card_id=others, t_from=t_end - pd.Timedelta(days=params["ring_days"] + params["days"] + 1),
                       t_to=t_end + pd.Timedelta(days=params["ring_days"])) if others else None)
        facts["peers"][(hit["product"], hit["cap"])] = (peers, rows)
    return facts


def ladder_ring_rule(facts, anchor, p) -> dict | None:
    """The single-card ladder, repeated with the same top amount (within 1%) as a ladder on >= min_other other cards within
    ring_days."""
    hit = ladder_rule(facts, anchor, p)
    if not hit or (hit["product"], hit["cap"]) not in facts["peers"]:
        return None
    peers, rows = facts["peers"][(hit["product"], hit["cap"])]
    if rows is None or not len(rows):
        return None
    cards, ids = [], []
    for c, g in rows[(rows.channel == "online") & (rows.ProductCD == hit["product"])].groupby("card_id"):
        g = g.sort_values(["ts", "TransactionID"])
        lad = _find_ladder(g, p)
        if lad is not None and abs(float(lad.amt.max()) - hit["cap"]) <= 0.01 * hit["cap"]:
            cards.append(c)
            ids += [_tid(x) for x in lad.TransactionID]
    if len(cards) < p["min_other"]:
        return None
    return {**hit, "cards": sorted(cards), "ring_ids": ids, "n_cards": len(cards)}


# ----------------------------------------------------------------------------------------------- runtime
FIELD_WORDS = {"R_emaildomain": "recipient email domain", "P_emaildomain": "purchaser email domain", "addr1": "card-present billing region",
               "product_band": "product and amount band", "access_signature": "access signature (product|proxy|device flag|device type)",
               "holder_key": "account key"}


def _bt_note(bt: dict) -> str:
    if bt["status"] == "validated":
        return f"backtest on the closed cases: lift {bt['lift']:.1f}, {bt['support']} confirmed-fraud hits, {bt['cleared_hits']} cleared hits"
    lift = f"lift {bt['lift']:.1f}" if bt.get("lift") else "no measurable lift"
    return f"weak signal: the closed cases do not validate this rule ({lift}, {bt.get('support') or 0} confirmed-fraud hits)"


def run_dormant(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    hit = dormant_rule({"h": ctx.h}, a, p)          # ctx.h came from the card_history tool in gather
    if not hit:
        return []
    claim = (f"Dormant {hit['account']} woke up: no transaction for {hit['gap_days']:.0f} days (since txn {hit['last_before']}), then "
             f"{len(hit['burst_ids'])} transaction(s) within {p['burst_hours']} hours from txn {hit['wake_txn']}, {hit['n_online']} online, "
             f"largest ${hit['max_amt']:,.2f} against an earlier maximum of ${hit['prior_max']:,.2f} ({_bt_note(bt)})")
    fnd.notes.setdefault("typologies", []).append({"name": "dormant_wake", **hit, "status": bt["status"], "llr": bt["llr"]})
    return [Signal("dormant_wake", "sequence", bt["llr"], claim, [hit["last_before"]] + hit["burst_ids"],
                   T.ref("card_history", card_id=ctx.card_id), how={"tool": "card_history", "params": {"card_id": ctx.card_id}})]


def run_micro(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    hit = micro_rule({"h": ctx.h}, a, p)
    if not hit:
        return []
    claim = (f"{hit['n']} online charges of ${hit['amt_min']:,.2f} to ${hit['amt_max']:,.2f} within {hit['span_days']:.1f} days on card "
             f"{ctx.card_id}, each under ${p['max_amt']:,.0f}, with no larger purchase after them and only {hit['prior_micro_90d']} such charge(s) "
             f"in the 90 days before: slow card probing rather than the one-hour R5 test ({_bt_note(bt)})")
    fnd.notes.setdefault("typologies", []).append({"name": "micro_charges", **hit, "status": bt["status"], "llr": bt["llr"]})
    return [Signal("micro_charges", "sequence", bt["llr"], claim, hit["ids"], T.ref("card_history", card_id=ctx.card_id),
                   how={"tool": "card_history", "params": {"card_id": ctx.card_id}})]


def run_micro_peers(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    facts = micro_peers_fetch(T, a, p)
    hit = micro_peers_rule(facts, a, p)
    if not hit:
        return []
    params = {"product_cd": a["ProductCD"], "amt": hit["amt"], "t0": str(a["ts"]), "days": p["days"], "tol": p["tol"]}
    claim = (f"The same micro amount ${hit['amt']:,.2f} (product {a['ProductCD']}) was charged on {len(hit['cards'])} other card(s) within "
             f"{p['days']} days ({_bt_note(bt)})")
    for c in hit["cards"]:
        fnd.notes.setdefault("related_cards", {}).setdefault(c, f"same micro amount ${hit['amt']:,.2f}")
    fnd.notes.setdefault("typologies", []).append({"name": "micro_charges_peers", **hit, "status": bt["status"], "llr": bt["llr"]})
    return [Signal("micro_charges_peers", "network", bt["llr"], claim, hit["cards"][:10] + hit["ids"][:10],
                   T.ref("amount_peers", **params), how={"tool": "amount_peers", "params": params})]


def run_fanin(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    facts = fanin_fetch(T, a, {**p, "days_list": (p["days"],)})
    hit = fanin_rule(facts, a, p)
    if not hit:
        return []
    t_from = pd.Timestamp(hit["t_end"]) - pd.Timedelta(days=p["days"])
    params = {"entity_field": hit["field"], "value": hit["value"], "t_from": str(t_from), "t_to": hit["t_end"]}
    others = hit.get("cards", [])
    claim = (f"{FIELD_WORDS[hit['field']].capitalize()} {hit['value']} was paid by {hit['cards_window']} distinct cards in the {p['days']} days "
             f"to {pd.Timestamp(hit['t_end']):%Y-%m-%d}, against {hit['baseline']:.1f} cards per {p['days']} days over the previous 90 days: "
             f"many cards suddenly paying one recipient ({_bt_note(bt)})")
    for c in others:
        fnd.notes.setdefault("related_cards", {}).setdefault(c, f"paid {FIELD_WORDS[hit['field']]} {hit['value']} in the same window")
    element = f"{FIELD_WORDS[hit['field']]} {hit['value']}"
    fnd.notes["related_element"] = fnd.notes.get("related_element") or element
    if not fnd.shared_element:
        fnd.shared_element = element
    if bt["status"] == "validated":
        for c in others:
            fnd.connected_cards.setdefault(c, f"paid {element} during the fan-in")
    fnd.notes.setdefault("typologies", []).append({"name": "recipient_fanin", **{k: v for k, v in hit.items() if k != "ids"},
                                                   "status": bt["status"], "llr": bt["llr"]})
    return [Signal("recipient_fanin", "network", bt["llr"], claim, others[:10] + [_tid(a["TransactionID"])],
                   T.ref("entity_neighbors", **params), how={"tool": "entity_neighbors", "params": params})]


def run_origin(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    facts = origin_fetch(T, a, {**p, "max_share_fetch": p["max_share"], "min_cards_fetch": p["min_cards"]})
    hit = origin_rule(facts, a, p)
    if not hit:
        return []
    t_from = facts["t_end"] - pd.Timedelta(days=2 * p["days"])
    params = {"entity_field": hit["field"], "value": hit["value"], "t_from": str(t_from), "t_to": str(facts["t_end"])}
    element = f"{FIELD_WORDS[hit['field']]} {hit['value']}"
    claim = (f"Common origin: this card's atypical activity (from {pd.Timestamp(hit['run_start']):%Y-%m-%d}) shares the rare {element} "
             f"(on {hit['share']:.2%} of active cards) with {hit['k']} other cards, each of which began its own atypical activity within "
             f"{p['days']} days after first touching it ({hit['frac']:.0%} of the {hit['n_touch']} cards that touched it; origin transactions "
             f"{', '.join(hit['origin_ids'][:5])}) ({_bt_note(bt)})")
    for c in hit["cards"]:
        fnd.notes.setdefault("related_cards", {}).setdefault(c, f"touched {element}, then atypical activity within {p['days']} days")
    fnd.notes["related_element"] = fnd.notes.get("related_element") or element
    fnd.shared_element = fnd.shared_element or element
    if bt["status"] == "validated":
        for c in hit["cards"]:
            fnd.connected_cards.setdefault(c, f"common origin: {element}")
    fnd.notes.setdefault("typologies", []).append({"name": "common_origin", **{k: v for k, v in hit.items() if k not in ("loss_ids",)},
                                                   "status": bt["status"], "llr": bt["llr"]})
    # entity ids are all rows of the re-runnable entity_neighbors result (attestation checks each one)
    return [Signal("common_origin", "network", bt["llr"], claim, hit["cards"][:10] + hit["origin_ids"][:10] + hit["own_touch_ids"][:5],
                   T.ref("entity_neighbors", **params), how={"tool": "entity_neighbors", "params": params})]


def run_proxy_burst(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    hit = proxy_burst_rule({"h": ctx.h}, a, p)
    if not hit:
        return []
    claim = (f"{hit['n']} online product {hit['product']} purchases of ${hit['amt_min']:,.2f} to ${hit['amt_max']:,.2f} within "
             f"{hit['span_hours']:.0f} hours on card {ctx.card_id}, every one from a device marked New behind an anonymous proxy "
             f"({_bt_note(bt)})")
    fnd.episode["proxy_new_burst"] = [int(x) for x in hit["ids"]]
    fnd.notes.setdefault("typologies", []).append({"name": "proxy_new_burst", **hit, "status": bt["status"], "llr": bt["llr"]})
    return [Signal("proxy_new_burst", "sequence", bt["llr"], claim, hit["ids"], T.ref("card_history", card_id=ctx.card_id),
                   how={"tool": "card_history", "params": {"card_id": ctx.card_id}})]


def _mark_coordinated(fnd, kind, llr, cards, reason, element):
    """Cross-card template: coordinated abuse across customers (R9). The other template cards become connected cards."""
    fnd.coordinated_undocumented = True
    fnd.undocumented_kind = kind
    fnd.vote("undocumented", max(llr, 0.01))
    for c in cards:
        fnd.connected_cards.setdefault(c, reason)
        fnd.notes.setdefault("related_cards", {}).setdefault(c, reason)
    fnd.notes["related_element"] = fnd.notes.get("related_element") or element
    fnd.shared_element = fnd.shared_element or element


def run_proxy_ring(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    if not proxy_burst_rule({"h": ctx.h}, a, p):
        return []                                   # no tool call unless the card itself shows the burst
    facts = proxy_ring_fetch(T, a, {**p, "single_variants": [p]}, h=ctx.h)
    hit = proxy_ring_rule(facts, a, p)
    if not hit:
        return []
    t_end = _day_end(a["ts"])
    params = {"entity_field": "access_signature", "value": hit["signature"], "t_from": str(t_end - pd.Timedelta(days=p["days"] + 1)),
              "t_to": str(t_end + pd.Timedelta(days=p["days"]))}
    element = (f"a repeated template of product {hit['product']} purchases of ${p['min_amt']:,.0f} or more in bursts from devices "
               "marked New behind an anonymous proxy, with no shared device")
    claim = (f"The same template (product {hit['product']}, New device behind an anonymous proxy, ${p['min_amt']:,.0f} or more, "
             f"{p['min_n']} or more within {p['hours']} hours) appears on {hit['n_cards']} other cards within {p['days']} days, none "
             f"sharing a device with this card: coordinated abuse across customers ({_bt_note(bt)})")
    fnd.episode["proxy_new_burst"] = [int(x) for x in hit["ids"]]
    _mark_coordinated(fnd, "proxy_new_burst", bt["llr"], hit["cards"], "same New-device anonymous-proxy purchase template", element)
    fnd.notes.setdefault("typologies", []).append({"name": "proxy_new_burst_ring", **{k: v for k, v in hit.items() if k != "ring_ids"},
                                                   "status": bt["status"], "llr": bt["llr"]})
    return [Signal("proxy_new_burst_ring", "network", bt["llr"], claim, hit["cards"][:10] + hit["ids"] + hit["ring_ids"][:10],
                   T.ref("entity_neighbors", **params), how={"tool": "entity_neighbors", "params": params})]


def run_ladder(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    hit = ladder_rule({"h": ctx.h}, a, p)
    if not hit:
        return []
    steps = ", ".join(f"x{x:g}" for x in hit["steps"][:6])
    claim = (f"Amount ladder on card {ctx.card_id}: {hit['n']} online product {hit['product']} purchases from ${hit['first_amt']:,.2f} "
             f"up to ${hit['cap']:,.2f} within {hit['span_days']:.1f} days (steps {steps}): a stepped cash-out probing the limit "
             f"({_bt_note(bt)})")
    fnd.episode["amount_ladder"] = [int(x) for x in hit["ids"]]
    fnd.notes.setdefault("typologies", []).append({"name": "amount_ladder", **hit, "status": bt["status"], "llr": bt["llr"]})
    return [Signal("amount_ladder", "sequence", bt["llr"], claim, hit["ids"], T.ref("card_history", card_id=ctx.card_id),
                   how={"tool": "card_history", "params": {"card_id": ctx.card_id}})]


def run_ladder_ring(ctx, fnd, bt) -> list[Signal]:
    T, a, p = ctx.tools, anchor_from_ctx(ctx), bt["params"]
    if not ladder_rule({"h": ctx.h}, a, p):
        return []
    facts = ladder_ring_fetch(T, a, {**p, "single_variants": [p]}, h=ctx.h)
    hit = ladder_ring_rule(facts, a, p)
    if not hit:
        return []
    peers = facts["peers"][(hit["product"], hit["cap"])][0]
    params = {"product_cd": hit["product"], "amt": hit["cap"], "t0": str(a["ts"]), "days": p["ring_days"], "tol": 0.001}
    cited = [_tid(x) for x in peers[peers.card_id.isin(hit["cards"] + [ctx.card_id])].TransactionID]
    element = f"a repeated amount ladder capped at ${hit['cap']:,.2f} (product {hit['product']})"
    claim = (f"The same ladder capped at ${hit['cap']:,.2f} appears on {hit['n_cards']} other cards within {p['ring_days']} days: "
             f"coordinated cash-out across customers ({_bt_note(bt)})")
    fnd.episode["amount_ladder"] = [int(x) for x in hit["ids"]]
    _mark_coordinated(fnd, "amount_ladder", bt["llr"], hit["cards"], f"same amount ladder capped at ${hit['cap']:,.2f}", element)
    fnd.notes.setdefault("typologies", []).append({"name": "amount_ladder_ring", **{k: v for k, v in hit.items() if k != "ring_ids"},
                                                   "status": bt["status"], "llr": bt["llr"]})
    return [Signal("amount_ladder_ring", "network", bt["llr"], claim, hit["cards"][:10] + cited[:20],
                   T.ref("amount_peers", **params), how={"tool": "amount_peers", "params": params})]


RUNNERS = {"common_origin": run_origin, "dormant_wake": run_dormant, "micro_charges": run_micro,
           "micro_charges_peers": run_micro_peers, "recipient_fanin": run_fanin, "proxy_new_burst": run_proxy_burst,
           "proxy_new_burst_ring": run_proxy_ring, "amount_ladder": run_ladder, "amount_ladder_ring": run_ladder_ring}


def detect(ctx, fnd, backtests: dict | None = None) -> list[Signal]:
    """Run every typology that has a backtest file and was not rejected by it; a detector with no backtest emits nothing."""
    bts = load_backtests() if backtests is None else backtests
    sigs: list[Signal] = []
    ctx.tools.step = 4
    for name in NAMES:
        if name in bts and bts[name]["status"] != "rejected":      # a rule its own backtest rejected is not run
            sigs += RUNNERS[name](ctx, fnd, bts[name])
    return sigs
