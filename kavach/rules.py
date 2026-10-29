"""Policy text to detector (Round 2, challenge 3): a written rule of the Fraud Policy becomes a working detector.

The rule's sentence is read from the organizer's policy file at run time (never pasted into code), tokenized by a small
lexicon (counts, windows, money, size words, channels, scopes, entities, sequence words, actions, conditions) and
compiled into an executable rule:

    prose --tokenize--> tokens --interpret--> Spec (parameters + every open interpretation choice) --compile--> Rule

Two detector templates exist: a single-card sequence (R5 card testing) and a cross-card shared element (R6 shared
origin). Prose leaves things open ("small", "larger", "several", "in one window", "show fraud", how soon "followed by",
which alerts the rule is about); each gap becomes a Choice that records the phrase, the options considered, the one
taken and why, and the backtest re-runs the detector with every alternative (one at a time) so the cost of each
reading is measured, not argued. Change the sentence and the detector changes (tests/test_policy_rules.py).

    python -m kavach.rules [R5|R6|all] [--pop N] [--no-agent] [--refresh-agent]
    python -m kavach.rules parse [R1..R10|all]        tokens, template and open choices for any rule (no data needed)
    python -m kavach.rules check R5 CC-0137            re-run one attested claim (a closed case or an HHF case)

Backtests follow kavach/backtest.py: one anchor transaction per closed case (backtest.labels), a reference sample of
transactions before 2017 that are in no confirmed-fraud case (backtest.population, seed 42); support (confirmed-fraud
cases hit), cleared hits (legitimate customers the rule would act against), population rate and lift with the same
0.5-hit floor and interval. Rule versus agent: on the closed cases against the agent's own card-testing detector
(kavach.detectors.onecard.detect, run unchanged) and the bank's labels; on the 18 HHF cases against the agent's dry-run
answers (kavach.harness.runner.run_case, KAVACH_NO_LLM=1, KAVACH_RECORD=0). Output: detectors/rules/<rule>.json and
docs/round2/policy-to-detector.md; every claim carries what, evidence ids, how (function or SQL plus parameters) and
when (timestamp plus kavach.duckq.dataset_version()).
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace

import numpy as np
import pandas as pd

from kavach.config import RAW2, ROOT

POLICY_FILES = (RAW2 / "policy" / "fraud_policy.md", ROOT / "docs" / "round2" / "fraud_policy.md")
OUT_DIR = ROOT / "detectors" / "rules"
DOC = ROOT / "docs" / "round2" / "policy-to-detector.md"
AGENT_CACHE = ROOT / "logs" / "agent_dryrun_r2.json"
REF_BEFORE = "2017-01-01"
NS_MIN = 60 * 10**9
NS_HOUR = 60 * NS_MIN
NS_DAY = 24 * NS_HOUR


def _rel(p) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _now() -> str:
    return pd.Timestamp.now().isoformat(timespec="seconds")


def _ns(t) -> int:
    return int(pd.Timestamp(t).value)


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and str(v) not in ("", "nan", "None", "<NA>")


# ================================================================================================ reading the policy
def policy_source() -> dict:
    """The organizer's policy file (data/raw/round2/policy, from `kavach download --round 2`), else the team copy."""
    for p in POLICY_FILES:
        if p.exists():
            raw = p.read_bytes()
            return {"path": p, "text": raw.decode("utf-8"), "sha256": hashlib.sha256(raw).hexdigest()}
    raise FileNotFoundError("the Fraud Policy file is missing; run `python -m kavach download --round 2` (looked for "
                            + ", ".join(_rel(p) for p in POLICY_FILES) + ")")


RULE_LINE = re.compile(r"^\*\*(R\d+)\.\s*(.+?)\.\*\*\s*(.+)$")
ACTION_ROW = re.compile(r"^\|\s*`([A-Z_]+)`\s*\|")


def rules_in_policy(src: dict | None = None) -> dict:
    """R1..R10 as written: id -> {id, title, text, line, source, sha256}."""
    src = src or policy_source()
    out = {}
    for i, line in enumerate(src["text"].splitlines(), 1):
        m = RULE_LINE.match(line.strip())
        if m:
            out[m.group(1)] = {"id": m.group(1), "title": m.group(2).strip(), "text": m.group(3).strip(), "line": i,
                               "source": _rel(src["path"]) if hasattr(src["path"], "relative_to") else str(src["path"]),
                               "sha256": src["sha256"]}
    return out


def policy_actions(src: dict | None = None) -> set:
    """The action identifiers of the policy's section 1 table (the only actions a compiled rule may recommend)."""
    src = src or policy_source()
    return {m.group(1) for line in src["text"].splitlines() if (m := ACTION_ROW.match(line.strip()))}


# ================================================================================================ tokenizer
NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
       "ten": 10}
NUM_RE = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|an|a)"
UNIT_MIN = {"minute": 1, "hour": 60, "day": 1440, "week": 10080}
ENTITY_FIELD = {"device profile": "device_key", "billing region": "addr1", "recipient email": "R_emaildomain",
                "purchaser email": "P_emaildomain"}


def _num(s: str) -> int:
    return int(s) if s.isdigit() else NUM[s.lower()]


@dataclass
class Token:
    slot: str
    text: str
    start: int
    end: int
    value: dict = field(default_factory=dict)
    vague: bool = False          # the words do not pin the value down; interpret() must resolve it as a Choice


def _money(m):
    op = m["op"].lower()
    return {"op": ">" if op in ("over", "above", "exceeds", "exceeding", "more than") else "<", "usd": float(m["amt"].replace(",", ""))}


LEXICON = (
    ("count", rf"\b(?P<n>{NUM_RE}) or more\b", lambda m: {"op": ">=", "n": _num(m["n"])}, False),
    ("count", rf"\bat least (?P<n>{NUM_RE})\b", lambda m: {"op": ">=", "n": _num(m["n"])}, False),
    ("count", r"\b(?P<w>several|multiple|many|a few)\b", lambda m: {"word": m["w"].lower()}, True),
    ("window", rf"\bwithin (?P<n>{NUM_RE}) (?P<u>minute|hour|day|week)s?\b",
     lambda m: {"minutes": _num(m["n"]) * UNIT_MIN[m["u"].lower()]}, False),
    ("window", r"\bin (?:one|the same|a single|a short) window\b", lambda m: {}, True),
    ("money", r"\b(?P<op>over|above|exceeds|exceeding|under|below|less than|more than) \$(?P<amt>[\d,]+(?:\.\d+)?)", _money, False),
    ("size", r"\b(?P<w>small|smaller|large|larger|big|bigger)\b",
     lambda m: {"word": m["w"].lower(), "comparative": m["w"].lower().endswith("er")}, True),
    ("channel", r"\b(?P<c>online|card-present|in person|in-person)\b",
     lambda m: {"channel": "online" if m["c"].lower() == "online" else "in_person"}, False),
    ("scope", r"\bon (?:one|the same|a single) card\b", lambda m: {"scope": "card"}, False),
    ("scope", rf"\b(?:several|multiple|many|{NUM_RE}) (?:other )?cards\b", lambda m: {"scope": "cross_card"}, False),
    ("sequence", r"\bfollowed by\b", lambda m: {"order": "after"}, True),
    ("event", r"\b(?P<e>authori[sz]ations?|purchases?|transactions?|charges?|payments?)\b",
     lambda m: {"event": re.sub(r"s$", "", m["e"].lower()).replace("authorisation", "authorization")},
     None),                               # vagueness decided per word below: the schema has no authorization status
    ("entity", r"\bthe same (?P<e>device profile|billing region|recipient email|purchaser email)\b",
     lambda m: {"field": ENTITY_FIELD[m["e"].lower()], "phrase": m["e"].lower()}, True),
    ("predicate", r"\bshows? fraud\b", lambda m: {"predicate": "fraud"}, True),
    ("state", r"\b(?:has )?already cleared\b", lambda m: {"state": "cleared"}, True),
    ("action", r"`(?P<a>[A-Z_]+)`", lambda m: {"action": m["a"]}, False),
    ("condition", r"\b[Ii]f (?P<c>[^,.]+),", lambda m: {"clause": m["c"]}, False),
    ("target", r"\bfor every card that shares it\b", lambda m: {"target": "all_sharing"}, True),
    ("output", r"\bname the shared element\b", lambda m: {"output": "shared_element"}, False),
)


def tokenize(text: str) -> list[Token]:
    toks = []
    for slot, pat, build, vague in LEXICON:
        for m in re.finditer(pat, text, flags=re.I if slot not in ("action",) else 0):
            v = build(m)
            vg = vague if vague is not None else (v.get("event") == "authorization")
            toks.append(Token(slot, m.group(0), m.start(), m.end(), v, bool(vg)))
    toks.sort(key=lambda t: (t.start, t.slot))
    return toks


def template_of(tokens: list[Token]) -> str | None:
    scopes = {t.value.get("scope") for t in tokens if t.slot == "scope"}
    slots = {t.slot for t in tokens}
    if "card" in scopes and "sequence" in slots and "count" in slots:
        return "single_card_sequence"
    if "cross_card" in scopes and "entity" in slots:
        return "cross_card_shared_element"
    return None


# ================================================================================================ interpretation
@dataclass
class Choice:
    id: str
    phrase: str                  # the words of the rule this choice resolves ("" when the rule is silent)
    question: str
    options: list                # [{"label", "params"}]; the first is the one taken
    chosen: str
    reason: str
    kind: str = "vague"          # vague: a word that needs a number; omitted: the text says nothing; schema: no such column
    measured: dict = field(default_factory=dict)   # data behind the choice: what, value, how (sql + params), when


@dataclass
class Spec:
    rule_id: str
    title: str
    text: str
    source: str
    sha256: str
    template: str
    params: dict
    consequence: dict
    tokens: list
    choices: list
    compiled_at: str = ""
    compile_ms: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tokens"] = [asdict(t) for t in self.tokens]
        d["choices"] = [asdict(c) for c in self.choices]
        return d


class NotADetector(ValueError):
    pass


def _measure(q, what: str, sql: str, params=()):
    df = q(sql, *params)
    val = df.iloc[0, 0] if len(df) else None
    return (None if val is None else float(val)), {"what": what, "how": {"sql": " ".join(sql.split()), "params": list(params)},
                                                   "when": _now()}


def _split_at(text: str, tokens: list[Token], pos: int):
    return [t for t in tokens if t.start < pos], [t for t in tokens if t.start >= pos]


def _consequence(text: str, tokens: list[Token], actions_ok: set | None) -> dict:
    """Actions of the main clause, conditional branches ("If ..., recommend X") and targets, from the tokens."""
    conds = [t for t in tokens if t.slot == "condition"]
    acts = [t for t in tokens if t.slot == "action"]
    bad = sorted({t.value["action"] for t in acts} - actions_ok) if actions_ok else []
    if bad:
        raise ValueError(f"the rule names actions that are not in the policy's action table: {bad}")
    main = [t.value["action"] for t in acts if not conds or t.start < conds[0].start]
    branches = []
    for i, c in enumerate(conds):
        end = conds[i + 1].start if i + 1 < len(conds) else len(text)
        b_acts = [t.value["action"] for t in acts if c.end <= t.start < end]
        money = [t.value for t in tokens if t.slot == "money" and c.start <= t.start < c.end]
        state = [t.value["state"] for t in tokens if t.slot == "state" and c.start <= t.start < c.end]
        branches.append({"if": c.value["clause"].strip(), "money": money, "state": state, "actions": b_acts})
    target = next((t.value["target"] for t in tokens if t.slot == "target"), None)
    out = next((t.value["output"] for t in tokens if t.slot == "output"), None)
    return {"actions": main, "branches": branches, "target_of_last_action": target, "outputs": [out] if out else []}


