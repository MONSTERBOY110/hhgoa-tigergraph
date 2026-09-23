"""Deterministic evidence-reply simulator (README section 5: replies are not provided, so we simulate).

The reply follows from the evidence gathered WITHOUT the customer, never from a coin flip:
fraud-leaning evidence -> the customer denies; legitimate-leaning -> the customer confirms;
recurring charge -> the customer recognises it; balanced -> no reply within 24 hours.
"""
from dataclasses import dataclass

DENY_P = 0.55
CONFIRM_P = 0.40
STRONG_GROUP = 1.5


@dataclass
class Reply:
    kind: str       # deny | confirm | no_reply | recognise_recurring
    text: str
    request_type: str = "customer_validation"


def customer_reply(p_without_customer: float, n_fraud_groups: int, recurring: bool, trigger: str,
                   n_episode: int = 1, still_has_card: bool = True, strongest_group: float = 0.0) -> Reply:
    if recurring:
        return Reply("recognise_recurring",
                     "Customer recognises the charge as their own recurring subscription after being shown the earlier "
                     "identical monthly charges, and withdraws the dispute")
    if trigger == "customer_report" and p_without_customer > CONFIRM_P:
        scope = "these purchases" if n_episode > 1 else "the purchase"
        return Reply("deny", f"Customer confirms the dispute: they did not make {scope}, did not share the card details, and still has the card")
    if p_without_customer >= DENY_P and (n_fraud_groups >= 2 or strongest_group >= STRONG_GROUP):
        scope = "these purchases" if n_episode > 1 else "this purchase"
        tail = " and still has the card" if still_has_card else ""
        if trigger == "customer_report":
            return Reply("deny", f"Customer confirms the dispute covers all of {scope} in the episode, states they did not make them{tail}")
        return Reply("deny", f"Customer states they did not make {scope}{tail}")
    if p_without_customer <= CONFIRM_P:
        if trigger == "customer_report":
            return Reply("confirm",
                         "On review of the transaction details with the customer, the customer recognises the purchase "
                         "(made by themselves or an authorised household member) and withdraws the dispute")
        return Reply("confirm", "Customer confirms they made the purchase")
    return Reply("no_reply", "No reply from the customer within 24 hours")
