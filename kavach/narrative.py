"""Prose for the case summary, the SAR narrative and the undocumented-pattern description.

Deterministic templates first (always valid); the LLM rewrites them from the same facts when available.
"""
import re

from kavach.llm import Llm, sentences

# a SAR must never put the customer in the role of the actor, and must not invent policy thresholds
BAD_SAR = re.compile(r"\b(customer|cardholder)\b[^.]{0,60}\b(used|made|conducted|initiated)\b(?![^.]{0,30}\bnot\b)|threshold", re.I)

PATTERN_WORDS = {
    "card_testing": "card testing",
    "card_not_present_fraud": "card-not-present fraud",
    "card_not_present_new_device": "card-not-present fraud from a device new to the account",
    "out_of_region_use": "card-present use in a billing region inconsistent with the cardholder",
    "account_takeover": "account takeover",
    "undocumented": "an undocumented coordinated pattern",
    "none": "no fraud",
}


def pattern_description(facts: dict) -> str:
    kind = facts.get("undocumented_kind")
    if kind == "sub_threshold":
        thr = facts["threshold"] or 500
        return (f"A stolen card number is cashed out through a tight run of online purchases, each priced just under ${thr:,.0f}, "
                f"apparently to stay below a per-transaction authorization or review limit. The same template appears on "
                f"{facts.get('n_template_cards', 0)} other customers' cards within three weeks and the bank's model scores it low, "
                "so it is repeated, coordinated abuse rather than a one-off. It was found by matching the burst shape across cards "
                "and against earlier confirmed cases.")
    return (f"One device profile ({facts.get('device', 'unknown')}) makes low-value online purchases on many unrelated customers' cards, "
            "always marked New to the account and behind an anonymous proxy, keeping amounts small so the model scores them low. "
            f"It touched {facts.get('n_connected', 0)} other cards around this alert and matches earlier confirmed cases on the same "
            "profile. It was found by pivoting from the card to the device profile and grouping every card on it.")


def summary_template(facts: dict) -> str:
    v, p = facts["verdict"], facts["p"]
    lead = facts["headline"]
    if v == "legitimate":
        s = (f"{lead} The evidence points to the cardholder: {facts['top_legit']}. "
             f"Fraud probability {p:.2f}; the alert is closed as legitimate.")
    elif v == "fraud":
        s = (f"{lead} Assessed as {PATTERN_WORDS[facts['pattern']]} with fraud probability {p:.2f}: {facts['top_fraud']}. "
             f"The episode covers {facts['n_txns']} transaction(s) totalling ${facts['exposure']:,.2f}"
             + (f", and {facts['n_connected']} other card(s) are linked through {facts['shared']}" if facts["n_connected"] else "") + ".")
    else:
        s = (f"{lead} The evidence is balanced (fraud probability {p:.2f}): for fraud, {facts['top_fraud'] or 'little beyond the alert'}; "
             f"against, {facts['top_legit'] or 'no clear exculpatory evidence'}. The case stays under monitoring pending the cardholder.")
    if facts.get("reply"):
        s += f" Evidence request: {facts['reply']}."
    return s


def sar_template(facts: dict) -> str:
    who = f"customer {facts['customer_id']}, card {facts['card_id']}"
    s = [
        f"This report concerns {who}, issued as a {facts['card_desc']}.",
        (f"On {facts['date_from']}, " if facts['date_from'] == facts['date_to'] else f"Between {facts['date_from']} and {facts['date_to']}, ")
        + f"{facts['n_txns']} transaction(s) totalling ${facts['exposure']:,.2f} "
        f"were identified as part of one fraud episode, beginning with transaction {facts['first_txn']}.",
        f"The activity took place {facts['where']}.",
        f"The transactions were {facts['how']}.",
        f"The activity is assessed as {PATTERN_WORDS[facts['pattern']]}: {facts['top_fraud']}.",
    ]
    if facts["n_connected"]:
        s.append(f"The same {facts['shared']} links this activity to {facts['n_connected']} other card(s), including "
                 f"{', '.join(facts['connected'][:4])}, which indicates a common actor across cardholders.")
    else:
        s.append("No other cardholder was found to share a device profile, region cluster or email with this activity.")
    if facts.get("prior_cases"):
        s.append(f"Earlier confirmed cases {', '.join(facts['prior_cases'][:3])} show the same pattern.")
    s.append(f"{facts['customer_statement']}")
    s.append(f"The bank has recommended {facts['containment'] or 'containment of the card'} and opened an internal case; the report is filed under {facts['rule']}.")
    return " ".join(x.strip() for x in s if x.strip())


def write_all(llm: Llm, facts: dict, want_sar: bool, undocumented: bool) -> dict:
    out = {"sources": {}}
    tmpl = summary_template(facts)
    text, src = llm.write(
        "Write a case summary for a bank fraud analyst in 3 to 5 sentences: (1) what triggered the alert, (2) the two or three "
        "strongest findings, taken from top_fraud and top_legit, (3) the verdict and fraud probability, (4) the final actions from "
        "final_actions in plain words. Only state findings present in the facts; never claim that something is absent or that there "
        "was no prior activity unless the facts say so; never say the card is blocked unless BLOCK_CARD is in final_actions. "
        "No lists, no headings.",
        facts, tmpl, check=lambda t: 2 <= sentences(t) <= 6)
    out["summary"], out["sources"]["summary"] = text, src
    if want_sar:
        tmpl = sar_template(facts)
        sar_facts = {k: v for k, v in facts.items() if k not in ("top_legit", "headline", "rule", "trigger")}
        text, src = llm.write(
            "Write the narrative of a Suspicious Activity Report in 7 to 11 sentences, FinCEN style, so it stands on its own: "
            "WHO (customer id, card id, devices, other cards), WHAT happened, WHEN (dates and times), WHERE (channel, billing regions, "
            "product codes), HOW it was carried out, and WHY it is suspicious. The transactions were made ON the customer's card by an "
            "unauthorized party; never write that the customer made or used them. State what the cardholder said only as given in "
            "customer_statement. Do not mention risk scores, internal policy rule numbers or policy thresholds. Past tense, factual, no speculation beyond the facts, "
            "no bullet points. Use identifiers exactly as given."
            + (" The pattern is undocumented: say it matches none of the documented fraud typologies." if undocumented else ""),
            sar_facts, tmpl, check=lambda t: 6 <= sentences(t) <= 12 and (not BAD_SAR.search(t) or (
                facts.get("undocumented_kind") == "sub_threshold" and not re.search(r"\b(customer|cardholder)\b[^.]{0,60}\b(used|made)\b(?![^.]{0,30}\bnot\b)", t, re.I))))
        out["sar"], out["sources"]["sar"] = text, src
    if undocumented:
        tmpl = pattern_description(facts)
        text, src = llm.write(
            "In 2 or 3 sentences, describe this fraud pattern in your own words: what the pattern is, who it affects, and how it was "
            "found. It fits none of the documented typologies (card testing, card-not-present, new device, out-of-region, account takeover).",
            facts, tmpl, check=lambda t: 2 <= sentences(t) <= 4)
        out["pattern_description"], out["sources"]["pattern_description"] = text, src
    return out
