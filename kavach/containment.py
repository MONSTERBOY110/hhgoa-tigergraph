"""Containment planner (Round 2, challenge 7): given a ring, the smallest set of cards to block that cuts the most exposure,
with the legitimate customers the action would touch named explicitly.

Input: a ring = the case card plus its connected cards (an answer's connected_card_ids, or a dict), the element the
cards share (named by the agent's ring signal, else inferred from the index), and a decision time (the case's opened_at by
default: containment is decided when the alert lands; `--as-of end` plans at the end of the data instead).

Per ring card, from the DuckDB index (one attested query):
  * element transactions: the card's transactions carrying the shared element from the ring's first element transaction
    (looked up to LOOKBACK_DAYS before the decision) to the decision time; their sum is the card's realised ring
    exposure (already spent: blocking cannot stop it);
  * forward exposure if left open = the card's average daily ring spend (realised / days since its first element
    transaction, at least one day) times the remaining window (window end minus the decision, at least the 72-hour
    monitoring period);
  * compromised when it carries the element AND at least one element transaction is atypical for the card's own history
    (kavach.duckq_ext.card_atypical_window: first use of the product, amount above every earlier one overall or for that
    product, first proxied connection); likely legitimate collateral when its element transactions fit its own profile and
    the element is habitual for it (seen before the ring) or common (most cards carrying it are not ring cards); unclear
    otherwise; not yet active when it shows no element transaction before the decision (named from later data);
  * the customer who owns it (customer_id of its transactions).

Plan: greedy by forward exposure cut per card over the compromised cards, stopping when the next card's marginal cut is
below STOP_USD (R5's own block threshold: a purchase over $100 already cleared justifies a block; below it decline and
step-up suffice) or the next card does not look compromised. Unclear cards get STEP_UP_AUTH, every ring card
MONITOR_CONNECTED_CARDS. Routes: BLOCK_CARD L1 when the card's own realised exposure <= $2,500, else L2. BLOCK_ALL_CARDS
is never recommended (R10 needs two of one customer's cards with confirmed fraud or confirmed compromised credentials; a
ring is neither). Alternatives scored as exposure stopped minus PENALTY_USD per legitimate customer acted against:
block nothing (monitor all), block every ring card active at the decision, the plan. A hindsight check (the index holds
what happened after the decision) re-scores the three with the ring spend that actually followed; the planner never uses it.

    python -m kavach.containment HHF-006 [--as-of opened|end|<timestamp>] [--from cases|dryrun] [--verify]
    python -m kavach.containment all          every answer with connected cards; writes docs/round2/containment.md
"""
import argparse
import json
import math
import re
import sys
import time
from dataclasses import asdict, dataclass, field

import pandas as pd

from kavach.config import CASES_DIR, ROOT

DOC = ROOT / "docs" / "round2" / "containment.md"
OUT_DIR = ROOT / "logs" / "containment"
STOP_USD = 100.0          # marginal forward exposure below which a block is not proportionate (R5's $100 block threshold)
PENALTY_USD = 1000.0      # per legitimate customer acted against: the section 3a materiality line ($1,000 makes a loss reportable)
L1_MAX = 2500.0           # BLOCK_CARD is L1 up to $2,500 of exposure, L2 above (policy section 2)
WINDOW_DAYS = 30          # ring window from its first element transaction (the agent's ring lookups span 30 days)
MIN_HORIZON_DAYS = 3      # a containment decision covers at least the 72-hour monitoring period
LOOKBACK_DAYS = 30        # how far before the decision the ring's first element transaction is searched
SETTLE_DAYS = 7           # an element seen on the card this long before the ring started is the card's own habit
COMMON_SHARE = 0.5        # the element is common when ring cards are under half of the cards carrying it
CARD_RE = re.compile(r"^C\d{5}-K\d+$")


def _now() -> str:
    return pd.Timestamp.now().isoformat(timespec="seconds")


def _rel(p) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _expr(field_: str) -> str:
    from kavach.duckq_ext import ENTITY_FIELDS
    if field_ == "device_key":
        return "device_key"
    if field_ not in ENTITY_FIELDS:
        raise ValueError(f"unknown element field {field_!r}")
    return ENTITY_FIELDS[field_]


LABEL = {"device_key": "device profile", "R_emaildomain": "recipient email domain", "P_emaildomain": "purchaser email domain",
         "addr1": "card-present billing region", "access_signature": "access signature (product|proxy|device New|device type)",
         "product_band": "product and amount band", "holder_key": "account key"}


# ================================================================================================ the ring
@dataclass
class Ring:
    case_id: str
    case_card: str
    cards: list                       # case card first, then connected cards
    element: dict                     # {"field", "value", "label", "source"}
    as_of: pd.Timestamp
    as_of_label: str
    case_verdict: str = ""
    case_actions: list = field(default_factory=list)
    flagged_txn: str = ""


def _signal_element(signals: list[dict], cards: set) -> dict | None:
    """The element a ring signal of the agent queried: entity_field/entity + value, or a device_key, when the signal names
    two or more of the ring's cards."""
    best = None
    for s in signals or []:
        named = {x for x in s.get("entity_ids") or [] if CARD_RE.match(str(x))} & cards
        prm = ((s.get("how") or {}).get("params") or {})
        f, v = prm.get("entity_field") or prm.get("entity"), prm.get("value")
        if not (f and v) and prm.get("device_key"):
            f, v = "device_key", prm["device_key"]
        if f and v and len(named) >= 2 and (best is None or len(named) > best[0]):
            best = (len(named), {"field": f, "value": str(v), "label": LABEL.get(f, f),
                                 "source": f"named by the agent's signal {s.get('name')} ({(s.get('how') or {}).get('tool')})"})
    return best[1] if best else None


