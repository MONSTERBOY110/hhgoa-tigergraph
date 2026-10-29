"""Detectors: general rules over a CaseContext that emit Signals and Findings.

Nothing here knows about case ids or specific transactions. Thresholds come from the
closed cases (kavach/weights.json) or are stated with the reason next to them.
"""
from dataclasses import dataclass, field

from kavach.assess import Signal


@dataclass
class Findings:
    """Structured facts the episode builder and policy need, beyond the signals."""
    pattern_votes: dict = field(default_factory=dict)       # pattern -> strength
    episode: dict = field(default_factory=dict)             # source -> list of txn ids (ints)
    connected_cards: dict = field(default_factory=dict)     # card_id -> reason
    connected_devices: list = field(default_factory=list)
    shared_element: str = ""
    coordinated_undocumented: bool = False
    undocumented_kind: str = ""
    card_testing: bool = False
    cleared_over_100: bool = False
    recurring: bool = False
    linked_fraud: bool = False
    conflicting: bool = False
    similar_cases: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)

    def vote(self, pattern: str, w: float) -> None:
        self.pattern_votes[pattern] = max(self.pattern_votes.get(pattern, 0.0), w)


def run_all(ctx) -> tuple[list[Signal], Findings]:
    from kavach.detectors import history, network, onecard

    fnd = Findings()
    sigs: list[Signal] = []
    for mod in (onecard, network, history):
        sigs += mod.detect(ctx, fnd)
    # detectors the agent discovered itself (kavach.grow), loaded only after an L2 approval; KAVACH_GROWN=0 disables them
    from kavach.grow import registry
    grown = registry.signals_for(ctx)
    for s in grown:
        fnd.notes.setdefault("grown_detectors", []).append(s.name)
    _grown_rings(ctx, fnd, grown, registry)
    sigs += grown
    if fnd.connected_devices or fnd.coordinated_undocumented or fnd.notes.get("related_cards"):
        # a ring reuses amounts and habits that fit its victims; a matching profile is part of the method, not an alibi
        sigs = [s for s in sigs if s.name not in ("amount_seen_before", "account_continuity", "profile_fit", "product_familiar",
                                                  "amount_within_max", "email_familiar", "hour_familiar", "proxy_seen_before")]
    return sigs, fnd


CROSS_CARD_FAMILIES = ("fanin", "shared_entity", "new_device_proxy")
ENTITY_LABEL = {"R_emaildomain": "recipient email domain", "P_emaildomain": "purchaser email domain", "device_key": "device profile",
                "addr1": "billing region", "holder_key": "account"}


def _grown_rings(ctx, fnd, grown, registry) -> None:
    """An approved grown detector of a cross-card family is, by construction, abuse repeated across customers that fits
    none of the five documented patterns: R9. The other cards it names become connected cards (the bank lists connected
    cards on undocumented ring cases), the shared value becomes the shared element, and the case card's own transactions
    carrying that value form the episode."""
    import re
    specs = {f"grown:{x['name']}": x for x in registry.load_approved(verbose=False)}
    for s in grown:
        spec = specs.get(s.name)
        if not spec or spec.get("family") not in CROSS_CARD_FAMILIES or s.llr <= 0:
            continue
        others = [c for c in s.entity_ids if re.fullmatch(r"C\d{5}-K\d+", str(c)) and c != ctx.card_id]
        if len(others) < 2:
            continue
        ent = spec.get("params", {}).get("entity", "device_key")
        val = ctx.f.get(ent)
        label = ENTITY_LABEL.get(ent, ent)
        fnd.coordinated_undocumented = True
        fnd.undocumented_kind = fnd.undocumented_kind or s.name
        fnd.vote("undocumented", s.llr)
        for c in others:
            fnd.connected_cards.setdefault(c, f"paid the same {label} {val} in the spike found by {spec['name']}")
        fnd.shared_element = fnd.shared_element or f"{label} {val}"
        fnd.notes["grown_spec"] = {"name": spec["name"], "rule": spec.get("rule", ""), "description": spec.get("description", ""),
                                   "approval": (spec.get("approval") or {}).get("id", ""), "entity": ent, "value": val}
        if val is not None and ent in ctx.h.columns:
            win = ctx.h[(ctx.h[ent] == val) & (ctx.h.ts <= ctx.opened) & ((ctx.h.ts - ctx.t0).abs() <= __import__("pandas").Timedelta(days=30))]
            if len(win):
                fnd.episode["grown_ring"] = list(win.TransactionID)
