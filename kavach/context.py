"""GATHER: everything the detectors need about one case, fetched through the tool registry."""
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from kavach.cardfeat import card_features, empty_device
from kavach.tools import ToolRegistry

RARE_DEVICE = 100         # device profiles on more cards than this are generic (e.g. 'iOS Device') and link nothing
NETWORK_DAYS = 7


@dataclass
class CaseContext:
    case: dict
    tools: ToolRegistry
    f: dict = field(default_factory=dict)          # flagged txn with card-relative features
    h: pd.DataFrame = None                          # card history with features
    t0: pd.Timestamp = None
    opened: pd.Timestamp = None
    cust_cards: pd.DataFrame = None
    device: str | None = None
    device_pop: int = 0
    device_cards: pd.DataFrame = None               # cards on the flagged device in the window
    device_rows: pd.DataFrame = None                # txns on the flagged device in the window
    region_cards: pd.DataFrame = None
    closed_hits: pd.DataFrame = None
    holder_hist: pd.DataFrame = None
    refs: dict = field(default_factory=dict)

    @property
    def card_id(self) -> str:
        return self.case["card_id"]

    @property
    def customer_id(self) -> str:
        return self.case["customer_id"]

    @property
    def trigger(self) -> str:
        return self.case["trigger_type"]

    def window(self, hours_before=48, hours_after=48) -> pd.DataFrame:
        h = self.h
        return h[(h.ts >= self.t0 - timedelta(hours=hours_before)) & (h.ts <= self.t0 + timedelta(hours=hours_after))]


def gather(case: dict, tools: ToolRegistry) -> CaseContext:
    ctx = CaseContext(case=case, tools=tools)
    tid = int(case["flagged_txn_id"])
    tools.step = 1
    raw = tools.call("card_history", card_id=case["card_id"])
    ctx.refs["history"] = tools.ref("card_history", card_id=case["card_id"])
    ctx.h = card_features(raw)
    row = ctx.h[ctx.h.TransactionID == tid]
    if row.empty:
        raise ValueError(f"flagged txn {tid} not on card {case['card_id']}")
    ctx.f = row.iloc[0].to_dict()
    ctx.t0 = pd.Timestamp(ctx.f["ts"])
    ctx.opened = pd.Timestamp(case["opened_at"])

    tools.step = 2
    ctx.cust_cards = tools.call("customer_cards", customer_id=case["customer_id"])
    ctx.device = None if empty_device(ctx.f.get("device_key")) else ctx.f["device_key"]
    if ctx.device:
        ctx.device_pop = tools.call("device_popularity", device_key=ctx.device)
        if ctx.device_pop <= RARE_DEVICE:
            ctx.device_cards = tools.call("device_neighbors", device_key=ctx.device, t0=ctx.t0, days=30)
            ctx.device_rows = tools.call("device_txns", device_key=ctx.device, t0=ctx.t0, days=30)
            ctx.refs["device"] = tools.ref("device_neighbors", device_key=ctx.device, days=30)
    if pd.notna(ctx.f.get("addr1")) and ctx.f.get("channel") == "in_person":
        ctx.region_cards = tools.call("region_activity", addr1=int(ctx.f["addr1"]), t0=ctx.t0, days=3)
        ctx.refs["region"] = tools.ref("region_activity", addr1=int(ctx.f["addr1"]), days=3)

    tools.step = 3
    win = ctx.window(72, 24)
    ctx.closed_hits = tools.call("closed_cases_for", card_ids=[case["card_id"]], customer_ids=[],
                                 txn_ids=list(win.TransactionID), device_keys=[ctx.device] if ctx.device and ctx.device_pop <= RARE_DEVICE else [])
    ctx.refs["closed"] = tools.ref("closed_cases_for", card_id=case["card_id"])
    hks = sorted({k for k in ctx.window(24 * 7, 24).holder_key if isinstance(k, str) and "||" not in k})
    ctx.holder_hist = tools.call("holder_fraud_history", holder_keys=hks, before=ctx.opened)
    ctx.refs["holder"] = tools.ref("holder_fraud_history", card_id=case["card_id"])
    return ctx