def find_element(cards: list, t_from, t_to) -> dict | None:
    """Infer the shared element from the index: the value carried by the most ring cards in the window, rare in the bank."""
    from kavach import duckq
    from kavach.detectors.network import generic_device
    best = None
    for f in ("device_key", "R_emaildomain", "P_emaildomain", "addr1", "access_signature"):
        e = _expr(f)
        df = duckq.q(f"""select {e} v, count(distinct card_id) k from txn where card_id in (select unnest(?)) and ts between ? and ?
                         and {e} is not null group by 1 order by k desc limit 5""", list(cards), pd.Timestamp(t_from).to_pydatetime(),
                     pd.Timestamp(t_to).to_pydatetime())
        for r in df.itertuples():
            if r.k < 2 or (f == "device_key" and generic_device(r.v, int(duckq.device_popularity(r.v)))):
                continue
            pop = int(duckq.q(f"select count(distinct card_id) n from txn where {e} = ? and ts between ? and ?", str(r.v),
                              pd.Timestamp(t_from).to_pydatetime(), pd.Timestamp(t_to).to_pydatetime()).n.iloc[0])
            score = (r.k, r.k / max(pop, 1))
            if best is None or score > best[0]:
                best = (score, {"field": f, "value": str(r.v), "label": LABEL[f], "source": f"inferred: carried by {r.k} ring cards, "
                                f"{pop} cards in the bank in the window"})
    return best[1] if best else None


def ring_from_answer(answer: dict, signals: list | None = None, as_of="opened", case_row: dict | None = None) -> Ring | None:
    """A Ring from an answer (cases/<id>.json or a dry-run answer). None when the answer lists no connected cards."""
    from kavach import duckq
    k = answer["case"]
    conn = [c for c in k.get("connected_card_ids") or [] if CARD_RE.match(str(c))]
    if not conn:
        return None
    cid = answer["case_id"]
    row = case_row or duckq.q("select * from cases where case_id = ?", cid).iloc[0].to_dict()
    flagged = str(int(float(row["flagged_txn_id"])))
    t = duckq.q("select card_id, ts from txn where TransactionID = ?", int(flagged))
    case_card = str(t.card_id.iloc[0]) if len(t) else row["card_id"]
    cards = [case_card] + [c for c in conn if c != case_card]
    a = _as_of(as_of, row)
    el = _signal_element(signals or [], set(cards)) or _sidecar_element(cid, set(cards))
    if el is None:
        el = find_element(cards, a - pd.Timedelta(days=LOOKBACK_DAYS), a)
    if el is None:
        raise ValueError(f"{cid}: no shared element could be named or inferred for {len(cards)} ring cards")
    return Ring(cid, case_card, cards, el, a, str(as_of), k.get("verdict", ""), [x["action"] for x in answer["next_best_actions"]["final"]],
                flagged)


def ring_from_dict(d: dict) -> Ring:
    """{"case_id", "cards": [case card first, ...], "element": {"field", "value"}, "as_of": ts} (for tests and ad-hoc rings)."""
    el = dict(d["element"])
    el.setdefault("label", LABEL.get(el["field"], el["field"]))
    el.setdefault("source", "given")
    return Ring(d.get("case_id", "adhoc"), d["cards"][0], list(dict.fromkeys(d["cards"])), el, pd.Timestamp(d["as_of"]),
                str(d["as_of"]), d.get("verdict", ""), d.get("actions", []), str(d.get("flagged_txn", "")))


def _sidecar_element(case_id: str, cards: set) -> dict | None:
    p = ROOT / "attestations" / f"{case_id}.json"
    if not p.exists():
        return None
    try:
        side = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sig = [{"name": c.get("signal") or c.get("id"), "entity_ids": (c.get("evidence") or {}).get("card_ids") or [], "how": c.get("how") or {}}
           for c in side.get("claims") or []]
    return _signal_element(sig, cards)


def _as_of(as_of, row: dict) -> pd.Timestamp:
    from kavach import duckq
    if as_of in (None, "opened"):
        return pd.Timestamp(row["opened_at"])
    if as_of == "end":
        return pd.Timestamp(duckq.q("select max(ts) m from txn").m.iloc[0])
    return pd.Timestamp(as_of)


# ================================================================================================ facts per card
RING_SQL = """select t.TransactionID, t.card_id, t.customer_id, t.ts, t.TransactionAmt amt, t.ProductCD, t.channel,
                     coalesce(({expr}) = ?, false) is_el
              from txn t where t.card_id in (select unnest(?)) and t.ts between ? and ? order by t.ts, t.TransactionID"""