def interpret(rule: dict, q=None, actions_ok: set | None = None) -> Spec:
    """Tokens -> Spec. `q(sql, *params) -> DataFrame` answers the data-dependent choices (kavach.duckq.q by default)."""
    t_start = time.perf_counter()
    text = rule["text"]
    toks = tokenize(text)
    tpl = template_of(toks)
    if tpl is None:
        raise NotADetector(f"{rule['id']} is a decision rule over a verdict, not a pattern in the data (no per-card sequence and "
                           "no shared element across cards); kavach/policy.py implements it")
    if q is None:
        from kavach import duckq
        q = duckq.q
    colon = text.find(":")
    cond_toks, cons_toks = _split_at(text, toks, colon if colon >= 0 else len(text))
    if tpl == "cross_card_shared_element":
        cond_toks, cons_toks = toks, toks           # "When <condition>, <consequence>": both read from the whole sentence
    cons = _consequence(text, cons_toks, actions_ok)
    if tpl == "single_card_sequence":
        params, choices = _interpret_sequence(text, cond_toks, cons, q)
    else:
        params, choices = _interpret_shared(text, cond_toks, cons, q)
    spec = Spec(rule["id"], rule["title"], text, rule.get("source", ""), rule.get("sha256", ""), tpl, params, cons, toks, choices,
                compiled_at=_now())
    spec.compile_ms = round((time.perf_counter() - t_start) * 1000, 1)
    return spec


def _first(toks, slot, key=None):
    for t in toks:
        if t.slot == slot and (key is None or key in t.value):
            return t
    return None


def _interpret_sequence(text, toks, cons, q):
    seq = _first(toks, "sequence")
    before, after = [t for t in toks if t.start < seq.start], [t for t in toks if t.start > seq.start]
    count = _first(before, "count", "n")
    win = _first(before, "window", "minutes")
    chan = _first(before, "channel")
    size_a = _first(before, "size")
    ev_a = _first(before, "event")
    size_b = _first(after, "size")
    ev_b = _first(after, "event")
    chan_b = _first(after, "channel")
    money_branch = [m for b in cons["branches"] for m in b["money"]]
    if count is None or win is None:
        raise NotADetector("a sequence rule needs an explicit count and window")
    choices = []
    # "small": a size word with no number. Label-free: the bottom 5% of this bank's online amounts before January.
    S, meas = _measure(q, "5th percentile of online transaction amounts before 2017",
                       "select quantile_cont(TransactionAmt, 0.05) from txn where channel = ? and ts < ?",
                       (chan.value["channel"] if chan else "online", REF_BEFORE))
    S1, _m1 = _measure(q, "1st percentile", "select quantile_cont(TransactionAmt, 0.01) from txn where channel = ? and ts < ?",
                       (chan.value["channel"] if chan else "online", REF_BEFORE))
    S10, _m10 = _measure(q, "10th percentile", "select quantile_cont(TransactionAmt, 0.10) from txn where channel = ? and ts < ?",
                         (chan.value["channel"] if chan else "online", REF_BEFORE))
    meas["value"] = S
    meas["alternatives"] = {"p01": S1, "p10": S10}
    S, S1, S10 = round(S, 2), round(S1, 2), round(S10, 2)
    if size_a is not None:
        choices.append(Choice(
            "small_amount", size_a.text, f"What amount is '{size_a.text}'?",
            [{"label": f"under ${S:,.2f} (5th percentile of online amounts)", "params": {"test_max_amt": S}},
             {"label": f"under ${S1:,.2f} (1st percentile)", "params": {"test_max_amt": S1}},
             {"label": f"under ${S10:,.2f} (10th percentile)", "params": {"test_max_amt": S10}}],
            f"under ${S:,.2f}",
            "The policy gives no number. A label-free reading: an amount is small when it sits in the bottom 5% of what this "
            f"bank's customers pay online (July to December 2016), which is ${S:,.2f}; it is computed from the index at compile "
            "time, so the rule never learned from the case labels it is scored on. It also matches the agent's own card-testing "
            "detector (under $10), which keeps the rule-versus-agent comparison about structure, not about a dollar figure.",
            "vague", meas))
    # the window: does "within an hour" bind the small authorizations only, or the whole sequence?
    W = win.value["minutes"]
    choices.append(Choice(
        "window_binding", win.text, f"Does '{win.text}' bound the small authorizations only, or also the larger purchase?",
        [{"label": "the small authorizations only", "params": {"window_binds": "tests"}},
         {"label": "the whole sequence, larger purchase included", "params": {"window_binds": "sequence"}}],
        "the small authorizations only",
        f"The comma reads '{count.text} small ... {win.text}, {seq.text} a larger purchase': the window phrase sits inside the "
        "first clause, before the comma, so it counts the tests; the cash-out is attached by 'followed by'.", "vague"))
    # how soon is "followed by"?
    choices.append(Choice(
        "follow_gap", seq.text, f"How soon after the last small authorization must the larger purchase come ('{seq.text}')?",
        [{"label": "within 24 hours", "params": {"follow_gap_h": 24}}, {"label": "within 1 hour", "params": {"follow_gap_h": 1}},
         {"label": "within 72 hours", "params": {"follow_gap_h": 72}}],
        "within 24 hours",
        "The text sets no gap. R5's remedy is to decline pending authorizations and require step-up before further activity, "
        "which only makes sense while the cash-out is imminent; a day keeps it the same episode, and the episode builder's "
        "short lookback is 3 days (sensitivity shows 1 and 72 hours).", "vague"))
    # "larger": a comparative whose referent is the small authorizations
    if size_b is not None:
        choices.append(Choice(
            "larger_purchase", size_b.text, f"How much larger is '{size_b.text}'?",
            [{"label": f"not small: ${S:,.2f} or more (larger than every test by construction)", "params": {"follow_rule": "not_small"}},
             {"label": "over 5 times the largest test and over $20 (the agent's bar)", "params": {"follow_rule": "x5_20"}},
             {"label": f"over ${money_branch[0]['usd']:,.0f} (the rule's own block threshold)" if money_branch else "over $100",
              "params": {"follow_rule": "over_branch"}}],
            "not small",
            f"'{size_b.text}' is a comparative and its referent is the small authorizations, so the literal reading is the opposite "
            "of small: any purchase at or above the small threshold is larger than every test. The agent's stricter bar (5 times "
            "and $20) and the rule's own $100 block threshold are measured as alternatives.", "vague"))
    # authorization: the schema has no authorization status
    if ev_a is not None and ev_a.vague:
        choices.append(Choice(
            "authorization", ev_a.text, f"What is an '{ev_a.text}' in this data?",
            [{"label": "every transaction row (approved authorizations only are recorded)", "params": {}}],
            "every transaction row",
            "The index has one row per approved transaction and no authorization or settlement status; declined test attempts are "
            "invisible, so the rule can only under-count tests. Recorded as a schema gap, no alternative is possible.", "schema"))
    # the follow-up's channel
    choices.append(Choice(
        "follow_channel", ev_b.text if ev_b else "", "Must the larger purchase also be online?",
        [{"label": "any channel", "params": {"follow_channel": "any"}}, {"label": "online only", "params": {"follow_channel": "online"}}],
        "any channel" if chan_b is None else chan_b.value["channel"],
        "'online' qualifies the small authorizations; the larger purchase carries no channel word, so it is not restricted.",
        "omitted"))
    # "on one card": the card id (customer cards pool several accounts in this data)
    sc = _first(toks, "scope", "scope")
    choices.append(Choice(
        "one_card", sc.text if sc else "", f"What is 'one card' ('{sc.text if sc else ''}')?",
        [{"label": "the card id of the alert", "params": {"unit": "card"}},
         {"label": "the account behind the card (holder_key, usable before 2017 only)", "params": {"unit": "holder"}}],
        "the card id",
        "The card id is what the alert, the case and the answer name. In this data a card id pools many accounts (holder_key = "
        "card1, billing region and account start), so a pooled card can show three small online purchases in an hour from three "
        "different people; the account reading is measured as the alternative, but it cannot be the default because holder_key "
        "is unusable from January 2017 (D1 resampled, january-data-profile.md), the month under investigation.", "schema"))
    # omitted: which alerts the rule is about, and what is known at decision time
    choices.append(Choice(
        "alert_scope", "", "Which alerts does the rule fire for? The rule describes a sequence on a card, not one transaction.",
        [{"label": "a sequence within 72 hours either side of the flagged transaction", "params": {"scope": 72}},
         {"label": "the flagged transaction is itself a test or the larger purchase", "params": {"scope": "member"}},
         {"label": "a sequence within 24 hours either side", "params": {"scope": 24}}],
        "within 72 hours either side",
        "R5 reacts to pending authorizations and to a purchase that has already cleared, i.e. to the card's activity around the "
        "alert, not to the flagged row alone. 72 hours equals the episode builder's short lookback (kavach.episode_model "
        "LOOKBACK_DAYS = 3).", "omitted"))
    choices.append(Choice(
        "knowledge_cutoff", "", "Which transactions may the rule see?",
        [{"label": "up to the case's opened_at (reference anchors: anchor + the median delay)", "params": {}}],
        "up to opened_at",
        "A detector may not use activity after the decision. Case anchors stop at opened_at; reference anchors, which have no "
        "case, stop at the anchor plus the median delay between a closed case's first fraudulent transaction and its "
        "opened_at, so both sides see the same amount of future on average.", "omitted"))
    params = {"min_tests": count.value["n"], "test_window_min": W, "test_channel": chan.value["channel"] if chan else None,
              "test_max_amt": S, "unit": "card", "window_binds": "tests", "follow_gap_h": 24, "follow_rule": "not_small", "follow_channel": "any",
              "scope": 72, "block_over": money_branch[0]["usd"] if money_branch else None,
              "branch_state": next((b["state"][0] for b in cons["branches"] if b["state"]), None)}
    if money_branch:
        choices.append(Choice(
            "already_cleared", next((b["if"] for b in cons["branches"]), ""), "What does 'already cleared' mean without a clearing status?",
            [{"label": f"a purchase over ${params['block_over']:,.0f} after the tests, at or before the decision time", "params": {}}],
            "dated at or before the decision", "The schema has no clearing status; a recorded transaction dated before the decision is "
            "treated as cleared (approved rows only are recorded).", "schema"))
    return params, choices


RING_SPAN_SQL = """with ring as (select c.case_id, unnest(string_split(c.card_id || '|' || c.connected_card_ids, '|')) card from closed c
                       where c.connected_card_ids is not null and c.connected_card_ids <> ''),
                  m as (select r.case_id, r.card, min(t.ts) first_ts from ring r
                        join closed_txn ct on ct.card_id = r.card and ct.outcome = 'confirmed_fraud'
                        join closed cc on cc.case_id = ct.case_id and cc.pattern = 'undocumented'
                        join txn t on t.TransactionID = ct.TransactionID group by 1, 2)
                  select max(span) from (select case_id, date_diff('hour', min(first_ts), max(first_ts)) / 24.0 span from m group by 1)"""


