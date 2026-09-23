"""Fraud Policy v1.0 as code: actions, approval routes, rules R1-R10, section 3a and section 6.

Pure functions over a PolicyInput. No data access, no LLM. Every recommended action
carries a reason that cites the rule it comes from.
"""
from dataclasses import dataclass, field

from kavach.answer import route_for

ORDER = [
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "STEP_UP_AUTH", "VERIFY_WITH_CUSTOMER", "BLOCK_CARD",
    "BLOCK_ALL_CARDS", "CREATE_CASE", "FILE_REPORT", "GENERATE_REPORT", "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
]

SAR_EXPOSURE = 1000.0
ESCALATE_EXPOSURE = 500.0
CASE_P = 0.30
BLOCK_P = 0.70
STRONG_P = 0.85
CLEAR_P = 0.15


@dataclass
class PolicyInput:
    p: float
    verdict: str                      # fraud | legitimate | uncertain
    pattern: str
    exposure: float
    trigger: str                      # risk_score | customer_report | analyst_request
    n_groups: int = 0                 # independent evidence groups supporting the verdict
    single_signal: bool = False       # case rests on one signal (R1)
    reply: str | None = None          # None | deny | confirm | no_reply | recognise_recurring
    card_testing: bool = False
    cleared_over_100: bool = False    # R5: a purchase over $100 already cleared
    shared_element: str = ""          # R6: named shared device/region/email, "" if none
    connected_cards: list = field(default_factory=list)
    linked_fraud: bool = False        # connects to another card's / customer's fraud (R2, 3a)
    recurring: bool = False           # R7: disputed charge matches own recurring pattern
    coordinated_undocumented: bool = False  # R9
    n_cards_confirmed_fraud: int = 0  # R10
    credentials_compromised: bool = False   # R10
    conflicting: bool = False         # R8


def _nba(action: str, reason: str, exposure: float) -> dict:
    return {"action": action, "route": route_for(action, exposure), "reason": reason}


def order(actions: list[dict]) -> list[dict]:
    seen, out = set(), []
    for a in sorted(actions, key=lambda x: ORDER.index(x["action"])):
        if a["action"] not in seen:
            seen.add(a["action"])
            out.append(a)
    return out


def sar_required(x: PolicyInput) -> tuple[bool, str]:
    """Section 3a: fraud confirmed or strongly suspected AND one aggravating condition."""
    suspected = x.verdict == "fraud" or x.p >= BLOCK_P
    if not suspected:
        return False, "§3a: fraud is not confirmed or strongly suspected, so no report; the case record is sufficient"
    if x.coordinated_undocumented:
        return True, "R9 and §3a: coordinated activity fitting no documented pattern"
    if x.shared_element:
        return True, f"R6 and §3a: activity connects to a shared origin ({x.shared_element})"
    if x.linked_fraud:
        return True, "R2 and §3a: activity connects to another card's or customer's fraud"
    if x.exposure > SAR_EXPOSURE:
        return True, f"R2 and §3a: exposure ${x.exposure:,.2f} exceeds $1,000"
    return False, (f"§3a: fraud confirmed but exposure ${x.exposure:,.2f} is under $1,000 with no shared device, "
                   "region cluster or link to another customer's fraud, so a case without a report")


def _fraud_actions(x: PolicyInput, why: str) -> list[dict]:
    """Containment + case + report for fraud that is established (by evidence or by a denial)."""
    e = x.exposure
    acts = []
    if x.card_testing and not x.cleared_over_100:
        acts += [_nba("DECLINE_TRANSACTION", "R5: card-testing sequence, decline pending authorizations", e),
                 _nba("STEP_UP_AUTH", "R5: require one-time passcode before further activity", e)]
    else:
        rule = "R5" if x.card_testing else why
        acts.append(_nba("BLOCK_CARD", f"{rule}: block and reissue; exposure ${e:,.2f} "
                                       f"{'is under' if e <= 2500 else 'exceeds'} $2,500", e))
    if x.n_cards_confirmed_fraud >= 2 or x.credentials_compromised:
        acts.append(_nba("BLOCK_ALL_CARDS", "R10: "
                         + ("two or more of the customer's cards show confirmed fraud" if x.n_cards_confirmed_fraud >= 2
                            else "customer credentials confirmed compromised"), e))
    acts.append(_nba("CREATE_CASE", f"{why} and §3a: open the internal fraud case with the evidence attached", e))
    file, reason = sar_required(x)
    if file:
        acts.append(_nba("FILE_REPORT", reason, e))
    if x.connected_cards:
        acts.append(_nba("MONITOR_CONNECTED_CARDS",
                         f"R6: {len(x.connected_cards)} other card(s) share "
                         f"{x.shared_element or 'the compromise'}; place them under monitoring", e))
    if x.coordinated_undocumented:
        acts.append(_nba("ESCALATE_TO_ANALYST", "R9: undocumented coordinated pattern, hand to an analyst", e))
    return acts