def ring_rows(ring: Ring, horizon_end: pd.Timestamp) -> tuple[pd.DataFrame, dict]:
    """Every transaction of the ring cards from LOOKBACK_DAYS before the decision to horizon_end, with the element flag and
    the card-relative atypical flags (one SQL query plus the agent's card_atypical_window tool)."""
    from kavach import duckq, duckq_ext
    e = _expr(ring.element["field"])
    lo = ring.as_of - pd.Timedelta(days=LOOKBACK_DAYS)
    sql = RING_SQL.format(expr=e)
    params = [ring.element["value"], list(ring.cards), lo.to_pydatetime(), pd.Timestamp(horizon_end).to_pydatetime()]
    df = duckq.q(sql, *params)
    at = duckq_ext.card_atypical_window(list(ring.cards), lo, horizon_end)
    strong = ("product_unseen", "amt_above_prior_max", "amt_above_product_max", "proxy_unseen")
    at["strong"] = at[list(strong)].fillna(False).astype(bool).any(axis=1)
    at["why"] = [", ".join(n.replace("_", " ") for n in strong if bool(r[n])) for _, r in at[list(strong)].fillna(False).iterrows()]
    df = df.merge(at[["TransactionID", "strong", "why"]], on="TransactionID", how="left")
    df["strong"] = df.strong.fillna(False).astype(bool)
    how = {"sql": " ".join(sql.split()), "params": [ring.element["value"], list(ring.cards), str(lo), str(horizon_end)],
           "flags": f"kavach.duckq_ext.card_atypical_window({len(ring.cards)} ring cards, {lo}, {horizon_end})"}
    return df, how


def card_facts(ring: Ring, window_days: float = WINDOW_DAYS) -> dict:
    """Per-card exposure, projection and classification at the decision time (plus the hindsight rows after it)."""
    from kavach import duckq
    data_end = pd.Timestamp(duckq.q("select max(ts) m from txn").m.iloc[0])
    probe_end = min(ring.as_of + pd.Timedelta(days=window_days + LOOKBACK_DAYS), data_end)
    df, how = ring_rows(ring, max(probe_end, ring.as_of))
    before = df[df.ts <= ring.as_of]
    el_before = before[before.is_el]
    if el_before.empty:
        raise ValueError(f"{ring.case_id}: no ring card carries {ring.element['label']} {ring.element['value']} in the "
                         f"{LOOKBACK_DAYS} days before {ring.as_of}")
    t_start = el_before.ts.min()
    first_el = el_before.TransactionID.iloc[0]
    w_end = t_start + pd.Timedelta(days=window_days)
    remaining = max((w_end - ring.as_of).total_seconds() / 86400, MIN_HORIZON_DAYS)
    h_end = min(ring.as_of + pd.Timedelta(days=remaining), data_end)
    e = _expr(ring.element["field"])
    habit = duckq.q(f"""select card_id, count(*) n from txn where card_id in (select unnest(?)) and ({e}) = ? and ts < ? group by 1""",
                    list(ring.cards), ring.element["value"], (t_start - pd.Timedelta(days=SETTLE_DAYS)).to_pydatetime())
    habit = dict(zip(habit.card_id, habit.n))
    pop = int(duckq.q(f"select count(distinct card_id) n from txn where ({e}) = ? and ts between ? and ?", ring.element["value"],
                      t_start.to_pydatetime(), ring.as_of.to_pydatetime()).n.iloc[0])
    ring_active = sorted(set(el_before.card_id))
    common = len(ring_active) / max(pop, 1) < COMMON_SHARE
    # how long the ring keeps using one card: 75th percentile of the active cards' element spans so far (at least one hour)
    spans = el_before[el_before.ts >= t_start].groupby("card_id").ts.agg(lambda x: (x.max() - x.min()).total_seconds() / 3600)
    life_h = max(float(spans.quantile(0.75)) if len(spans) else 1.0, 1.0)
    ring_days = max((ring.as_of - t_start).total_seconds() / 86400, 1.0)
    arrival = len(ring_active) / ring_days
    outsiders_sql = f"""select card_id, any_value(customer_id) customer_id, count(*) n, sum(abs(TransactionAmt)) amt from txn
                        where ({e}) = ? and ts between ? and ? and card_id not in (select unnest(?)) group by 1 order by 1"""
    out_before = duckq.q(outsiders_sql, ring.element["value"], t_start.to_pydatetime(), ring.as_of.to_pydatetime(), list(ring.cards))
    out_after = duckq.q(outsiders_sql, ring.element["value"], ring.as_of.to_pydatetime(), h_end.to_pydatetime(), list(ring.cards))
    cust = duckq.q("select card_id, any_value(customer_id) customer_id from txn where card_id in (select unnest(?)) group by 1",
                   list(ring.cards))
    cust = dict(zip(cust.card_id, cust.customer_id))
    cards = []
    for c in ring.cards:
        g = df[df.card_id == c]
        eb = g[(g.ts >= t_start) & (g.ts <= ring.as_of) & g.is_el]
        own_b = g[(g.ts >= t_start) & (g.ts <= ring.as_of) & ~g.is_el]
        after = g[(g.ts > ring.as_of) & (g.ts <= h_end)]
        ea, oa = after[after.is_el], after[~after.is_el]
        realised = round(float(eb.amt.abs().sum()), 2)
        days = max((ring.as_of - eb.ts.min()).total_seconds() / 86400, 1.0) if len(eb) else 0.0
        rate = realised / days if days else 0.0
        forward = round(rate * remaining, 2)
        elapsed_h = (ring.as_of - eb.ts.min()).total_seconds() / 3600 if len(eb) else 0.0
        forward_burst = round(rate * min(remaining, max(0.0, life_h - elapsed_h) / 24), 2) if len(eb) else 0.0
        atyp = eb[eb.strong]
        n_habit = int(habit.get(c, 0))
        if eb.empty:
            cls, why = "not_yet_active", (f"no transaction with the element before {ring.as_of:%Y-%m-%d %H:%M}; named from later data, "
                                          "so there is nothing to block yet")
        elif len(atyp):
            r0 = atyp.iloc[0]
            cls, why = "compromised", (f"carries the element ({len(eb)} txns, ${realised:,.2f}) and {len(atyp)} of them are atypical for the "
                                       f"card (e.g. {int(r0.TransactionID)}: {r0.why})")
        elif n_habit or common:
            cls = "likely_legitimate"
            why = (f"its {len(eb)} element transaction(s) fit its own history (none atypical) and "
                   + (f"it used this element {n_habit} time(s) before the ring started" if n_habit else
                      f"the element is common ({pop} cards carried it in the window, {len(ring_active)} of them ring cards)"))
        else:
            cls, why = "unclear", (f"carries the rare element ({len(eb)} txns, ${realised:,.2f}) but none of those transactions is atypical "
                                   "for the card and it never used the element before")
        cards.append({"card": c, "customer": cust.get(c, ""), "role": "case card" if c == ring.case_card else "connected",
                      "class": cls, "why": why, "n_element": int(len(eb)), "realised": realised,
                      "element_txns": [str(int(x)) for x in eb.TransactionID][:20],
                      "atypical_txns": [str(int(x)) for x in atyp.TransactionID][:20],
                      "first_element_ts": str(eb.ts.min()) if len(eb) else "", "days_active": round(days, 2),
                      "daily_rate": round(rate, 2), "forward": forward, "forward_burst": forward_burst,
                      "own_spend_since_start": round(float(own_b.amt.abs().sum()), 2),
                      "n_own_since_start": int(len(own_b)), "prior_element_uses": n_habit,
                      "hindsight": {"element_after": round(float(ea.amt.abs().sum()), 2), "n_element_after": int(len(ea)),
                                    "element_after_txns": [str(int(x)) for x in ea.TransactionID][:20],
                                    "own_after": round(float(oa.amt.abs().sum()), 2), "n_own_after": int(len(oa))}})
    active = [c for c in cards if c["n_element"]]
    mean_loss = sum(c["realised"] for c in active) / max(len(active), 1)
    ring_level = {"card_life_hours_p75": round(life_h, 1), "active_cards": len(active), "arrival_per_day": round(arrival, 3),
                  "mean_card_loss": round(mean_loss, 2), "projected_new_victims": round(arrival * remaining, 2),
                  "projected_new_exposure": round(arrival * remaining * mean_loss, 2),
                  "outsiders_before": [{"card": r.card_id, "customer": r.customer_id, "n": int(r.n), "amt": round(float(r.amt), 2)}
                                       for r in out_before.itertuples()],
                  "outsiders_after": [{"card": r.card_id, "customer": r.customer_id, "n": int(r.n), "amt": round(float(r.amt), 2)}
                                      for r in out_after.itertuples()],
                  "outsiders_sql": " ".join(outsiders_sql.split())}
    return {"t_start": str(t_start), "first_element_txn": str(int(first_el)), "window_end": str(w_end), "remaining_days": round(remaining, 2),
            "ring_level": ring_level,
            "horizon_end": str(h_end), "data_end": str(data_end), "element_popularity": pop, "element_common": common,
            "ring_active_cards": len(ring_active), "cards": cards, "how": how}