def _interpret_shared(text, toks, cons, q):
    count = _first(toks, "count")
    ents = [t for t in toks if t.slot == "entity"]
    win = _first(toks, "window")
    pred = _first(toks, "predicate")
    choices = []
    n_cards = count.value.get("n", 3) if count else 3
    if count is not None and count.vague:
        choices.append(Choice(
            "several", count.text, f"How many is '{count.text}' cards?",
            [{"label": "3 or more cards, the alert's card included", "params": {"min_cards": 3}},
             {"label": "2 or more", "params": {"min_cards": 2}}, {"label": "4 or more", "params": {"min_cards": 4}}],
            "3 or more", "'Several' means more than two. The bank's own ring note (CC-2649: 'Two other cardholders reported the same "
            "device profile this month') treats three cards as a ring, and the agent's ring detectors use the same minimum "
            "(kavach.detectors.network.RING_MIN_CARDS = 3).", "vague"))
        n_cards = 3
    if pred is not None:
        choices.append(Choice(
            "show_fraud", pred.text, f"When does a card '{pred.text}' from the element?",
            [{"label": "precedent or atypical: a confirmed-fraud case opened before the decision holds one of its element "
                       "transactions, or an element transaction is atypical for the card", "params": {"evidence": "either"}},
             {"label": "atypical only", "params": {"evidence": "atypical"}},
             {"label": "precedent only", "params": {"evidence": "precedent"}},
             {"label": "atypical, a device marked New also counting", "params": {"evidence": "atypical_or_new"}}],
            "precedent or atypical",
            "Fraud is only confirmed in the closed cases, so a confirmed case opened before the decision (not the alert's own) is "
            "the strongest reading; but a ring's newest victims have no case yet, so an element transaction that is atypical for "
            "the card's own history also counts (first use of the product, amount above every earlier one overall or for that "
            "product, first proxied connection: kavach.duckq_ext.card_atypical_window, the flags the typology detectors use). A "
            "device marked New is left out by default: it is common in normal traffic (onecard.py notes it does not separate fraud "
            "for most products).", "vague"))
    for e in ents:
        f, ph = e.value["field"], e.value["phrase"]
        if f == "device_key":
            choices.append(Choice(
                "device_profile", e.text, "What is 'the same device profile'?",
                [{"label": "identical device_key, excluding placeholder and family-level profiles", "params": {}}],
                "identical device_key, rare profiles only",
                "device_key joins DeviceInfo, OS, browser and screen. A placeholder ('|||') or a family name such as 'Windows' or "
                "'iOS Device' is shared by thousands of phones and is not one device: those are excluded with the agent's own test "
                "(kavach.detectors.network.generic_device: empty, on more than 100 cards, or a family name).", "schema"))
        elif f == "addr1":
            choices.append(Choice(
                "billing_region", e.text, "Which rows carry 'the same billing region'?",
                [{"label": "addr1 on every row, any channel", "params": {"region_channel": "any"}},
                 {"label": "addr1 on card-present rows only", "params": {"region_channel": "in_person"}}],
                "addr1 on every row",
                "addr1 is the billing region of the cardholder on every row; the rule says billing region, not place of purchase, "
                "so the literal reading keeps online rows (a regional breach shows up there). The typology detectors' card-present "
                "reading is measured as the alternative.", "schema"))
        elif f in ("R_emaildomain", "P_emaildomain"):
            choices.append(Choice(
                "email", e.text, f"What is 'the same {ph}'?",
                [{"label": f"the same {f} (the data holds the domain only)", "params": {}}],
                f"the same {f}",
                "The index stores email domains, not addresses, so 'the same email' can only be the same domain; for free-mail domains "
                "this is weak, which the baseline condition below handles.", "schema"))
    span, meas = _measure(q, "longest span, in days, between the first confirmed-fraud transactions of the cards of one bank ring "
                             "(closed cases listing connected cards)", RING_SPAN_SQL)
    meas["value"] = span
    span_txt = f"{span:.1f} days" if span is not None else "unknown (no ring in the closed cases)"
    choices.append(Choice(
        "window", win.text if win else "", f"How long is '{win.text if win else 'one window'}'?",
        [{"label": "14 days either side of the alert", "params": {"window_days": 14}},
         {"label": "7 days either side", "params": {"window_days": 7}}, {"label": "3 days either side", "params": {"window_days": 3}},
         {"label": "30 days either side", "params": {"window_days": 30}}],
        "14 days either side",
        f"The text gives no length. The bank's own confirmed ring ran {span_txt} from its first to its last confirmed card (the "
        "SM-G935F ring, CC-2649 to CC-3035), and an alert can come late in a ring, so the window must hold about two weeks of the "
        "ring's history before the alert; with the cutoff at the decision time, 14 days either side is in practice the 14 days "
        "before the alert. The agent's 7-day network window and 3 and 30 days are measured as alternatives.", "vague", meas))
    choices.append(Choice(
        "common_element", "", "Does sharing a common element (gmail.com, a big region) count?",
        [{"label": "only when the fraud-showing cards are at least 3 times the element's own baseline", "params": {"baseline_ratio": 3.0}},
         {"label": "no baseline condition", "params": {"baseline_ratio": 0.0}},
         {"label": "at least 5 times the baseline", "params": {"baseline_ratio": 5.0}}],
        "at least 3 times the baseline",
        "The rule is silent, but 'the same billing region' or 'the same recipient email' identifies a shared origin only when "
        "more cards show fraud from it than usually do: gmail.com and the largest regions carry dozens of atypical purchases "
        "every week. The baseline is the mean number of cards showing fraud from that value per window over the 90 days before "
        "the window (complete windows inside the data only); with no baseline the value must be rare (100 cards or fewer in "
        "the window).", "omitted"))
    choices.append(Choice(
        "anchor_card", "", "Is the alert's own card one of the 'several'?",
        [{"label": "yes, when its flagged transaction carries the element", "params": {}}], "yes",
        "The alert is the reason to look; its card carries the element through the flagged transaction, and R6 is applied to "
        "that card's case. The other cards must show fraud on their own evidence.", "omitted"))
    choices.append(Choice(
        "elements_of", "", "Which of the alert's elements are checked?",
        [{"label": "the values on the flagged transaction", "params": {}}], "the flagged transaction's values",
        "R6 names the shared element of the alert; the flagged transaction's device profile, billing region and recipient domain "
        "are the candidates (earlier rows of the card would add values the alert is not about).", "omitted"))
    if cons.get("target_of_last_action"):
        choices.append(Choice(
            "every_card", "for every card that shares it", "Which cards are 'every card that shares it'?",
            [{"label": "every card with the element in the window when the element is rare (100 cards or fewer); for a common "
                       "element only the cards that show fraud from it", "params": {"rare_cards": 100}}],
            "all sharing cards for a rare element, else the fraud-showing ones",
            "Taken literally for gmail.com the rule would put thousands of customers under monitoring; monitoring costs the "
            "customer nothing, but an analyst cannot act on thousands of names. Both counts are reported per hit.", "vague"))
    choices.append(Choice(
        "knowledge_cutoff", "", "Which evidence may the rule see?",
        [{"label": "transactions and closed cases dated up to opened_at (reference anchors: anchor + the median delay)", "params": {}}],
        "up to opened_at", "Same cut as R5; the alert's own closed case never counts as precedent for itself.", "omitted"))
    params = {"min_cards": n_cards, "fields": [e.value["field"] for e in ents], "window_days": 14, "evidence": "either",
              "baseline_ratio": 3.0, "baseline_days": 90, "region_channel": "any", "rare_cards": 100}
    return params, choices


# ================================================================================================ compiled rules
def compile_rule(spec: Spec):
    return SequenceRule(spec) if spec.template == "single_card_sequence" else SharedElementRule(spec)


def compile_from_policy(rule_id: str, q=None, src: dict | None = None):
    """Read the rule from the policy file and compile it; returns (rule, timings)."""
    t0 = time.perf_counter()
    src = src or policy_source()
    rules = rules_in_policy(src)
    if rule_id not in rules:
        raise KeyError(f"{rule_id} is not in {_rel(src['path'])}")
    t_read = time.perf_counter()
    spec = interpret(rules[rule_id], q=q, actions_ok=policy_actions(src))
    rule = compile_rule(spec)
    t_done = time.perf_counter()
    return rule, {"read_ms": round((t_read - t0) * 1000, 1), "interpret_ms": spec.compile_ms,
                  "total_ms": round((t_done - t0) * 1000, 1)}


def valid_account(hk, ts) -> bool:
    """holder_key identifies one account only before 2017 and only when its parts are present."""
    return isinstance(hk, str) and "||" not in hk and not hk.endswith("|") and pd.Timestamp(ts) < pd.Timestamp(REF_BEFORE)


class SequenceRule:
    """Single-card sequence (R5): >= min_tests small test_channel transactions within test_window_min, followed by a larger
    purchase within follow_gap_h. fire(card_history_df, anchor) is the pure detector; arrays() + fire_arrays() is the same
    code on pre-sorted numpy arrays (the backtest evaluates 200,000 anchors)."""

    kind = "single_card_sequence"

    def __init__(self, spec: Spec):
        self.spec, self.params = spec, dict(spec.params)

    @staticmethod
    def arrays(h: pd.DataFrame) -> dict:
        h = h.sort_values(["ts", "TransactionID"])
        return {"ts": h.ts.values.astype("datetime64[ns]").astype("int64"), "amt": h.amt.values.astype(float),
                "online": (h.channel.values == "online"), "tid": h.TransactionID.values.astype("int64")}

    def fire(self, card_history_df: pd.DataFrame, anchor, until=None, params: dict | None = None) -> dict | None:
        p = {**self.params, **(params or {})}
        h = card_history_df
        if p.get("unit") == "holder":
            hk = anchor.get("holder_key")
            if valid_account(hk, anchor["ts"]) and "holder_key" in h:
                h = h[h.holder_key == hk]
        return self.fire_arrays(self.arrays(h), anchor, until, params)

    def fire_arrays(self, A: dict, anchor, until=None, params: dict | None = None) -> dict | None:
        p = {**self.params, **(params or {})}
        t0, tid0 = _ns(anchor["ts"]), int(anchor["TransactionID"])
        S, n, W = float(p["test_max_amt"]), int(p["min_tests"]), int(p["test_window_min"]) * NS_MIN
        gap = int(p["follow_gap_h"] * NS_HOUR)
        if p["scope"] == "member":
            lo, hi = t0 - W - gap, t0 + W + gap
        else:
            lo, hi = t0 - int(p["scope"] * NS_HOUR), t0 + int(p["scope"] * NS_HOUR)
        if until is not None:
            hi = min(hi, _ns(until))
        ts, amt = A["ts"], A["amt"]
        i0, i1 = int(np.searchsorted(ts, lo, "left")), int(np.searchsorted(ts, hi, "right"))
        if i1 - i0 < n + 1:
            return None
        sl = slice(i0, i1)
        t_s, a_s, on_s, id_s = ts[sl], amt[sl], A["online"][sl], A["tid"][sl]
        test_ok = (a_s < S) & (on_s if p.get("test_channel") == "online" else (~on_s if p.get("test_channel") == "in_person" else True))
        small = np.nonzero(test_ok)[0]
        if len(small) < n:
            return None
        ts_small = t_s[small]
        for k in range(n - 1, len(small)):
            te = ts_small[k]
            run = small[(ts_small >= te - W) & (ts_small <= te)]
            if len(run) < n:
                continue
            after = np.arange(small[k] + 1, len(t_s))
            after = after[(t_s[after] > te) & (t_s[after] <= te + gap)]
            if p["window_binds"] == "sequence":
                after = after[t_s[after] <= t_s[run[0]] + W]
            if p["follow_channel"] == "online":
                after = after[on_s[after]]
            fa = a_s[after]
            rule = p["follow_rule"]
            if rule == "not_small":
                ok = fa >= S
            elif rule == "x5_20":
                ok = fa > max(20.0, 5 * float(a_s[run].max()))
            else:
                ok = fa > float(p["block_over"] or 100.0)
            fol = after[ok]
            if not len(fol):
                continue
            members = [int(x) for x in id_s[run]] + [int(id_s[fol[0]])]
            if p["scope"] == "member" and tid0 not in members:
                continue
            over = after[fa > float(p["block_over"])] if p.get("block_over") else after[:0]
            cleared = bool(len(over))
            acts = list(self.spec.consequence["actions"])
            for b in self.spec.consequence["branches"]:
                if b["money"] and cleared:
                    acts += [a for a in b["actions"] if a not in acts]
            return {"tests": [str(int(x)) for x in id_s[run]], "test_amts": [round(float(x), 2) for x in a_s[run]],
                    "follow": str(int(id_s[fol[0]])), "follow_amt": round(float(a_s[fol[0]]), 2),
                    "run_start": str(pd.Timestamp(int(t_s[run[0]]))), "run_span_min": round((te - int(t_s[run[0]])) / NS_MIN, 1),
                    "gap_min": round((int(t_s[fol[0]]) - te) / NS_MIN, 1), "cleared_over_branch": cleared,
                    "over_branch_ids": [str(int(x)) for x in id_s[over]], "actions": acts,
                    "txns": [str(m) for m in members]}
        return None

    def diagnose(self, A: dict, anchor, until=None, hours: float = 72) -> dict:
        """Why the rule did or did not fire: the most small transactions in any test window within +-hours, and whether a
        purchase at or above the small threshold follows that run within the gap (for the one-line disagreement reasons)."""
        p = self.params
        t0 = _ns(anchor["ts"])
        lo, hi = t0 - int(hours * NS_HOUR), t0 + int(hours * NS_HOUR)
        if until is not None:
            hi = min(hi, _ns(until))
        ts = A["ts"]
        i0, i1 = int(np.searchsorted(ts, lo, "left")), int(np.searchsorted(ts, hi, "right"))
        t_s, a_s, on_s = ts[i0:i1], A["amt"][i0:i1], A["online"][i0:i1]
        small = np.nonzero((a_s < float(p["test_max_amt"])) & on_s)[0]
        W = int(p["test_window_min"]) * NS_MIN
        best, best_end = 0, None
        for k in range(len(small)):
            c = int(((t_s[small] >= t_s[small[k]] - W) & (t_s[small] <= t_s[small[k]])).sum())
            if c > best:
                best, best_end = c, int(t_s[small[k]])
        span_days = (int(t_s[small[-1]]) - int(t_s[small[0]])) / NS_DAY if len(small) > 1 else 0.0
        return {"n_small": int(len(small)), "max_small_in_window": best, "span_days": round(span_days, 1),
                "small_hours_from_anchor": None if best_end is None else round((best_end - t0) / NS_HOUR, 1)}


