"""Evidence model: log-odds over independent evidence groups, verdict, and the section 6 stop rule."""
import math
from dataclasses import dataclass, field

GROUPS = ("sequence", "device", "region", "identity_flags", "network", "history", "customer", "score")
P_MIN, P_MAX = 0.03, 0.97

# Prior log-odds by trigger: even odds. For model and analyst alerts the calibrated score signal
# carries the base rate for that kind of transaction; customer reports carry the customer signal.
PRIOR = {"risk_score": 0.0, "customer_report": 0.0, "analyst_request": 0.0}


@dataclass
class Signal:
    name: str
    group: str
    llr: float                    # natural-log likelihood ratio; > 0 favours fraud, < 0 favours legitimate
    claim: str
    entity_ids: list = field(default_factory=list)
    ref: str = ""
    source: str = "graph"

    def __post_init__(self):
        assert self.group in GROUPS, self.group


@dataclass
class Assessment:
    p: float
    verdict: str
    fraud_groups: list
    legit_groups: list
    by_group: dict

    @property
    def n_groups(self) -> int:
        return len(self.fraud_groups) if self.verdict != "legitimate" else len(self.legit_groups)


def assess(signals: list[Signal], prior_logodds: float = 0.0) -> Assessment:
    """Strongest fraud signal and strongest legit signal per group (no double counting inside a group)."""
    by_group: dict[str, float] = {}
    for g in GROUPS:
        pos = max([s.llr for s in signals if s.group == g and s.llr > 0], default=0.0)
        neg = min([s.llr for s in signals if s.group == g and s.llr < 0], default=0.0)
        if pos or neg:
            by_group[g] = pos + neg
    z = prior_logodds + sum(by_group.values())
    p = min(P_MAX, max(P_MIN, 1 / (1 + math.exp(-z))))
    fraud_groups = [g for g, v in by_group.items() if v >= 0.4 and g != "score"]
    legit_groups = [g for g, v in by_group.items() if v <= -0.4 and g != "score"]
    if p >= 0.70 and len(fraud_groups) >= 2:
        verdict = "fraud"
    elif p <= 0.30 and (legit_groups or by_group.get("score", 0) <= -0.4):
        verdict = "legitimate"
    else:
        verdict = "uncertain"
    return Assessment(round(p, 2), verdict, fraud_groups, legit_groups, by_group)


def settled(a: Assessment) -> bool:
    """Section 6: stop when p >= 0.85 or <= 0.15 with at least two independent groups behind it."""
    if a.p >= 0.85:
        return len(a.fraud_groups) >= 2
    if a.p <= 0.15:
        # a strongly exculpatory calibrated score counts as one piece, never as the only one
        return len(a.legit_groups) >= 2 or (len(a.legit_groups) >= 1 and a.by_group.get("score", 0) <= -0.4)
    return False