# ================================================================================================ the plan
def _block(c: dict) -> dict:
    route = "L1" if c["realised"] <= L1_MAX else "L2"
    return {"action": "BLOCK_CARD", "card": c["card"], "customer": c["customer"], "route": route,
            "reason": f"R6 containment: compromised ring card, forward exposure ${c['forward']:,.2f} if left open; own exposure "
                      f"${c['realised']:,.2f} {'<=' if route == 'L1' else '>'} $2,500 so {route}; waits for a human approval"}


def score(blocked: set, facts: dict, penalty: float) -> dict:
    """Exposure stopped (projected forward exposure of blocked compromised or unclear cards) minus penalty per legitimate
    customer acted against; residual = what stays open; hindsight = the same with the ring spend that actually followed."""
    cards = facts["cards"]
    risky = [c for c in cards if c["class"] in ("compromised", "unclear")]
    stopped = round(sum(c["forward"] for c in risky if c["card"] in blocked), 2)
    residual = round(sum(c["forward"] for c in risky if c["card"] not in blocked), 2)
    legit = [c for c in cards if c["card"] in blocked and c["class"] == "likely_legitimate"]
    legit_customers = sorted({c["customer"] for c in legit})
    hs_stop = round(sum(c["hindsight"]["element_after"] for c in cards if c["card"] in blocked and c["class"] != "likely_legitimate"), 2)
    hs_res = round(sum(c["hindsight"]["element_after"] for c in cards if c["card"] not in blocked and c["class"] != "likely_legitimate"), 2)
    own_hit = round(sum(c["hindsight"]["own_after"] for c in cards if c["card"] in blocked), 2)
    return {"cards_blocked": len(blocked), "exposure_stopped": stopped, "residual": residual,
            "legit_customers_acted_against": legit_customers, "penalty": round(penalty * len(legit_customers), 2),
            "score": round(stopped - penalty * len(legit_customers), 2),
            "hindsight": {"ring_spend_stopped": hs_stop, "ring_spend_residual": hs_res, "own_spend_interrupted": own_hit,
                          "score": round(hs_stop - penalty * len(legit_customers), 2)}}


