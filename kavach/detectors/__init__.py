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
    if fnd.connected_devices or fnd.coordinated_undocumented:
        # a ring reuses amounts that fit its victims; a matching amount is part of the method, not an alibi
        sigs = [s for s in sigs if s.name not in ("amount_seen_before", "account_continuity")]
    return sigs, fnd