def _legit_actions(x: PolicyInput, why: str) -> list[dict]:
    e = x.exposure
    acts = []
    if x.trigger == "risk_score":
        acts.append(_nba("ALLOW_TRANSACTION", f"{why}: the flagged transaction is consistent with the cardholder", e))
    if x.trigger == "customer_report" or x.reply is not None:
        acts.append(_nba("CREATE_CASE", "§3a: a dispute or evidence request always opens a case; closed as legitimate", e))
    acts.append(_nba("CLOSE_NO_FRAUD", f"{why}: close the alert as legitimate", e))
    return acts


def initial_actions(x: PolicyInput, will_request: bool) -> list[dict]:
    """Recommendation before any requested evidence comes back."""
    e = x.exposure
    if x.recurring and x.trigger == "customer_report":
        return order([
            _nba("CREATE_CASE", "R7 and §3a: customer dispute opens a case", e),
            _nba("VERIFY_WITH_CUSTOMER", "R7: disputed charge matches the cardholder's own recurring pattern; verify, do not block", e),
            _nba("WARN_CUSTOMER", "R7: remind the customer of the recurring charge", e),
        ])
    if not will_request:
        if x.verdict == "legitimate":
            return order(_legit_actions(x, "§6 and R3" if x.reply == "confirm" else "§6"))
        if x.verdict == "fraud":
            why = "R2" if x.trigger == "customer_report" else ("R9" if x.coordinated_undocumented else "R6" if x.shared_element else "R5" if x.card_testing else "§6")
            return order(_fraud_actions(x, why))
        # uncertain without a request
        acts = [_nba("MONITOR_CARD", "R8: evidence is balanced; keep the card under monitoring", e)]
        if x.p >= CASE_P or x.trigger == "customer_report":
            acts.append(_nba("CREATE_CASE", "§3a: fraud probability at or above 0.30 or a dispute opens a case", e))
        if x.exposure > ESCALATE_EXPOSURE or x.conflicting:
            acts.append(_nba("ESCALATE_TO_ANALYST", "R8: uncertain verdict with exposure over $500 or conflicting evidence", e))
        return order(acts)

    # evidence will be requested: R1 verify first, never block yet on a weak case
    acts = []
    if x.card_testing:
        acts += [_nba("DECLINE_TRANSACTION", "R5: testing sequence observed, decline pending authorizations", e),
                 _nba("STEP_UP_AUTH", "R5: require one-time passcode before further activity", e)]
        if x.cleared_over_100 and x.p >= BLOCK_P and not x.single_signal:
            acts.append(_nba("BLOCK_CARD", "R5: a purchase over $100 has already cleared", e))
    if x.trigger == "customer_report":
        acts.append(_nba("VERIFY_WITH_CUSTOMER", "R1 and R2: confirm which of the related transactions the customer disputes before blocking", e))
    else:
        why = (f"fraud probability {x.p:.2f} is below 0.70" if x.p < BLOCK_P else
               f"the case rests on a single signal (probability {x.p:.2f})" if x.single_signal else
               f"probability {x.p:.2f} is not yet backed by two independent evidence groups")
        acts.append(_nba("VERIFY_WITH_CUSTOMER", f"R1 and §5: {why}; verify with the cardholder before any block", e))
    acts.append(_nba("CREATE_CASE", "§3a: requesting evidence opens a case", e))
    if x.shared_element and x.connected_cards:
        acts.append(_nba("MONITOR_CONNECTED_CARDS", f"R6: cards sharing {x.shared_element} go under monitoring while we verify", e))
    return order(acts)


def final_actions(x: PolicyInput) -> list[dict]:
    """Recommendation after the (simulated) reply in x.reply."""
    e = x.exposure
    if x.reply == "recognise_recurring":
        return order([
            _nba("CREATE_CASE", "R7 and §3a: dispute case recorded and closed as legitimate", e),
            _nba("WARN_CUSTOMER", "R7: recurring charge reminder sent", e),
            _nba("CLOSE_NO_FRAUD", "R3 and R7: customer recognises the recurring charge", e),
        ])
    if x.reply == "confirm":
        return order(_legit_actions(x, "R3"))
    if x.reply == "deny":
        return order(_fraud_actions(x, "R2"))
    if x.reply == "no_reply":
        acts = [_nba("MONITOR_CARD", "R4: no reply within 24 hours, raise monitoring for 72 hours", e),
                _nba("DECLINE_TRANSACTION", "R4: decline pending authorizations while unverified", e),
                _nba("CREATE_CASE", "§3a: the evidence request opened a case", e)]
        if e > ESCALATE_EXPOSURE or x.conflicting:
            acts.append(_nba("ESCALATE_TO_ANALYST", "R4 and R8: no reply, uncertain, and exposure over $500" if e > ESCALATE_EXPOSURE
                             else "R8: evidence conflicts", e))
        if x.shared_element and x.connected_cards:
            acts.append(_nba("MONITOR_CONNECTED_CARDS", f"R6: cards sharing {x.shared_element} stay under monitoring", e))
        return order(acts)
    raise ValueError(f"unknown reply {x.reply!r}")


def status_for(verdict: str, final: list[dict]) -> str:
    acts = {a["action"] for a in final}
    if "ESCALATE_TO_ANALYST" in acts:
        return "escalated"
    if verdict == "fraud":
        return "closed_fraud"
    if verdict == "legitimate":
        return "closed_legitimate"
    return "open"