def element_rule_score(plan_score: dict, facts: dict, penalty: float) -> dict:
    """The plan plus a rule that declines new transactions carrying the element for the rest of the window
    (DECLINE_TRANSACTION, L1): it also stops cards the ring has not reached yet (projected from the ring's own arrival rate
    times its mean loss per card), and it touches every customer outside the ring who used the element since the ring began
    (each counted as a legitimate customer acted against)."""
    rl = facts["ring_level"]
    outs = sorted({o["customer"] for o in rl["outsiders_before"]})
    stopped = round(plan_score["exposure_stopped"] + rl["projected_new_exposure"], 2)
    cards = facts["cards"]
    hs_stop = round(sum(c["hindsight"]["element_after"] for c in cards if c["class"] != "likely_legitimate"), 2)
    outs_after = sorted({o["customer"] for o in rl["outsiders_after"]})
    legit = sorted(set(plan_score["legit_customers_acted_against"]) | set(outs))
    hs_legit = set(plan_score["legit_customers_acted_against"]) | set(outs_after)
    return {"cards_blocked": plan_score["cards_blocked"], "exposure_stopped": stopped, "residual": 0.0,
            "legit_customers_acted_against": legit, "penalty": round(penalty * len(legit), 2), "score": round(stopped - penalty * len(legit), 2),
            "declines_on_outsiders": outs[:40],
            "hindsight": {"ring_spend_stopped": hs_stop, "ring_spend_residual": 0.0,
                          "own_spend_interrupted": round(plan_score["hindsight"]["own_spend_interrupted"] + sum(o["amt"] for o in rl["outsiders_after"]), 2),
                          "outsider_customers_declined": outs_after[:40], "score": round(hs_stop - penalty * len(hs_legit), 2)}}


def plan(ring: Ring, stop_usd: float = STOP_USD, penalty: float = PENALTY_USD, window_days: float = WINDOW_DAYS) -> dict:
    t0 = time.perf_counter()
    facts = card_facts(ring, window_days)
    cards = facts["cards"]
    cand = sorted([c for c in cards if c["class"] == "compromised"], key=lambda c: (-c["forward"], -c["realised"], c["card"]))
    chosen, steps, stop = [], [], ""
    ordered = sorted([c for c in cards if c["class"] != "not_yet_active"], key=lambda c: (-c["forward"], c["card"]))
    for c in ordered:
        if c["class"] != "compromised":
            stop = stop or (f"next card by exposure, {c['card']} (${c['forward']:,.2f} forward), is {c['class'].replace('_', ' ')}: the "
                            "planner never blocks a card that does not look compromised")
            steps.append({"card": c["card"], "forward": c["forward"], "taken": False, "why": c["class"]})
            continue
        if c["forward"] < stop_usd:
            stop = stop or (f"next compromised card, {c['card']}, would cut ${c['forward']:,.2f}, below the ${stop_usd:,.0f} stop rule")
            steps.append({"card": c["card"], "forward": c["forward"], "taken": False, "why": f"marginal cut below ${stop_usd:,.0f}"})
            continue
        chosen.append(c)
        steps.append({"card": c["card"], "forward": c["forward"], "taken": True, "why": "compromised, largest remaining cut"})
    blocked = {c["card"] for c in chosen}
    actions = [_block(c) for c in chosen]
    for c in cards:
        if c["card"] in blocked:
            continue
        if c["class"] in ("compromised", "unclear"):
            actions.append({"action": "STEP_UP_AUTH", "card": c["card"], "customer": c["customer"], "route": "auto",
                            "reason": "R5 and R6: " + ("compromised but its forward exposure is under the stop rule" if c["class"] == "compromised"
                                                       else "evidence unclear") + "; one-time passcode before further activity (low impact)"})
        actions.append({"action": "MONITOR_CONNECTED_CARDS", "card": c["card"], "customer": c["customer"], "route": "auto",
                         "reason": f"R6: shares {ring.element['label']} {ring.element['value']}; monitoring has no customer impact"})
    active = {c["card"] for c in cards if c["class"] != "not_yet_active"}
    alts = {"monitor_all": score(set(), facts, penalty), "block_all": score(active, facts, penalty), "plan": score(blocked, facts, penalty)}
    alts["plan_plus_element_rule"] = element_rule_score(alts["plan"], facts, penalty)
    calib = {"projected_window": round(sum(c["forward"] for c in cards), 2), "projected_burst": round(sum(c["forward_burst"] for c in cards), 2),
             "actual_after_on_active_cards": round(sum(c["hindsight"]["element_after"] for c in cards if c["class"] != "not_yet_active"), 2),
             "actual_after_on_later_cards": round(sum(c["hindsight"]["element_after"] for c in cards if c["class"] == "not_yet_active"), 2),
             "projected_new_exposure": facts["ring_level"]["projected_new_exposure"]}
    total_forward = round(sum(c["forward"] for c in cards if c["class"] in ("compromised", "unclear")), 2)
    legit = [{"customer": c["customer"], "card": c["card"], "why": c["why"],
              "plan_action": "BLOCK_CARD" if c["card"] in blocked else "MONITOR_CONNECTED_CARDS only (no customer impact)"}
             for c in cards if c["class"] == "likely_legitimate"]
    staged = ring.case_verdict != "fraud"
    from kavach import duckq
    dv = duckq.dataset_version().get("version", "unknown")
    out = {"case_id": ring.case_id, "case_card": ring.case_card, "case_verdict": ring.case_verdict, "case_final_actions": ring.case_actions,
           "as_of": str(ring.as_of), "as_of_label": ring.as_of_label, "element": ring.element, "n_ring_cards": len(ring.cards),
           "parameters": {"stop_usd": stop_usd, "penalty_usd": penalty, "window_days": window_days, "min_horizon_days": MIN_HORIZON_DAYS,
                          "lookback_days": LOOKBACK_DAYS, "l1_max": L1_MAX},
           "facts": facts, "blocked": sorted(blocked), "greedy_steps": steps, "stop_reason": stop or "every compromised card was taken",
           "actions": actions, "total_forward_at_risk": total_forward, "legitimate_customers_touched": legit,
           "alternatives": alts, "calibration": calib, "never": "BLOCK_ALL_CARDS: R10 is not met by a ring (no customer has two cards with confirmed fraud or "
                                          "confirmed compromised credentials)",
           "staged": staged, "staging_note": ("the case verdict is not fraud, so every block waits for the analyst escalation (R8) and the L1/L2 "
                                              "approval; only the auto actions (monitoring, step-up) may run now") if staged else
                                             "blocks are recommended with their routes and wait for the L1/L2 approval",
           "run_at": _now(), "dataset_version": dv, "seconds": round(time.perf_counter() - t0, 2)}
    out["claims"] = _claims(out, dv)
    return out


