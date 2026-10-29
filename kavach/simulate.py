"""Deterministic evidence-reply simulator (policy section 5: replies are not provided, so we simulate).

The reply follows from the evidence gathered WITHOUT the customer, never from a coin flip:
  * a recurring charge -> the customer recognises it (R7);
  * the profile fits (p without the customer at or below CONFIRM_P, or two or more independent groups on the legitimate
    side while p stays below DENY_P) -> never a denial: the customer confirms (R3) when nothing leans to fraud,
    else does not reply (R4);
  * fraud-leaning evidence exists without the customer -> the customer denies (R2);
  * otherwise -> no reply within 24 hours (R4).
A customer who already disputed is not assumed to repeat the denial: the bank cleared a dispute whose purchase fitted
the cardholder's normal pattern (round1-postmortem.md, HHG-004).
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


def profile_fits(p_without_customer: float, n_legit_groups: int) -> bool:
    """The purchase fits the cardholder: p without the customer at or below CONFIRM_P, or two or more independent
    legitimate groups while the balance does not lean to fraud (p below DENY_P; two weak profile matches do not
    outweigh, say, an account already in confirmed-fraud cases)."""
    return p_without_customer <= CONFIRM_P or (n_legit_groups >= 2 and p_without_customer < DENY_P)


def fraud_leaning(p_without_customer: float, n_fraud_groups: int, trigger: str, strongest_group: float,
                  n_fraud_signals: int = 0) -> bool:
    if trigger == "customer_report":
        # the dispute is already on record; one independent fraud group or signal that agrees with it is enough
        return p_without_customer > CONFIRM_P and (n_fraud_groups >= 1 or n_fraud_signals >= 1 or strongest_group >= STRONG_GROUP)
    return p_without_customer >= DENY_P and (n_fraud_groups >= 2 or strongest_group >= STRONG_GROUP)


def customer_reply(p_without_customer: float, n_fraud_groups: int, recurring: bool, trigger: str,
                   n_episode: int = 1, still_has_card: bool = True, strongest_group: float = 0.0,
                   n_legit_groups: int = 0, n_fraud_signals: int = 0) -> Reply:
    """n_fraud_signals: individual fraud-leaning signals (llr >= 0.3, not score, not customer) found without the
    customer, counted before netting inside their group: an amount far above the account's maximum still argues
    for fraud even when the product and email fit, so the customer is never assumed to confirm it."""
    if recurring:
        return Reply("recognise_recurring",
                     "Customer recognises the charge as their own recurring subscription after being shown the earlier "
                     "identical monthly charges, and withdraws the dispute")
    fits = profile_fits(p_without_customer, n_legit_groups)
    leaning = fraud_leaning(p_without_customer, n_fraud_groups, trigger, strongest_group, n_fraud_signals)
    if fits:
        if p_without_customer <= CONFIRM_P and not n_fraud_groups and not n_fraud_signals:
            if trigger == "customer_report":
                return Reply("confirm",
                             "On review of the transaction details with the customer, the customer recognises the purchase "
                             "(made by themselves or an authorised household member) and withdraws the dispute")
            return Reply("confirm", "Customer confirms they made the purchase")
        return Reply("no_reply", "No reply from the customer within 24 hours")
    if leaning:
        scope = "these purchases" if n_episode > 1 else "this purchase"
        tail = " and still has the card" if still_has_card else ""
        if trigger == "customer_report":
            return Reply("deny", f"Customer confirms the dispute covers {scope}: they did not make {'them' if n_episode > 1 else 'it'}, "
                                 f"did not share the card details{tail}")
        return Reply("deny", f"Customer states they did not make {scope}{tail}")
    return Reply("no_reply", "No reply from the customer within 24 hours")