class SharedElementRule:
    """Cross-card shared element (R6): at least min_cards cards (the alert's included) show fraud from one value of a named
    field within +-window_days of the alert, and the fraud-showing cards are at least baseline_ratio times the value's own
    baseline. Facts come from an ElementIndex (one pass over the DuckDB index); fire() is pure over them."""

    kind = "cross_card_shared_element"

    def __init__(self, spec: Spec):
        self.spec, self.params = spec, dict(spec.params)

    def fire(self, card_history_df: pd.DataFrame | None, anchor, index: "ElementIndex", until=None, own_case: str | None = None,
             params: dict | None = None) -> dict | None:
        """All elements of the anchor that fire, rarest first (None when none does). card_history_df may be None; it is only
        used to read the anchor's own row when the anchor dict lacks the entity columns."""
        p = {**self.params, **(params or {})}
        a = dict(anchor)
        if card_history_df is not None and any(f not in a for f in p["fields"]):
            row = card_history_df[card_history_df.TransactionID == int(a["TransactionID"])]
            if len(row):
                a = {**row.iloc[0].to_dict(), **a}
        t0 = _ns(a["ts"])
        cut = _ns(until) if until is not None else t0 + index.median_delay_ns
        hits = []
        for f in p["fields"]:
            v = index.value_of(f, a, p)
            if v is None:
                continue
            h = index.evaluate(f, v, str(a["card_id"]), t0, cut, own_case, p)
            if h:
                hits.append(h)
        if not hits:
            return None
        hits.sort(key=lambda h: (h["baseline"] if h["baseline"] is not None else -1, -h["n_cards"]))
        primary = hits[0]
        acts = list(self.spec.consequence["actions"])
        return {**primary, "elements": hits, "actions": acts,
                "shared_element": f"{FIELD_LABEL[primary['field']]} {primary['value']}",
                "txns": primary["txns"], "cards": primary["cards"]}


FIELD_LABEL = {"device_key": "device profile", "addr1": "billing region", "R_emaildomain": "recipient email domain",
               "P_emaildomain": "purchaser email domain"}