def _claims(p: dict, dv: str) -> list[dict]:
    when = {"ts": p["run_at"], "dataset_version": dv}
    how = {"function": f"python -m kavach.containment {p['case_id']} --as-of {p['as_of_label']} --verify", "query": p["facts"]["how"]}
    cl = []
    alt = p["alternatives"]["plan"]
    cl.append({"what": f"Blocking {len(p['blocked'])} of {p['n_ring_cards']} ring cards cuts ${alt['exposure_stopped']:,.2f} of projected "
                       f"exposure and leaves ${alt['residual']:,.2f}, acting against {len(alt['legit_customers_acted_against'])} legitimate customers",
               "evidence": {"card_ids": p["blocked"], "customer_ids": sorted({a['customer'] for a in p['actions'] if a['action'] == 'BLOCK_CARD'}),
                            "txn_ids": [t for c in p["facts"]["cards"] if c["card"] in p["blocked"] for t in c["element_txns"]][:60]},
               "how": how, "when": when})
    for c in p["facts"]["cards"]:
        cl.append({"what": f"Card {c['card']} (customer {c['customer']}) is {c['class'].replace('_', ' ')}: realised ring exposure "
                           f"${c['realised']:,.2f} over {c['n_element']} element transaction(s), forward ${c['forward']:,.2f}",
                   "evidence": {"card_ids": [c["card"]], "customer_ids": [c["customer"]], "txn_ids": c["element_txns"]},
                   "how": how, "when": when})
    for lg in p["legitimate_customers_touched"]:
        cl.append({"what": f"Customer {lg['customer']} (card {lg['card']}) is likely legitimate collateral: {lg['why']}; plan action: {lg['plan_action']}",
                   "evidence": {"card_ids": [lg["card"]], "customer_ids": [lg["customer"]]}, "how": how, "when": when})
    return cl


def verify(p: dict) -> dict:
    """Re-run a stored plan from the index and compare every number and id."""
    ring = Ring(p["case_id"], p["case_card"], [c["card"] for c in p["facts"]["cards"]], p["element"], pd.Timestamp(p["as_of"]),
                p["as_of_label"], p["case_verdict"], p["case_final_actions"])
    q = plan(ring, p["parameters"]["stop_usd"], p["parameters"]["penalty_usd"], p["parameters"]["window_days"])
    keys = ("realised", "forward", "class", "element_txns", "customer")
    diff = [(a["card"], k, a[k], b[k]) for a, b in zip(p["facts"]["cards"], q["facts"]["cards"]) for k in keys if a[k] != b[k]]
    diff += [("plan", "blocked", p["blocked"], q["blocked"])] if p["blocked"] != q["blocked"] else []
    return {"case_id": p["case_id"], "as_of": p["as_of"], "identical": not diff, "differences": diff[:20], "dataset_version": q["dataset_version"],
            "at": _now()}


# ================================================================================================ sources and output
def answers(source: str = "auto") -> dict:
    """case_id -> (answer, signals): cases/<id>.json when it lists connected cards (source auto or cases), else the agent's dry
    run (kavach.rules.agent_answers: run_case with KAVACH_NO_LLM=1, KAVACH_RECORD=0)."""
    out = {}
    if source in ("auto", "cases"):
        for p in sorted(CASES_DIR.glob("*.json")):
            try:
                a = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if a.get("case", {}).get("connected_card_ids"):
                out[a["case_id"]] = (a, [], f"{_rel(p)}")
    if source in ("auto", "dryrun") and not out:
        from kavach import rules
        ag = rules.agent_answers()
        for cid, ent in ag["cases"].items():
            a = ent.get("answer")
            if a and a["case"].get("connected_card_ids"):
                out[cid] = (a, ent.get("signals", []), f"agent dry run {ag['run_at']} ({ag['how']})")
    return out


