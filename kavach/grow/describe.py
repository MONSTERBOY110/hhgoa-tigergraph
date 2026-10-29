"""Names and plain-language descriptions of mined detectors.

The name is a deterministic slug from the family and its parameters (stable file names). The description is written
by the LLM from aggregate backtest facts only (counts, rates, closed-case ids), with the usual id post-check, and
falls back to a template that is recorded as such.
"""
from __future__ import annotations

from kavach.llm import Llm, sentences

TASK = ("Write 2 to 3 plain sentences that describe this fraud detector for a bank's fraud team: what behaviour it "
        "catches, in everyday words, and how it did in the backtest, copying the numbers exactly from the facts. "
        "No bullet points, no headings, no recommendations, no identifiers other than those in the facts.")


def title(c) -> str:
    fam = {"band_burst": "Burst inside an amount band", "shared_entity": "Entity shared across cards",
           "dormancy": "Dormant card wakes up", "micro": "Run of micro charges", "fanin": "Fan-in spike to one recipient",
           "repeat_amount": "Repeated same-amount charges", "new_device_proxy": "New device behind a proxy on many cards"}
    return fam.get(c.family, c.family)


def facts(c) -> dict:
    return {"detector": c.name, "family": c.family, "rule": c.rule,
            "fraud_cases_hit": c.support, "fraud_cases_total": c.n_fraud,
            "cleared_cases_hit": c.cleared_hits, "cleared_cases_total": c.n_cleared,
            "population_rate_percent": round(100 * c.population_rate, 3), "population_sample": c.n_pop,
            "lift": c.lift, "precision": c.precision, "bank_pattern_labels_among_hits": c.patterns,
            "status": c.status, "reproduces_hand_written_detector": c.rediscovers or None,
            "example_closed_cases": c.fraud_case_ids[:3]}


def template(c) -> str:
    tail = (f" It reproduces the hand-written {c.rediscovers} detector, found without being told about it."
            if c.rediscovers else " No hand-written detector of the same shape covers 60 percent or more of its hits.")
    return (f"Fires when {c.rule}. In the bank's closed cases it fired on {c.support} of {c.n_fraud:,} confirmed-fraud cases "
            f"and {c.cleared_hits} of {c.n_cleared} cleared ones (precision {c.precision:.1%}), and on "
            f"{100 * c.population_rate:.3f}% of a {c.n_pop:,}-transaction population sample, a lift of {c.lift:.1f}." + tail)


def describe(c, llm: Llm | None = None) -> tuple[str, str]:
    """(description, source) where source is 'llm' or 'template'."""
    llm = llm or Llm()
    return llm.write(TASK, facts(c), template(c), check=lambda t: 2 <= sentences(t) <= 3 and len(t) <= 700)