class ElementIndex:
    """Every transaction with the element columns, the card-relative atypical flags of kavach.duckq_ext.card_atypical_window
    (the agent's tool, run once over all cards) and the earliest confirmed-fraud closed case containing it."""

    STRONG = ("product_unseen", "amt_above_prior_max", "amt_above_product_max", "proxy_unseen")

    def __init__(self, frame: pd.DataFrame, data_start, median_delay_ns: int, sql: dict):
        f = frame.sort_values(["ts", "TransactionID"]).reset_index(drop=True)
        self.frame = f
        self.data_start = _ns(data_start)
        self.median_delay_ns = int(median_delay_ns)
        self.sql = sql
        self.cards = pd.Index(sorted(f.card_id.unique()))
        f["_card"] = self.cards.get_indexer(f.card_id)
        self._by = {}
        for fld in ("device_key", "addr1", "R_emaildomain", "P_emaildomain"):
            col = f[fld]
            ok = col.notna().values
            if fld == "addr1":
                txt = np.where(ok, col.fillna(-1).astype(float).astype("int64").astype(str), None)
            else:
                txt = np.where(ok, col.astype(str), None)
            # positions into f (the frame is sorted by time, so each value's positions are in time order)
            self._by[fld] = pd.Series(txt).groupby(pd.Series(txt), sort=False, dropna=True).indices
        self._pop = {}
        self._base = {}
        self._vals = {}
        self._arr = {k: f[k].values for k in ("TransactionID", "_card", "channel", "strong", "device_new", "prec_ns")}
        self._arr["ts"] = f.ts.values.astype("datetime64[ns]").astype("int64")
        self._prec_case = f.prec_case.values

    # ---------------------------------------------------------------- construction
    @classmethod
    def build(cls) -> "ElementIndex":
        """One pass over the DuckDB index behind kavach.duckq (tests point duckq._con at a synthetic connection)."""
        from kavach import duckq, duckq_ext
        q = duckq.q
        cards = list(q("select distinct card_id from txn order by 1").card_id)
        rng = q("select min(ts) lo, max(ts) hi from txn")
        lo, hi = pd.Timestamp(rng.lo.iloc[0]), pd.Timestamp(rng.hi.iloc[0])
        at = duckq_ext.card_atypical_window(cards, lo, hi)
        prec_sql = """select ct.TransactionID, min(cast(c.opened_at as timestamp)) prec_opened, arg_min(ct.case_id, cast(c.opened_at as timestamp)) prec_case
                      from closed_txn ct join closed c using (case_id) where ct.outcome = 'confirmed_fraud' group by 1"""
        prec = q(prec_sql)
        delay_sql = """select median(epoch(cast(c.opened_at as timestamp)) - epoch(t.ts)) d from closed c join txn t
                       on t.TransactionID = try_cast(try_cast(c.first_fraud_txn_id as double) as bigint) where c.outcome = 'confirmed_fraud'"""
        dly = q(delay_sql).d.iloc[0]
        dly = 0.0 if dly is None or (isinstance(dly, float) and math.isnan(dly)) else float(dly)
        f = at.merge(prec, on="TransactionID", how="left")
        f["strong"] = f[list(cls.STRONG)].fillna(False).astype(bool).any(axis=1)
        f["device_new"] = f.device_new.fillna(False).astype(bool)
        f["prec_ns"] = pd.to_datetime(f.prec_opened).values.astype("datetime64[ns]").astype("int64")
        f.loc[f.prec_opened.isna(), "prec_ns"] = np.iinfo("int64").max
        f["prec_case"] = f.prec_case.fillna("")
        return cls(f[["TransactionID", "card_id", "ts", "amt", "ProductCD", "channel", "addr1", "R_emaildomain", "P_emaildomain",
                      "device_key", "strong", "device_new", "prec_ns", "prec_case"]], lo, int(dly * 1e9),
                   {"atypical": "kavach.duckq_ext.card_atypical_window(all cards, data start, data end)", "precedent": " ".join(prec_sql.split()),
                    "median_delay": " ".join(delay_sql.split()), "median_delay_hours": round(dly / 3600, 2)})

    # ---------------------------------------------------------------- lookups
    def value_of(self, f: str, a: dict, p: dict):
        v = a.get(f)
        if not _ok(v):
            return None
        if f == "addr1":
            if p.get("region_channel") == "in_person" and a.get("channel") != "in_person":
                return None
            return str(int(float(v)))
        v = str(v)
        if f == "device_key":
            from kavach.detectors.network import generic_device
            if generic_device(v, self.popularity(f, v)):
                return None
        return v

    def popularity(self, f: str, v: str) -> int:
        k = (f, v)
        if k not in self._pop:
            idx, _ = self.rows(f, v)
            self._pop[k] = int(len(np.unique(self._arr["_card"][idx])))
        return self._pop[k]

    def rows(self, f: str, v: str):
        """(positions, their timestamps) of every transaction carrying the value, in time order (memoised)."""
        k = (f, v)
        if k not in self._vals:
            idx = self._by[f].get(v)
            idx = np.array([], dtype="int64") if idx is None else np.asarray(idx, dtype="int64")
            self._vals[k] = (idx, self._arr["ts"][idx])
        return self._vals[k]

    def _show(self, idx, evidence: str, cut: int, own_case: str | None):
        a = self._arr
        atyp = a["strong"][idx] | (a["device_new"][idx] if evidence == "atypical_or_new" else False)
        prec = (a["prec_ns"][idx] <= cut)
        if own_case:
            prec = prec & (self._prec_case[idx] != own_case)
        if evidence == "atypical" or evidence == "atypical_or_new":
            return atyp
        if evidence == "precedent":
            return prec
        return atyp | prec

    def baseline(self, f: str, v: str, t0: int, p: dict):
        """Mean distinct cards showing fraud (atypical evidence; precedent dated before the window) from the value per window
        length over the 90 days before the window; day-aligned and memoised. None when no complete window lies in the data."""
        W = int(p["window_days"] * NS_DAY)
        day = (t0 // NS_DAY) * NS_DAY
        key = (f, v, day, p["window_days"], p["evidence"], p.get("region_channel"))
        if key in self._base:
            return self._base[key]
        span = 2 * W
        lo_w = day - W
        n_b = max(1, int(p["baseline_days"] * NS_DAY // span))
        b0 = lo_w - n_b * span
        complete = [i for i in range(n_b) if b0 + i * span >= self.data_start]
        if not complete:
            self._base[key] = None
            return None
        idx, ts = self.rows(f, v)
        sel = idx[int(np.searchsorted(ts, b0, "left")):int(np.searchsorted(ts, lo_w, "left"))]
        if p.get("region_channel") == "in_person" and f == "addr1":
            sel = sel[self._arr["channel"][sel] == "in_person"]
        show = self._show(sel, p["evidence"], lo_w, None)
        sel = sel[show]
        if not len(sel):
            self._base[key] = 0.0
            return 0.0
        b = (self._arr["ts"][sel] - b0) // span
        pairs = np.unique(b * (len(self.cards) + 1) + self._arr["_card"][sel])
        per = np.bincount((pairs // (len(self.cards) + 1)).astype(int), minlength=n_b)
        val = float(sum(per[i] for i in complete) / len(complete))
        self._base[key] = val
        return val

    def evaluate(self, f: str, v: str, card: str, t0: int, cut: int, own_case: str | None, p: dict) -> dict | None:
        W = int(p["window_days"] * NS_DAY)
        idx, ts = self.rows(f, v)
        if not len(idx):
            return None
        lo, hi = t0 - W, min(t0 + W, cut)
        win = idx[int(np.searchsorted(ts, lo, "left")):int(np.searchsorted(ts, hi, "right"))]
        if f == "addr1" and p.get("region_channel") == "in_person":
            win = win[self._arr["channel"][win] == "in_person"]
        if not len(win):
            return None
        show = self._show(win, p["evidence"], cut, own_case)
        own = self.cards.get_loc(card) if card in self.cards else -1
        fr = win[show & (self._arr["_card"][win] != own)]
        other = np.unique(self._arr["_card"][fr])
        n_cards = len(other) + 1
        if n_cards < p["min_cards"]:
            return None
        n_sharing = int(len(np.unique(self._arr["_card"][win])))
        base = self.baseline(f, v, t0, p)
        if p["baseline_ratio"] > 0:
            if base is None:
                if n_sharing > p["rare_cards"]:
                    return None
            elif n_cards < p["baseline_ratio"] * max(base, 1.0 / p["baseline_ratio"]):
                return None
        cards = [str(self.cards[i]) for i in other]
        prec_cases = sorted({c for c in self._prec_case[fr] if c and c != own_case})
        sharing = [str(self.cards[i]) for i in np.unique(self._arr["_card"][win]) if i != own] if n_sharing <= p["rare_cards"] else []
        return {"field": f, "value": v, "label": FIELD_LABEL[f], "n_cards": n_cards, "cards": cards[:60], "n_other": len(other),
                "txns": [str(int(x)) for x in self._arr["TransactionID"][fr]][:60], "baseline": None if base is None else round(base, 3),
                "n_sharing": n_sharing, "monitor_cards": sharing[:200] if sharing else cards[:200],
                "monitor_rule": "every card sharing the element (rare element)" if sharing else "the fraud-showing cards (common element)",
                "precedent_cases": prec_cases[:20], "window": [str(pd.Timestamp(lo)), str(pd.Timestamp(hi))]}


# ================================================================================================ backtest
def _labels_and_rows():
    from kavach import backtest as bt
    from kavach import duckq
    lab = bt.labels()
    opened = duckq.q("select case_id, cast(opened_at as timestamp) opened_at from closed")
    lab = lab.merge(opened, on="case_id", how="left")
    rows = bt.anchor_rows(lab.tid.unique())
    return lab, rows


def _variants(spec: Spec) -> list[dict]:
    """Default point plus one variant per alternative option of every choice (one choice changed at a time)."""
    out = [{"name": "default", "choice": "", "option": "as chosen", "params": {}}]
    for c in spec.choices:
        for o in c.options[1:]:
            if o["params"]:
                out.append({"name": f"{c.id}: {o['label']}", "choice": c.id, "option": o["label"], "params": o["params"]})
    return out


def _all_card_arrays(rule: SequenceRule) -> tuple[dict, dict]:
    """Sorted arrays per card id, and per account (holder_key, rows before 2017) for the account reading."""
    from kavach import duckq
    t = duckq.q("select TransactionID, card_id, holder_key, ts, TransactionAmt amt, channel from txn order by card_id, ts, TransactionID")
    cards = {card: rule.arrays(g) for card, g in t.groupby("card_id", sort=False)}
    hk = t[(t.ts < pd.Timestamp(REF_BEFORE)) & t.holder_key.notna()]
    hk = hk[~hk.holder_key.str.contains("||", regex=False) & ~hk.holder_key.str.endswith("|")]
    holders = {k: rule.arrays(g) for k, g in hk.groupby("holder_key", sort=False)}
    return cards, holders


def _pick(arrays: tuple, a: dict, p: dict):
    cards, holders = arrays
    if p.get("unit") == "holder" and valid_account(a.get("holder_key"), a["ts"]):
        return holders.get(a["holder_key"], cards.get(a["card_id"]))
    return cards.get(a["card_id"])


def _summ(name, variant, fired_closed: dict, fired_pop: dict, lab, n_pop):
    from kavach.backtest import lift_ci95
    fr, cl = lab[lab.outcome == "confirmed_fraud"], lab[lab.outcome == "cleared"]
    f_hits = sum(1 for t in fr.tid if fired_closed.get(int(t)))
    c_hits = sum(1 for t in cl.tid if fired_closed.get(int(t)))
    p_hits = sum(1 for v in fired_pop.values() if v)
    nf, nc = len(fr), len(cl)
    pf, pp = (f_hits / nf if nf else 0.0), (p_hits / n_pop if n_pop else 0.0)
    lift = (pf / max(pp, 0.5 / n_pop)) if f_hits else 0.0
    return {"variant": variant["name"], "choice": variant["choice"], "option": variant["option"], "params": variant["params"],
            "support": f_hits, "cleared_hits": c_hits, "pop_hits": p_hits, "n_fraud": nf, "n_cleared": nc, "n_population": n_pop,
            "p_fraud": round(pf, 6), "p_cleared": round(c_hits / nc, 6) if nc else 0.0, "p_pop": round(pp, 7), "lift": round(lift, 3),
            "lift_ci95": lift_ci95(f_hits, nf, p_hits, n_pop),
            "lift_note": "population had no hits; lift uses 0.5 hits as the floor (an upper reading)" if f_hits and not p_hits else "",
            "precision_closed": round(f_hits / (f_hits + c_hits), 3) if f_hits + c_hits else None}


def backtest(rule, pop_n: int, log=print, index: "ElementIndex | None" = None) -> dict:
    """Closed cases + reference sample, every variant. Returns {results, fired (default, per closed case), population ids}."""
    from kavach import backtest as bt
    t_start = time.perf_counter()
    lab, rows = _labels_and_rows()
    rowmap = {int(r["TransactionID"]): r for r in rows.to_dict("records")}
    pop_t = population(pop_n) if pop_n else []
    prow = bt.anchor_rows(pop_t) if pop_t else rows.iloc[0:0]
    pop_recs = prow.to_dict("records")
    variants = _variants(rule.spec)
    fired_c = {i: {} for i in range(len(variants))}
    fired_p = {i: {} for i in range(len(variants))}
    hits_default = {}
    if isinstance(rule, SequenceRule):
        arrays = _all_card_arrays(rule)
        dly = index.median_delay_ns if index is not None else _median_delay_ns()
        for r in lab.itertuples():
            a = rowmap.get(int(r.tid))
            if a is None:
                continue
            until = r.opened_at if pd.notna(r.opened_at) else pd.Timestamp(a["ts"]) + pd.Timedelta(dly, "ns")
            for i, v in enumerate(variants):
                h = rule.fire_arrays(_pick(arrays, a, {**rule.params, **v["params"]}), a, until, v["params"])
                fired_c[i][int(r.tid)] = bool(h)
                if i == 0 and h:
                    hits_default[int(r.tid)] = h
        log(f"  [{rule.spec.rule_id}] closed cases done ({time.perf_counter() - t_start:.0f}s)")
        for a in pop_recs:
            until = pd.Timestamp(a["ts"]) + pd.Timedelta(dly, "ns")
            for i, v in enumerate(variants):
                fired_p[i][int(a["TransactionID"])] = bool(rule.fire_arrays(_pick(arrays, a, {**rule.params, **v["params"]}), a, until,
                                                                            v["params"]))
    else:
        for r in lab.itertuples():
            a = rowmap.get(int(r.tid))
            if a is None:
                continue
            until = r.opened_at if pd.notna(r.opened_at) else None
            for i, v in enumerate(variants):
                h = rule.fire(None, a, index, until=until, own_case=r.case_id, params=v["params"])
                fired_c[i][int(r.tid)] = bool(h)
                if i == 0 and h:
                    hits_default[int(r.tid)] = h
        log(f"  [{rule.spec.rule_id}] closed cases done ({time.perf_counter() - t_start:.0f}s)")
        for k, a in enumerate(pop_recs):
            for i, v in enumerate(variants):
                fired_p[i][int(a["TransactionID"])] = bool(rule.fire(None, a, index, until=None, params=v["params"]))
            if k and k % 5000 == 0:
                log(f"  [{rule.spec.rule_id}] population {k}/{len(pop_recs)} ({time.perf_counter() - t_start:.0f}s)")
    n_pop = max(len(pop_recs), 1)
    results = [_summ(rule.spec.rule_id, v, fired_c[i], fired_p[i], lab, n_pop) for i, v in enumerate(variants)]
    pop_fire = [str(t) for t, f in fired_p[0].items() if f][:25]
    return {"results": results, "hits": hits_default, "labels": lab, "rows": rowmap, "population_fired_sample": pop_fire,
            "n_population": len(pop_recs), "seconds": round(time.perf_counter() - t_start, 1)}


def population(n: int, seed: int = 42) -> list[int]:
    """kavach.backtest's reference population (transactions before 2017 in no confirmed-fraud closed case), sampled by a
    seeded hash instead of DuckDB's reservoir so that a re-run returns the same ids (the attestation re-runs it)."""
    from kavach import duckq
    df = duckq.q(f"""select TransactionID from txn where ts < ? and TransactionID not in
                     (select TransactionID from closed_txn where outcome = 'confirmed_fraud')
                     order by hash(TransactionID + {int(seed)}), TransactionID limit {int(n)}""", REF_BEFORE)
    return sorted(int(x) for x in df.TransactionID)


def _median_delay_ns() -> int:
    from kavach import duckq
    d = duckq.q("""select median(epoch(cast(c.opened_at as timestamp)) - epoch(t.ts)) d from closed c join txn t
                   on t.TransactionID = try_cast(try_cast(c.first_fraud_txn_id as double) as bigint) where c.outcome = 'confirmed_fraud'""").d.iloc[0]
    return int(float(d) * 1e9)


# ================================================================================================ the agent's judgement
def agent_answers(refresh: bool = False, case_ids=None, log=print) -> dict:
    """The agent's own answers on the Round 2 pack: a dry run inside the harness (KAVACH_NO_LLM=1, KAVACH_RECORD=0, nothing
    written to cases/ or the case files), cached in logs/agent_dryrun_r2.json with the run time and dataset version. Each
    entry keeps the answer and the signals that name other cards (with the query that produced them)."""
    from kavach import duckq
    dv = duckq.dataset_version().get("version", "unknown")
    if AGENT_CACHE.exists() and not refresh:
        c = json.loads(AGENT_CACHE.read_text(encoding="utf-8"))
        if c.get("dataset_version") == dv and (case_ids is None or all(x in c["cases"] for x in case_ids)):
            return c
    old = {k: os.environ.get(k) for k in ("KAVACH_NO_LLM", "KAVACH_RECORD")}
    os.environ["KAVACH_NO_LLM"], os.environ["KAVACH_RECORD"] = "1", "0"
    try:
        from kavach.harness.runner import run_case
        pack = duckq.case_pack(2).to_dict("records")
        out = {"run_at": _now(), "dataset_version": dv, "how": "kavach.harness.runner.run_case(row) with KAVACH_NO_LLM=1, KAVACH_RECORD=0",
               "cases": {}}
        for c in pack:
            if case_ids is not None and c["case_id"] not in case_ids:
                continue
            o = run_case(c, quiet=True)
            if o.answer is None:
                out["cases"][c["case_id"]] = {"incident": (o.incident or {}).get("error", "incident")}
                continue
            sig = []
            for s in (o.trace or {}).get("signals", []):
                cards = [x for x in s.get("entity_ids") or [] if re.fullmatch(r"C\d{5}-K\d+", str(x))]
                if s.get("name") in ("card_testing", "testing_ladder", "device_burst") or cards:
                    sig.append({k: s.get(k) for k in ("name", "group", "llr", "claim", "entity_ids", "how")})
            out["cases"][c["case_id"]] = {"answer": o.answer, "signals": sig, "row": {k: str(v) for k, v in c.items() if k != "trigger_text"}}
            log(f"  agent dry run {c['case_id']}: {o.answer['case']['verdict']} {o.answer['case']['pattern']}")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    AGENT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    AGENT_CACHE.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


class _RefTools:
    """Enough of ToolRegistry for onecard.detect (it only builds refs; no tool call is made by the card-testing branch)."""
    step = 0

    @staticmethod
    def ref(name, **kw):
        from kavach.tools import ToolRegistry
        return ToolRegistry.ref(name, **kw)

    def call(self, name, **kw):
        from kavach import duckq, duckq_ext
        return (getattr(duckq, name, None) or getattr(duckq_ext, name))(**kw)


def agent_card_testing(h_feat: pd.DataFrame, tid: int, opened, closed_hits: pd.DataFrame, case_id: str = "") -> dict:
    """Run the agent's own single-card detector (kavach.detectors.onecard.detect, unchanged) on one transaction and return
    whether it reached the card_testing judgement: its R5 branch (fnd.card_testing) or its multi-day testing ladder
    (fnd.notes['testing_ladder']); pattern_for() labels both card_testing."""
    from kavach.detectors import Findings, onecard
    row = h_feat[h_feat.TransactionID == tid]
    if row.empty:
        return {"fired": False, "branch": "", "error": "anchor not on card"}
    f = row.iloc[0].to_dict()
    t0 = pd.Timestamp(f["ts"])

    def window(hb=48, ha=48):
        return h_feat[(h_feat.ts >= t0 - pd.Timedelta(hours=hb)) & (h_feat.ts <= t0 + pd.Timedelta(hours=ha))]
    # trigger "customer_report": the score branch then reads no calibration table; nothing in the card-testing branch depends on it
    ctx = SimpleNamespace(f=f, h=h_feat, t0=t0, opened=pd.Timestamp(opened) if opened is not None else t0, tools=_RefTools(),
                          case={"case_id": case_id, "risk_score": None}, trigger="customer_report", customer_id=f.get("customer_id"),
                          card_id=f["card_id"], refs={k: f"query:card_history(card_id={f['card_id']})" for k in ("history", "holder", "region", "device")},
                          device=None, device_pop=0,
                          holder_hist=None, closed_hits=closed_hits, dq_notes=[], window=window)
    fnd = Findings()
    try:
        sigs = onecard.detect(ctx, fnd)
    except Exception as e:  # noqa: BLE001  (recorded per case, never silently counted as "did not fire")
        return {"fired": False, "branch": "", "error": f"{type(e).__name__}: {str(e)[:120]}"}
    branch = "card_testing" if fnd.card_testing else ("testing_ladder" if fnd.notes.get("testing_ladder") else "")
    ids = fnd.episode.get("card_testing") or fnd.episode.get("testing_ladder") or []
    return {"fired": bool(branch), "branch": branch, "txns": [str(int(x)) for x in ids][:20],
            "signals": [s.name for s in sigs if s.name in ("card_testing", "testing_ladder", "device_burst")]}


def compare_r5_closed(rule: SequenceRule, bt_out: dict, log=print) -> dict:
    """Rule versus the agent's card-testing detector versus the bank's label, on every closed case."""
    from kavach import duckq
    from kavach.cardfeat import card_features
    lab, rowmap = bt_out["labels"], bt_out["rows"]
    closed = duckq.q("select case_id, card_id, outcome, pattern, opened_at, txn_ids from closed")
    meta = closed.set_index("case_id")
    allc = closed[["card_id", "outcome", "opened_at", "case_id"]].copy()
    rows = []
    t0 = time.perf_counter()
    lab = lab.sort_values("card_id")
    cur_card, hf, A = None, None, None
    for k, r in enumerate(lab.itertuples(), 1):
        a = rowmap.get(int(r.tid))
        if a is None:
            continue
        if a["card_id"] != cur_card:
            cur_card = a["card_id"]
            hf = card_features(duckq.card_history(cur_card))
            A = rule.arrays(hf)
        until = r.opened_at
        hit = bt_out["hits"].get(int(r.tid))
        ch = allc[allc.card_id == cur_card]
        ag = agent_card_testing(hf, int(r.tid), r.opened_at, ch, r.case_id)
        m = meta.loc[r.case_id]
        diag = rule.diagnose(A, a, until)
        rows.append({"case_id": r.case_id, "outcome": r.outcome, "pattern": m.pattern, "card": cur_card, "anchor": str(r.tid),
                     "rule": bool(hit), "agent": ag["fired"], "agent_branch": ag["branch"], "agent_error": ag.get("error", ""),
                     "agent_signals": ag.get("signals", []),
                     "bank_ct": m.pattern == "card_testing", "rule_txns": hit["txns"] if hit else [], "agent_txns": ag.get("txns", []),
                     "rule_hit": hit, "diag": diag})
        if k % 1000 == 0:
            log(f"  [R5] agent comparison {k}/{len(lab)} ({time.perf_counter() - t0:.0f}s)")
    return {"rows": rows, "seconds": round(time.perf_counter() - t0, 1)}


def r5_reason(r: dict, rule: SequenceRule, against: str) -> str:
    """One line on why rule and agent (or bank) disagree on a closed case."""
    d, h, p = r["diag"], r["rule_hit"], rule.params
    if against == "agent":
        if r["rule"] and not r["agent"]:
            if h and h["gap_min"] > 180:
                return (f"rule: {len(h['tests'])} tests then ${h['follow_amt']:,.2f} {h['gap_min'] / 60:.1f} h later; the agent's "
                        "card-testing check looks only 3 hours either side of the flagged transaction")
            if h and abs(pd.Timestamp(h["run_start"]) - pd.Timestamp(rule_anchor_ts(r))) > pd.Timedelta(hours=3):
                return "rule: the test run lies more than 3 hours from the flagged transaction, outside the agent's window"
            if h and h["follow_amt"] <= max(20.0, 5 * max(h["test_amts"])):
                return (f"rule: follow-up ${h['follow_amt']:,.2f} is not small, but under the agent's bar of 5 times the largest "
                        f"test (${max(h['test_amts']):,.2f}) and $20")
            return "rule: tests after the flagged transaction; the agent counts only tests at or before it"
        if r["agent"] and not r["rule"]:
            if r["agent_branch"] == "testing_ladder" and "device_burst" in r.get("agent_signals", []):
                return (f"agent: device-burst ladder (one device profile with 10 or more online purchases in 48 h, a small one before "
                        f"the largest); R5 needs {p['min_tests']} small within {p['test_window_min']} minutes (rule saw at most "
                        f"{d['max_small_in_window']})")
            if r["agent_branch"] == "testing_ladder":
                return (f"agent: 7-day testing ladder on a card with earlier confirmed fraud ({d['n_small']} small online "
                        f"transactions within 72 h, at most {d['max_small_in_window']} in an hour); R5 needs {p['min_tests']} within "
                        f"{p['test_window_min']} minutes")
            if d["max_small_in_window"] < p["min_tests"]:
                return f"agent: its run straddles the rule's ${p['test_max_amt']:,.2f} small threshold (rule finds {d['max_small_in_window']} small in an hour)"
            return "agent: its 5-times bar is met but no purchase at or above the small threshold follows within the rule's gap and cutoff"
        return ""
    # against the bank's label
    if r["bank_ct"] and not r["rule"]:
        if d["max_small_in_window"] < p["min_tests"]:
            return (f"bank: card_testing over days; at most {d['max_small_in_window']} small online transactions in any "
                    f"{p['test_window_min']} minutes within 72 h of the anchor ({d['n_small']} small in that span); R5 needs {p['min_tests']}")
        return "bank: card_testing, but no purchase at or above the small threshold follows the test run within 24 hours before the case opened"
    if r["rule"] and not r["bank_ct"]:
        if r["outcome"] == "cleared":
            return (f"cleared alert: {len(h['tests'])} small online transactions within {h['run_span_min']:.0f} minutes then "
                    f"${h['follow_amt']:,.2f}; the cardholder's own habit, a legitimate customer R5 would act against")
        return f"bank labelled it {r['pattern']}: the R5 shape is present in the episode window but the bank named the dominant pattern"
    return ""


def rule_anchor_ts(r: dict):
    from kavach import duckq
    return duckq.q("select ts from txn where TransactionID = ?", int(r["anchor"])).ts.iloc[0]


def compare_hhf(rule, agent: dict, index: "ElementIndex | None" = None) -> list[dict]:
    """Rule versus the agent's dry-run answer on each HHF case."""
    from kavach import duckq
    out = []
    pack = duckq.case_pack(2).to_dict("records")
    for c in pack:
        cid = c["case_id"]
        ent = agent["cases"].get(cid, {})
        ans = ent.get("answer")
        trow = duckq.q(f"select {duckq.TXN_COLS} from txn where TransactionID = ?", int(float(c["flagged_txn_id"])))
        if not len(trow) or ans is None:
            out.append({"case_id": cid, "error": "flagged transaction or agent answer missing"})
            continue
        a = trow.iloc[0].to_dict()
        card = a["card_id"]
        h = duckq.card_history(card)
        until = pd.Timestamp(c["opened_at"])
        k = ans["case"]
        final = [x["action"] for x in ans["next_best_actions"]["final"]]
        if isinstance(rule, SequenceRule):
            hit = rule.fire(h, a, until)
            diag = rule.diagnose(rule.arrays(h), a, until)
            agent_yes = k["pattern"] == "card_testing"
            sigs = [s["name"] for s in ent.get("signals", [])]
            if bool(hit) == agent_yes:
                why = ("both see the R5 shape" if agent_yes else
                       f"neither: at most {diag['max_small_in_window']} small online transactions in any hour within 72 h")
            elif hit:
                why = (f"rule fires ({len(hit['tests'])} tests, then ${hit['follow_amt']:,.2f} after {hit['gap_min']:.0f} min) but the "
                       f"agent concluded {k['verdict']} / {k['pattern']}" + ("" if "card_testing" in sigs else "; its own check did not fire"))
            else:
                why = (f"agent says card_testing ({', '.join(sigs) or 'pattern vote'}) but the rule finds at most "
                       f"{diag['max_small_in_window']} small online transactions in any hour (the agent's run spans days)")
            out.append({"case_id": cid, "card": card, "flagged": str(int(a["TransactionID"])), "customer": c["customer_id"],
                        "rule": bool(hit), "rule_txns": hit["txns"] if hit else [], "rule_actions": hit["actions"] if hit else [],
                        "agent_verdict": k["verdict"], "agent_pattern": k["pattern"], "agent_actions": final,
                        "agent_txns": k["affected_txn_ids"][:20], "agree": bool(hit) == agent_yes, "why": why})
        else:
            hit = rule.fire(h, a, index, until=until)
            conn = list(k["connected_card_ids"])
            ring_sig = [s for s in ent.get("signals", []) if s.get("group") == "network" or s["name"].startswith("grown:")]
            agent_el = _agent_element(ring_sig)
            agent_yes = bool(conn) or "MONITOR_CONNECTED_CARDS" in final
            rule_cards = set(hit["cards"]) if hit else set()
            overlap = sorted(rule_cards & set(conn))
            if bool(hit) == agent_yes:
                if hit:
                    why = (f"both see a ring: rule on {hit['shared_element']} ({hit['n_cards']} cards, baseline {hit['baseline']}); agent on "
                           f"{agent_el or 'its ring signal'}; {len(overlap)} card(s) in common")
                else:
                    why = "neither sees cards showing fraud from a shared element of the flagged transaction"
            elif hit:
                why = (f"rule fires on {hit['shared_element']} ({hit['n_cards']} cards showing fraud, baseline {hit['baseline']}) but the agent "
                       f"lists no connected or related cards (verdict {k['verdict']})")
            else:
                why = (f"agent links {len(conn)} card(s) through {agent_el or 'a ring signal'}, which is not one of R6's three elements "
                       "(device profile, billing region, recipient email) on the flagged transaction, or its cards do not show fraud "
                       "within 7 days" if conn else f"agent monitors related cards through {agent_el or 'a ring signal'} without listing them")
            out.append({"case_id": cid, "card": card, "flagged": str(int(a["TransactionID"])), "customer": c["customer_id"],
                        "rule": bool(hit), "rule_element": hit["shared_element"] if hit else "", "rule_cards": sorted(rule_cards)[:40],
                        "rule_txns": hit["txns"][:20] if hit else [], "rule_baseline": hit["baseline"] if hit else None,
                        "rule_elements": [f"{e['label']} {e['value']} ({e['n_cards']} cards)" for e in hit["elements"]] if hit else [],
                        "agent_verdict": k["verdict"], "agent_pattern": k["pattern"], "agent_element": agent_el, "agent_connected": conn,
                        "agent_actions": final, "common_cards": overlap, "agree": bool(hit) == agent_yes, "why": why})
    return out


def _agent_element(sigs: list[dict]) -> str:
    for s in sigs:
        prm = (s.get("how") or {}).get("params") or {}
        f = prm.get("entity_field") or prm.get("entity")
        v = prm.get("value")
        if f and v:
            return f"{f} {v}"
        if prm.get("device_key"):
            return f"device_key {prm['device_key']}"
    return ""


# ================================================================================================ report
def _claim(what, evidence: dict, how: dict, dv: str) -> dict:
    return {"what": what, "evidence": {k: v for k, v in evidence.items() if v}, "how": how, "when": {"ts": _now(), "dataset_version": dv}}


def run(rule_id: str, pop_n: int | None = None, with_agent: bool = True, refresh_agent: bool = False, index=None, log=print) -> dict:
    from kavach import duckq
    t_prose = time.perf_counter()
    rule, timing = compile_from_policy(rule_id)
    spec = rule.spec
    dv = duckq.dataset_version().get("version", "unknown")
    log(f"[{rule_id}] compiled from {spec.source} (sha256 {spec.sha256[:12]}) in {timing['total_ms']} ms; {len(spec.choices)} open choices")
    if isinstance(rule, SharedElementRule) and index is None:
        t = time.perf_counter()
        index = ElementIndex.build()
        log(f"[{rule_id}] element index: {len(index.frame):,} transactions in {time.perf_counter() - t:.0f}s")
    # first working fire: the earliest closed confirmed-fraud anchor (a smoke test that the compiled object runs)
    smoke = duckq.q("select card_id, try_cast(try_cast(first_fraud_txn_id as double) as bigint) tid from closed where outcome = "
                    "'confirmed_fraud' and first_fraud_txn_id is not null order by opened_at limit 1")
    a = duckq.q(f"select {duckq.TXN_COLS} from txn where TransactionID = ?", int(smoke.tid.iloc[0])).iloc[0].to_dict()
    _ = rule.fire(duckq.card_history(a["card_id"]), a) if isinstance(rule, SequenceRule) else rule.fire(None, a, index)
    timing["prose_to_first_fire_ms"] = round((time.perf_counter() - t_prose) * 1000, 1)
    n = pop_n if pop_n is not None else (200_000 if isinstance(rule, SequenceRule) else 20_000)
    log(f"[{rule_id}] backtest over the closed cases and a {n:,}-transaction reference sample")
    bt_out = backtest(rule, n, log=log, index=index)
    timing["prose_to_backtest_s"] = round(time.perf_counter() - t_prose, 1)
    res = bt_out["results"]
    d = res[0]
    log(f"[{rule_id}] default: support {d['support']}/{d['n_fraud']}, cleared {d['cleared_hits']}/{d['n_cleared']}, "
        f"population {d['pop_hits']}/{d['n_population']}, lift {d['lift']}")
    lab = bt_out["labels"]
    hits = []
    for r in lab.itertuples():
        h = bt_out["hits"].get(int(r.tid))
        if h:
            hits.append({"case_id": r.case_id, "outcome": r.outcome, "card": r.card_id, "anchor": str(r.tid), "txns": h["txns"][:20],
                         "cards": h.get("cards", [])[:20], "element": h.get("shared_element", ""), "actions": h.get("actions", [])})
    out = {"rule": rule_id, "run_at": _now(), "dataset_version": dv, "spec": spec.to_dict(), "timing": timing,
           "backtest": {"variants": res, "n_population": bt_out["n_population"], "population_fired_sample": bt_out["population_fired_sample"],
                        "seconds": bt_out["seconds"], "fraud_hits": [h for h in hits if h["outcome"] == "confirmed_fraud"],
                        "cleared_hits": [h for h in hits if h["outcome"] == "cleared"],
                        "how": {"function": "kavach.rules.backtest", "params": {"rule": rule_id, "pop_n": n},
                                "anchors": "kavach.backtest.labels() + kavach.rules.population(n, seed 42): transactions before 2017 in no "
                                           "confirmed-fraud case, ordered by hash(TransactionID + 42)"}}}
    if isinstance(rule, SharedElementRule):
        out["element_index"] = index.sql
    # rule versus agent
    if isinstance(rule, SequenceRule):
        t = time.perf_counter()
        cmp = compare_r5_closed(rule, bt_out, log=log)
        rows = cmp["rows"]
        for r in rows:
            r["why_agent"] = r5_reason(r, rule, "agent") if r["rule"] != r["agent"] else ""
            r["why_bank"] = r5_reason(r, rule, "bank") if r["rule"] != r["bank_ct"] else ""
            r.pop("rule_hit", None)
        out["closed_comparison"] = {"rows": rows, "seconds": round(time.perf_counter() - t, 1),
                                    "how": {"function": "kavach.rules.compare_r5_closed",
                                            "agent": "kavach.detectors.onecard.detect on card_features(card_history(card)), closed_hits = the card's closed cases"}}
    else:
        out["closed_comparison"] = r6_closed_comparison(bt_out)
    if with_agent:
        ag = agent_answers(refresh=refresh_agent, log=log)
        out["hhf_comparison"] = {"agent_run_at": ag["run_at"], "agent_how": ag["how"],
                                 "rows": compare_hhf(rule, ag, index)}
    out["claims"] = claims_for(out, dv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{rule_id}.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


def r6_closed_comparison(bt_out: dict) -> dict:
    """R6 against the bank's own ring labels: connected cards listed, pattern undocumented, report filed."""
    from kavach import duckq
    meta = duckq.q("select case_id, outcome, pattern, connected_card_ids, report_filed from closed").set_index("case_id")
    rows = []
    for r in bt_out["labels"].itertuples():
        m = meta.loc[r.case_id]
        h = bt_out["hits"].get(int(r.tid))
        conn = [x for x in str(m.connected_card_ids or "").split("|") if x and x not in ("nan", "None")]
        bank_ring = bool(conn)
        rule = bool(h)
        if rule == bank_ring and not rule:
            continue                      # agreement on "no ring": counted, not listed
        common = sorted(set(h["cards"]) & set(conn)) if h else []
        if rule and bank_ring:
            why = f"both: rule on {h['shared_element']} ({h['n_cards']} cards), bank lists {len(conn)} connected cards, {len(common)} in common"
        elif rule:
            why = (f"rule on {h['shared_element']} ({h['n_cards']} cards showing fraud, baseline {h['baseline']}); the bank lists no connected "
                   + ("cards (a cleared alert: a legitimate customer R6 would act against)" if r.outcome == "cleared" else
                      f"cards and labelled it {m.pattern}"))
        else:
            why = (f"bank lists {len(conn)} connected cards; the rule finds fewer than 3 cards showing fraud from a shared element of the "
                   "anchor within 7 days before the case opened")
        rows.append({"case_id": r.case_id, "outcome": r.outcome, "pattern": m.pattern, "card": r.card_id, "anchor": str(r.tid),
                     "rule": rule, "bank_ring": bank_ring, "rule_element": h["shared_element"] if h else "",
                     "rule_cards": h["cards"][:20] if h else [], "rule_txns": h["txns"][:12] if h else [], "bank_connected": conn[:30],
                     "common_cards": common, "why": why})
    n_bank = int((meta.connected_card_ids.fillna("").astype(str).str.len() > 3).sum())
    return {"rows": rows, "n_bank_ring_cases": n_bank, "how": {"function": "kavach.rules.r6_closed_comparison",
                                                                 "bank": "closed.connected_card_ids non-empty"}}


def claims_for(out: dict, dv: str) -> list[dict]:
    rid = out["rule"]
    cl = []
    d = out["backtest"]["variants"][0]
    how_bt = {"function": "python -m kavach.rules " + rid, "params": out["backtest"]["how"]["params"]}
    cl.append(_claim(f"{rid} as compiled fires on {d['support']} of {d['n_fraud']} confirmed-fraud closed cases, {d['cleared_hits']} of "
                     f"{d['n_cleared']} cleared alerts and {d['pop_hits']} of {d['n_population']} reference transactions (lift {d['lift']})",
                     {"case_ids": [h["case_id"] for h in out["backtest"]["fraud_hits"] + out["backtest"]["cleared_hits"]][:200]},
                     how_bt, dv))
    for h in out["backtest"]["cleared_hits"]:
        cl.append(_claim(f"{rid} would act against cleared customer case {h['case_id']} (card {h['card']})",
                         {"case_ids": [h["case_id"]], "card_ids": [h["card"]] + h["cards"][:10], "txn_ids": h["txns"]},
                         {"function": f"python -m kavach.rules check {rid} {h['case_id']}", "params": {"anchor": h["anchor"]}}, dv))
    for r in out.get("hhf_comparison", {}).get("rows", []):
        if "error" in r:
            continue
        ids = {"case_ids": [r["case_id"]], "card_ids": [r["card"]] + (r.get("rule_cards") or [])[:20] + (r.get("agent_connected") or [])[:20],
               "txn_ids": [r["flagged"]] + (r.get("rule_txns") or [])[:20], "customer_ids": [r["customer"]]}
        cl.append(_claim(f"{rid} on {r['case_id']}: rule {'fires' if r['rule'] else 'does not fire'}, agent "
                         f"{'agrees' if r['agree'] else 'disagrees'} ({r['why']})", ids,
                         {"function": f"python -m kavach.rules check {rid} {r['case_id']}", "params": {"anchor": r["flagged"]}}, dv))
    return cl


def check(rule_id: str, case_id: str, index=None) -> dict:
    """Re-run one claim: the compiled rule on one closed or HHF case (anchor and cutoff as in the backtest)."""
    from kavach import backtest as bt
    from kavach import duckq
    rule, _ = compile_from_policy(rule_id)
    if case_id.startswith("CC-"):
        lab = bt.labels()
        r = lab[lab.case_id == case_id]
        if r.empty:
            raise KeyError(case_id)
        tid = int(r.tid.iloc[0])
        until = pd.Timestamp(duckq.q("select opened_at from closed where case_id = ?", case_id).opened_at.iloc[0])
        own = case_id
    else:
        c = duckq.q("select * from cases where case_id = ?", case_id)
        if c.empty:
            raise KeyError(case_id)
        tid, until, own = int(float(c.flagged_txn_id.iloc[0])), pd.Timestamp(c.opened_at.iloc[0]), None
    a = duckq.q(f"select {duckq.TXN_COLS} from txn where TransactionID = ?", tid).iloc[0].to_dict()
    h = duckq.card_history(a["card_id"])
    if isinstance(rule, SequenceRule):
        hit = rule.fire(h, a, until)
    else:
        hit = rule.fire(h, a, index or ElementIndex.build(), until=until, own_case=own)
    return {"rule": rule_id, "case_id": case_id, "anchor": str(tid), "until": str(until), "fired": bool(hit), "hit": hit,
            "dataset_version": duckq.dataset_version().get("version"), "at": _now()}


# ================================================================================================ document
def _md_choice(c: dict) -> str:
    opts = "; ".join(o["label"] for o in c["options"][1:]) or "none (no alternative reading exists in this schema)"
    meas = ""
    if c.get("measured"):
        m = c["measured"]
        meas = f" Measured: {m['what']} = {m['value']:.2f} (`{m['how']['sql']}` with {m['how']['params']}, {m['when']})."
    ph = f"'{c['phrase']}'" if c["phrase"] else "(the rule is silent)"
    return f"| `{c['id']}` | {ph} | {c['kind']} | {c['chosen']} | {opts} | {c['reason']}{meas} |"


def _variant_table(res: list[dict]) -> list[str]:
    out = ["| reading | fraud hits | cleared hits | reference hits | lift (95% interval) | precision on closed |", "|---|---|---|---|---|---|"]
    for r in res:
        ci = f" ({r['lift_ci95'][0]} to {r['lift_ci95'][1]})" if r.get("lift_ci95") else ""
        out.append(f"| {r['variant']} | {r['support']} / {r['n_fraud']} | {r['cleared_hits']} / {r['n_cleared']} | {r['pop_hits']} / "
                   f"{r['n_population']} | {r['lift']:.2f}{ci}{' *' if r['lift_note'] else ''} | {r['precision_closed'] if r['precision_closed'] is not None else 'n/a'} |")
    return out


def write_doc(outs: dict) -> None:
    from kavach import duckq
    dv = duckq.dataset_version().get("version", "unknown")
    L = ["# Policy text to detector (challenge 3)", "",
         f"Generated by `python -m kavach.rules all` (kavach/rules.py) at {_now()} on dataset version `{dv}`. The rule sentences are "
         "read from the organizer's policy file at run time; nothing about them is pasted into code. Raw results, every hit and every "
         "comparison row: `detectors/rules/<rule>.json`.", "",
         "Pipeline: the sentence is tokenized by a small lexicon (counts, windows, money, size words, channels, card scope, sequence "
         "words, entities, predicates, states, actions, conditions); the tokens pick a detector template (single-card sequence or "
         "cross-card shared element); every gap the prose leaves becomes a recorded choice; the compiled rule object exposes "
         "`fire(card_history_df, anchor)`. The backtest re-runs the detector once per alternative reading, one choice at a time, "
         "so the table below each rule shows what each ambiguity costs.", "",
         "Re-run any claim: `python -m kavach.rules check <R5|R6> <CC-xxxx|HHF-xxx>` (one case) or `python -m kavach.rules <rule>` "
         "(the whole backtest and comparison).", ""]
    for rid, o in outs.items():
        sp, tm, bt_ = o["spec"], o["timing"], o["backtest"]
        L += [f"## {rid}. {sp['title']}", "", f"> {sp['text']}", "",
              f"Source: `{sp['source']}` (sha256 `{sp['sha256'][:16]}`). Template: `{sp['template']}`.", "",
              "### Prose to detector", "",
              f"- Read and parse: {tm['read_ms']} ms; interpretation (including the data-measured choices): {tm['interpret_ms']} ms; "
              f"compile to a working `fire()` and run it on a first closed case: {tm['prose_to_first_fire_ms']} ms; to a full "
              f"backtest: {tm['prose_to_backtest_s']} s.",
              "- Tokens: " + "; ".join(f"`{t['text']}` -> {t['slot']}{' (open)' if t['vague'] else ''}" for t in sp["tokens"]) + ".",
              "- Executable parameters: " + ", ".join(f"`{k}` = {v}" for k, v in sp["params"].items()) + ".",
              "- Consequence: actions " + ", ".join(f"`{a}`" for a in sp["consequence"]["actions"])
              + "".join(f"; if {b['if']}: " + ", ".join(f"`{a}`" for a in b["actions"]) for b in sp["consequence"]["branches"])
              + (f"; outputs: {', '.join(sp['consequence']['outputs'])}" if sp["consequence"]["outputs"] else "")
              + (f"; the last action targets {sp['consequence']['target_of_last_action']}" if sp["consequence"]["target_of_last_action"] else "")
              + ".", "",
              "### What the prose left open, and how it was resolved", "",
              "| choice | words | kind | taken | alternatives measured | why |", "|---|---|---|---|---|---|"]
        L += [_md_choice(c) for c in sp["choices"]]
        L += ["", "### Backtest (closed cases and reference sample, kavach/backtest.py conventions)", ""]
        L += _variant_table(bt_["variants"])
        L += ["", "`*` the reference sample had no hit, so the lift uses 0.5 hits as the floor and is an upper reading.", "",
              f"Population: {bt_['n_population']:,} transactions before 2017 in no confirmed-fraud case (seeded hash sample, seed 42); "
              f"backtest run {bt_['seconds']} s.", ""]
        fh, ch = bt_["fraud_hits"], bt_["cleared_hits"]
        L.append(f"Confirmed-fraud hits ({len(fh)}): " + (", ".join(f"{h['case_id']} (card {h['card']}, txns {', '.join(h['txns'][:6])})" for h in fh[:40]) or "none")
                 + (f", and {len(fh) - 40} more in the JSON" if len(fh) > 40 else "") + ".")
        L.append("")
        L.append(f"Cleared alerts it would have acted against ({len(ch)}): " + (", ".join(f"{h['case_id']} (card {h['card']}, txns {', '.join(h['txns'][:6])})" for h in ch) or "none") + ".")
        L.append("")
        L += _comparison_md(rid, o)
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text("\n".join(L) + "\n", encoding="utf-8")


def _comparison_md(rid: str, o: dict) -> list[str]:
    L = ["### Rule versus agent", ""]
    cc = o.get("closed_comparison", {})
    if rid == "R5" and cc.get("rows"):
        rows = cc["rows"]
        def n(f):
            return sum(1 for r in rows if f(r))
        L += ["Closed cases (one anchor each): the compiled rule, the agent's own card-testing judgement "
              "(kavach.detectors.onecard.detect run unchanged: its R5 branch or its multi-day testing ladder) and the bank's label.", "",
              "| | agent fires | agent silent |", "|---|---|---|",
              f"| rule fires | {n(lambda r: r['rule'] and r['agent'])} | {n(lambda r: r['rule'] and not r['agent'])} |",
              f"| rule silent | {n(lambda r: not r['rule'] and r['agent'])} | {n(lambda r: not r['rule'] and not r['agent'])} |", "",
              "| | bank label card_testing | other label |", "|---|---|---|",
              f"| rule fires | {n(lambda r: r['rule'] and r['bank_ct'])} | {n(lambda r: r['rule'] and not r['bank_ct'])} |",
              f"| rule silent | {n(lambda r: not r['rule'] and r['bank_ct'])} | {n(lambda r: not r['rule'] and not r['bank_ct'])} |", "",
              f"Errors while running the agent's detector: {n(lambda r: bool(r['agent_error']))}.", "",
              "Agreements with the bank's card_testing label (both fire): "
              + (", ".join(f"{r['case_id']} (card {r['card']}, txns {', '.join(r['rule_txns'][:5])})" for r in rows if r["rule"] and r["bank_ct"]) or "none") + ".", "",
              "Every disagreement between rule and agent:", "", "| case | outcome / bank label | card | anchor | rule | agent | rule txns | agent txns | why |",
              "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["rule"] != r["agent"]:
                L.append(f"| {r['case_id']} | {r['outcome']} / {r['pattern']} | {r['card']} | {r['anchor']} | {'fires' if r['rule'] else '-'} | "
                         f"{r['agent_branch'] or '-'} | {', '.join(r['rule_txns'][:5])} | {', '.join(r['agent_txns'][:5])} | {r['why_agent']} |")
        L += ["", "Every disagreement between rule and the bank's card_testing label:", "",
              "| case | outcome / bank label | card | anchor | rule | rule txns | why |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["rule"] != r["bank_ct"]:
                L.append(f"| {r['case_id']} | {r['outcome']} / {r['pattern']} | {r['card']} | {r['anchor']} | {'fires' if r['rule'] else '-'} | "
                         f"{', '.join(r['rule_txns'][:5])} | {r['why_bank']} |")
        L.append("")
    elif rid == "R6" and cc.get("rows") is not None:
        rows = cc["rows"]
        both = [r for r in rows if r["rule"] and r["bank_ring"]]
        L += [f"Closed cases: the bank lists connected cards on {cc['n_bank_ring_cases']} cases. Rule and bank both see a ring on "
              f"{len(both)}; the rule alone on {sum(1 for r in rows if r['rule'] and not r['bank_ring'])} "
              f"(of which {sum(1 for r in rows if r['rule'] and not r['bank_ring'] and r['outcome'] == 'cleared')} cleared alerts); "
              f"the bank alone on {sum(1 for r in rows if r['bank_ring'] and not r['rule'])}. Every listed row:", "",
              "| case | outcome / label | card | anchor | rule element | rule cards | bank connected | common | why |",
              "|---|---|---|---|---|---|---|---|---|"]
        for r in rows:
            L.append(f"| {r['case_id']} | {r['outcome']} / {r['pattern']} | {r['card']} | {r['anchor']} | {r['rule_element'] or '-'} | "
                     f"{', '.join(r['rule_cards'][:6])}{' ...' if len(r['rule_cards']) > 6 else ''} | {len(r['bank_connected'])} | "
                     f"{len(r['common_cards'])} | {r['why']} |")
        L.append("")
    hh = o.get("hhf_comparison", {})
    if hh.get("rows"):
        L += [f"The 18 HHF cases, against the agent's dry-run answers ({hh['agent_how']}, run {hh['agent_run_at']}):", ""]
        if rid == "R5":
            L += ["| case | card | flagged | rule | rule txns | agent verdict / pattern | agent final actions | agree | why |", "|---|---|---|---|---|---|---|---|---|"]
            for r in hh["rows"]:
                if "error" in r:
                    L.append(f"| {r['case_id']} | | | | | | | | {r['error']} |")
                    continue
                L.append(f"| {r['case_id']} | {r['card']} | {r['flagged']} | {'fires' if r['rule'] else '-'} | {', '.join(r['rule_txns'][:6])} | "
                         f"{r['agent_verdict']} / {r['agent_pattern']} | {', '.join(r['agent_actions'])} | {'yes' if r['agree'] else 'NO'} | {r['why']} |")
        else:
            L += ["| case | card | flagged | rule element | rule cards | agent element | agent connected | common | agree | why |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
            for r in hh["rows"]:
                if "error" in r:
                    L.append(f"| {r['case_id']} | | | | | | | | | {r['error']} |")
                    continue
                L.append(f"| {r['case_id']} | {r['card']} | {r['flagged']} | {r['rule_element'] or '-'} | {len(r['rule_cards'])} | "
                         f"{r['agent_element'] or '-'} | {len(r['agent_connected'])} | {len(r['common_cards'])} | {'yes' if r['agree'] else 'NO'} | {r['why']} |")
        L.append("")
    return L


# ================================================================================================ CLI
def _print_spec(spec: Spec) -> None:
    print(f"{spec.rule_id}. {spec.title}  [{spec.template}]  from {spec.source} (sha256 {spec.sha256[:12]})")
    print(f"  text: {spec.text}")
    print("  tokens: " + "; ".join(f"{t.text!r}->{t.slot}{'*' if t.vague else ''}" for t in spec.tokens))
    print("  params: " + json.dumps(spec.params, default=str))
    print("  consequence: " + json.dumps(spec.consequence))
    print("  open choices:")
    for c in spec.choices:
        print(f"    - {c.id} [{c.kind}] {c.phrase!r}: {c.chosen}  (alternatives: {', '.join(o['label'] for o in c.options[1:]) or 'none'})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.rules")
    ap.add_argument("what", nargs="?", default="all", help="R5, R6, all, parse, or check")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--pop", type=int, default=None, help="reference sample size (default 200,000 for R5, 20,000 for R6)")
    ap.add_argument("--no-agent", action="store_true", help="skip the HHF comparison (no dry run)")
    ap.add_argument("--refresh-agent", action="store_true", help="re-run the agent dry run instead of the cached answers")
    a = ap.parse_args(argv)
    if a.what == "parse":
        src = policy_source()
        rules = rules_in_policy(src)
        ids = a.args or ["all"]
        for rid in (sorted(rules, key=lambda x: int(x[1:])) if ids == ["all"] else ids):
            toks = tokenize(rules[rid]["text"])
            tpl = template_of(toks)
            print(f"{rid}: template {tpl or 'none (decision rule; kavach/policy.py)'}; tokens: "
                  + "; ".join(f"{t.text!r}->{t.slot}{'*' if t.vague else ''}" for t in toks))
        return 0
    if a.what == "check":
        rid, cid = a.args[0], a.args[1]
        print(json.dumps(check(rid, cid), indent=1, default=str))
        return 0
    ids = ["R5", "R6"] if a.what == "all" else [a.what]
    outs = {}
    index = None
    for rid in ids:
        rule, _ = compile_from_policy(rid)
        _print_spec(rule.spec)
        if isinstance(rule, SharedElementRule) and index is None:
            index = ElementIndex.build()
        outs[rid] = run(rid, a.pop, with_agent=not a.no_agent, refresh_agent=a.refresh_agent, index=index,
                        log=lambda m: print(m, flush=True))
        print()
        print("\n".join(_variant_table(outs[rid]["backtest"]["variants"])))
        print()
        for line in _comparison_md(rid, outs[rid]):
            if line.startswith("|") or line.startswith("Closed") or line.startswith("The 18"):
                print(line)
    # keep the other rule's last result in the document when only one rule was run
    for rid in ("R5", "R6"):
        if rid not in outs and (OUT_DIR / f"{rid}.json").exists():
            outs[rid] = json.loads((OUT_DIR / f"{rid}.json").read_text(encoding="utf-8"))
    write_doc({k: outs[k] for k in ("R5", "R6") if k in outs})
    print(f"wrote {_rel(DOC)} and {', '.join(_rel(OUT_DIR / f'{k}.json') for k in ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