def _md_plan(p: dict, src: str) -> list[str]:
    f = p["facts"]
    L = [f"### Plan at {p['as_of_label']} ({p['as_of']})", "",
         f"Ring window: first element transaction {f['first_element_txn']} at {f['t_start']}; window end {f['window_end']}; remaining "
         f"{f['remaining_days']} days (minimum {p['parameters']['min_horizon_days']}). The element was carried by {f['element_popularity']} "
         f"card(s) in the bank between the ring's start and the decision ({f['ring_active_cards']} of them ring cards, "
         f"{'common' if f['element_common'] else 'rare'}).", "",
         "| card | customer | role | class | element txns | realised | daily rate | forward if open | own spend since start | why |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for c in sorted(f["cards"], key=lambda c: (-c["forward"], c["card"])):
        L.append(f"| {c['card']} | {c['customer']} | {c['role']} | {c['class'].replace('_', ' ')} | {', '.join(c['element_txns'][:4])}"
                 f"{' ...' if len(c['element_txns']) > 4 else ''} | ${c['realised']:,.2f} | ${c['daily_rate']:,.2f} | ${c['forward']:,.2f} | "
                 f"${c['own_spend_since_start']:,.2f} ({c['n_own_since_start']}) | {c['why']} |")
    steps = ", ".join(f"{'+' if s['taken'] else '-'}{s['card']} (${s['forward']:,.0f})" for s in p["greedy_steps"][:20])
    L += ["", f"Greedy order (+ blocked, - not blocked): {steps}. Stop: {p['stop_reason']}.", "",
          "Recommended (blocks wait for the stated approval route; " + p["staging_note"] + "):", ""]
    for a in p["actions"]:
        if a["action"] != "MONITOR_CONNECTED_CARDS":
            L.append(f"- `{a['action']}` {a['card']} (customer {a['customer']}), route `{a['route']}`: {a['reason']}.")
    mon = [a["card"] for a in p["actions"] if a["action"] == "MONITOR_CONNECTED_CARDS"]
    L.append(f"- `MONITOR_CONNECTED_CARDS` (auto) on {len(mon)} card(s): {', '.join(mon)}.")
    L.append(f"- Never `BLOCK_ALL_CARDS`: {p['never']}.")
    L += ["", "Legitimate customers this plan touches: " + ("; ".join(f"customer {x['customer']} (card {x['card']}): {x['plan_action']}, because {x['why']}"
                                                                     for x in p["legitimate_customers_touched"]) or "none classified likely legitimate") + ".", "",
          f"| alternative | cards blocked | exposure stopped (projected) | residual | legitimate customers acted against | score (stopped - ${p['parameters']['penalty_usd']:,.0f} each) | hindsight: ring spend stopped | hindsight: residual | hindsight: own spend interrupted | hindsight score |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    names = {"monitor_all": "block nothing (monitor all)", "block_all": "block every active ring card", "plan": "the planner's plan",
             "plan_plus_element_rule": "the plan plus declining new transactions carrying the element (L1)"}
    for k in ("monitor_all", "block_all", "plan", "plan_plus_element_rule"):
        s = p["alternatives"][k]
        L.append(f"| {names[k]} | {s['cards_blocked']} | ${s['exposure_stopped']:,.2f} | ${s['residual']:,.2f} | "
                 f"{len(s['legit_customers_acted_against'])}{': ' + ', '.join(s['legit_customers_acted_against']) if s['legit_customers_acted_against'] else ''} | "
                 f"${s['score']:,.2f} | ${s['hindsight']['ring_spend_stopped']:,.2f} | ${s['hindsight']['ring_spend_residual']:,.2f} | "
                 f"${s['hindsight']['own_spend_interrupted']:,.2f} | ${s['hindsight']['score']:,.2f} |")
    cb, rl = p["calibration"], f["ring_level"]
    outs = rl["outsiders_before"]
    L += ["", f"Projection check: the window projection (daily rate times remaining window) puts ${cb['projected_window']:,.2f} at risk on the "
          f"active cards; a burst-aware projection (the ring uses one card for {rl['card_life_hours_p75']} hours at the 75th percentile, so a "
          f"card past that has little left) puts ${cb['projected_burst']:,.2f}; the index shows ${cb['actual_after_on_active_cards']:,.2f} of "
          f"element spend actually followed on those cards, and ${cb['actual_after_on_later_cards']:,.2f} on ring cards the ring had not "
          f"reached yet (projected from its arrival rate of {rl['arrival_per_day']} cards a day: ${cb['projected_new_exposure']:,.2f}). "
          f"Outside the ring, {len(outs)} card(s) used the element between the ring's start and the decision"
          + (" (" + ", ".join(o["customer"] + " / " + o["card"] for o in outs[:12]) + (" ..." if len(outs) > 12 else "") + ")" if outs else "")
          + "; an element rule would decline their next purchases."]
    L += ["", f"Attested: {len(p['claims'])} claims (what, card, customer and transaction ids, how = `{p['claims'][0]['how']['function']}` plus the "
          f"query below, when = {p['run_at']} on dataset `{p['dataset_version']}`). Query: `{f['how']['sql']}` with parameters "
          f"{f['how']['params'][0]!r}, {len(f['how']['params'][1])} ring cards, {f['how']['params'][2]} to {f['how']['params'][3]}; flags: "
          f"{f['how']['flags']}.", ""]
    return L


def write_doc(plans: list[tuple[dict, dict | None, str]]) -> None:
    from kavach import duckq
    dv = duckq.dataset_version().get("version", "unknown")
    L = ["# Containment planner (challenge 7)", "",
         f"Generated by `python -m kavach.containment all` (kavach/containment.py) at {_now()} on dataset version `{dv}`. One plan per ring "
         "(an answer with connected cards). Raw plans: `logs/containment/<case>.json`; re-run and compare any plan with "
         "`python -m kavach.containment <case> --verify`.", "",
         "How a plan is made: per ring card, the transactions carrying the shared element since the ring's first element transaction "
         "are its realised exposure; its average daily ring spend times the remaining window is the forward exposure a block would "
         "cut; it is compromised when it carries the element and at least one element transaction is atypical for its own history, "
         "likely legitimate when its element transactions fit its profile and the element is habitual for it or common, unclear "
         "otherwise. The planner takes compromised cards greedily by forward exposure and stops when the next cut is under "
         f"${STOP_USD:,.0f} (R5's own block threshold) or the next card is not compromised. Alternatives are scored as projected "
         f"exposure stopped minus ${PENALTY_USD:,.0f} (the section 3a materiality line) per legitimate customer acted against; a "
         "hindsight column re-scores them with the ring spend that really followed the decision (the planner never sees it). "
         "Routes: BLOCK_CARD L1 up to $2,500 of the card's own exposure, L2 above; BLOCK_ALL_CARDS never (R10).", ""]
    for p_open, p_end, src in plans:
        el = p_open["element"]
        L += [f"## {p_open['case_id']}: ring of {p_open['n_ring_cards']} cards on {el['label']} `{el['value']}`", "",
              f"Source of the ring: {src}. Case card {p_open['case_card']}, verdict {p_open['case_verdict']}, final actions "
              f"{', '.join(p_open['case_final_actions'])}. Element: {el['source']}.", ""]
        L += _md_plan(p_open, src)
        if p_end:
            L += _md_plan(p_end, src)
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text("\n".join(L) + "\n", encoding="utf-8")


def _print(p: dict) -> None:
    print(f"{p['case_id']} as of {p['as_of']} ({p['as_of_label']}): ring {p['n_ring_cards']} cards on {p['element']['label']} {p['element']['value']}")
    for c in sorted(p["facts"]["cards"], key=lambda c: -c["forward"]):
        print(f"  {c['card']:10s} {c['customer']:7s} {c['class']:18s} n={c['n_element']:2d} realised ${c['realised']:>9,.2f} "
              f"forward ${c['forward']:>9,.2f} burst ${c['forward_burst']:>9,.2f} after(el) ${c['hindsight']['element_after']:>9,.2f}")
    print(f"  calibration {p['calibration']}; outsiders before {len(p['facts']['ring_level']['outsiders_before'])}, after "
          f"{len(p['facts']['ring_level']['outsiders_after'])}")
    print(f"  blocked {p['blocked']}; stop: {p['stop_reason']}")
    for k, s in p["alternatives"].items():
        print(f"  {k:22s} stopped ${s['exposure_stopped']:>10,.2f} residual ${s['residual']:>10,.2f} legit {len(s['legit_customers_acted_against'])} "
              f"score ${s['score']:>10,.2f} | hindsight stopped ${s['hindsight']['ring_spend_stopped']:>9,.2f} score ${s['hindsight']['score']:>10,.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.containment")
    ap.add_argument("case_id", nargs="?", default="all")
    ap.add_argument("--as-of", default="opened", help="opened (default), end, or a timestamp")
    ap.add_argument("--from", dest="source", default="auto", choices=("auto", "cases", "dryrun"))
    ap.add_argument("--verify", action="store_true", help="re-run the stored plan and compare")
    ap.add_argument("--stop", type=float, default=STOP_USD)
    ap.add_argument("--penalty", type=float, default=PENALTY_USD)
    a = ap.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if a.verify:
        ids = [a.case_id] if a.case_id != "all" else [p.stem.split("@")[0] for p in OUT_DIR.glob("*.json")]
        ok = True
        for cid in sorted(set(ids)):
            for p in sorted(OUT_DIR.glob(f"{cid}@*.json")):
                r = verify(json.loads(p.read_text(encoding="utf-8")))
                ok &= r["identical"]
                print(json.dumps(r, default=str))
        return 0 if ok else 1
    src = answers(a.source)
    ids = sorted(src) if a.case_id == "all" else [a.case_id]
    if not ids:
        print("no answer lists connected cards; nothing to plan")
        return 0
    plans = []
    for cid in ids:
        if cid not in src:
            print(f"{cid}: no connected cards in {a.source} answers; nothing to plan")
            continue
        ans, sigs, where = src[cid]
        views = [a.as_of] if a.case_id != "all" else ["opened", "end"]
        done = []
        for v in views:
            ring = ring_from_answer(ans, sigs, as_of=v)
            p = plan(ring, a.stop, a.penalty)
            p["source"] = where
            (OUT_DIR / f"{cid}@{v.replace(':', '').replace(' ', 'T')}.json").write_text(json.dumps(p, indent=1, default=str), encoding="utf-8")
            _print(p)
            done.append(p)
        plans.append((done[0], done[1] if len(done) > 1 else None, where))
    if a.case_id == "all":
        write_doc(plans)
        print(f"wrote {_rel(DOC)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
