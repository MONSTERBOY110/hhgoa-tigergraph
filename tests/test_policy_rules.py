"""Policy text to detector (kavach/rules.py) on synthetic data: the sentence decides the detector, every gap is a recorded
choice, and the compiled rules fire (and stay silent) where the words say. No dependency on the real index."""
import duckdb
import pandas as pd
import pytest

from kavach import duckq, rules

COLS = ["TransactionID", "card_id", "customer_id", "ts", "TransactionAmt", "ProductCD", "channel", "risk_score", "addr1", "addr2",
        "dist1", "dist2", "P_emaildomain", "R_emaildomain", "device_key", "DeviceType", "id_15", "id_23", "id_31", "M1", "M2", "M3",
        "M4", "M5", "M6", "M7", "M8", "M9", "C1", "C2", "C13", "C14", "D1", "D2", "D3", "D4", "D10", "D15", "holder_key", "card1",
        "card4", "card6"]
NUM = ("addr1", "addr2", "dist1", "dist2", "C1", "C2", "C13", "C14", "D1", "D2", "D3", "D4", "D10", "D15", "card1", "risk_score")

POLICY = """# Policy

| Action | What | Impact |
|---|---|---|
| `DECLINE_TRANSACTION` | x | Low |
| `STEP_UP_AUTH` | x | Low |
| `BLOCK_CARD` | x | High |
| `CREATE_CASE` | x | None |
| `FILE_REPORT` | x | None |
| `MONITOR_CONNECTED_CARDS` | x | None |
| `VERIFY_WITH_CUSTOMER` | x | Low |

**R1. Verify before you block on a weak signal.** If the case rests on a single signal, recommend `VERIFY_WITH_CUSTOMER` before any block.

**R5. Card testing.** Three or more small online authorizations on one card within an hour, followed by a larger purchase: recommend `DECLINE_TRANSACTION` and `STEP_UP_AUTH`. If a purchase over $100 has already cleared, recommend `BLOCK_CARD`.

**R6. Shared origin.** When several cards show fraud from the same device profile, the same billing region, or the same recipient email in one window, name the shared element, recommend `CREATE_CASE` and `FILE_REPORT`, and `MONITOR_CONNECTED_CARDS` for every card that shares it.
"""


class Rows:
    def __init__(self):
        self.rows, self.next = [], 1000000

    def add(self, card, ts, amt, product="W", channel=None, **kw):
        r = {c: None for c in COLS}
        r.update(TransactionID=self.next, card_id=card, customer_id=card.split("-")[0], ts=pd.Timestamp(ts), TransactionAmt=float(amt),
                 ProductCD=product, channel=channel or ("in_person" if product == "W" else "online"), risk_score=0.1, addr1=100.0)
        r.update(kw)
        self.rows.append(r)
        self.next += 1
        return r["TransactionID"]

    def frame(self):
        df = pd.DataFrame(self.rows, columns=COLS)
        for c in NUM:
            df[c] = df[c].astype("float64")
        for c in set(COLS) - set(NUM) - {"TransactionID", "ts", "TransactionAmt"}:
            df[c] = df[c].astype("string")
        return df


def _db(rows: Rows, closed=None, closed_txn=None):
    con = duckdb.connect()
    df = rows.frame()
    con.register("t_df", df)
    con.execute("create table txn as select * from t_df")
    cc = closed if closed is not None else pd.DataFrame(columns=["case_id", "customer_id", "card_id", "opened_at", "closed_at", "outcome",
                                                                 "pattern", "first_fraud_txn_id", "txn_ids", "n_txns", "exposure_usd",
                                                                 "connected_card_ids", "actions_taken", "report_filed", "analyst_notes"])
    ct = closed_txn if closed_txn is not None else pd.DataFrame(columns=["case_id", "customer_id", "card_id", "outcome", "pattern", "TransactionID"])
    cc = cc.astype("string")
    ct = ct.astype({"TransactionID": "int64"}) if len(ct) else ct.astype("string").assign(TransactionID=pd.Series(dtype="int64"))
    con.register("c_df", cc)
    con.register("ct_df", ct)
    con.execute("create table closed as select * from c_df")
    con.execute("create table closed_txn as select * from ct_df")
    return con


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "fraud_policy.md"
    p.write_text(POLICY, encoding="utf-8")
    return {"path": p, "text": POLICY, "sha256": "test"}


@pytest.fixture
def db():
    """A bank whose online amounts put the 5th percentile at a known value, set as kavach.duckq's connection."""
    rows = Rows()
    for i in range(100):                                   # online amounts 1..100: 5th percentile = 5.95
        rows.add("C90000-K1", pd.Timestamp("2016-08-01") + pd.Timedelta(hours=i), i + 1, product="C")
    old = duckq._con
    duckq._con = _db(rows)
    yield rows
    duckq._con = old


def _compile(src, rid, text=None):
    r = rules.rules_in_policy(src)[rid]
    if text is not None:
        r = {**r, "text": text}
    spec = rules.interpret(r, actions_ok=rules.policy_actions(src))
    return rules.compile_rule(spec)


# ------------------------------------------------------------------------------------------------ reading and parsing
def test_rules_and_actions_are_read_from_the_policy_text(src):
    rs = rules.rules_in_policy(src)
    assert set(rs) == {"R1", "R5", "R6"}
    assert rs["R5"]["text"].startswith("Three or more small online authorizations")
    assert "BLOCK_CARD" in rules.policy_actions(src) and "CLOSE_NO_FRAUD" not in rules.policy_actions(src)


def test_r5_tokens_explicit_and_open(src):
    toks = rules.tokenize(rules.rules_in_policy(src)["R5"]["text"])
    by = {(t.slot, t.text): t for t in toks}
    assert by[("count", "Three or more")].value == {"op": ">=", "n": 3} and not by[("count", "Three or more")].vague
    assert by[("window", "within an hour")].value == {"minutes": 60}
    assert by[("money", "over $100")].value == {"op": ">", "usd": 100.0}
    assert by[("size", "small")].vague and by[("size", "larger")].vague and by[("sequence", "followed by")].vague
    assert rules.template_of(toks) == "single_card_sequence"


def test_r5_spec_records_every_open_choice(src, db):
    rule = _compile(src, "R5")
    p = rule.params
    assert (p["min_tests"], p["test_window_min"], p["test_channel"], p["block_over"]) == (3, 60, "online", 100.0)
    assert p["test_max_amt"] == pytest.approx(5.95, abs=0.01)             # measured from the data, not a constant
    ids = {c.id for c in rule.spec.choices}
    assert {"small_amount", "window_binding", "follow_gap", "larger_purchase", "alert_scope", "one_card", "knowledge_cutoff"} <= ids
    small = next(c for c in rule.spec.choices if c.id == "small_amount")
    assert small.measured["how"]["sql"].startswith("select quantile_cont") and small.reason
    assert rule.spec.consequence["actions"] == ["DECLINE_TRANSACTION", "STEP_UP_AUTH"]
    assert rule.spec.consequence["branches"][0]["actions"] == ["BLOCK_CARD"]


def test_changing_the_sentence_changes_the_detector(src, db):
    text = ("Five or more small online authorizations on one card within 10 minutes, followed by a larger purchase: "
            "recommend `DECLINE_TRANSACTION`.")
    rule = _compile(src, "R5", text)
    assert (rule.params["min_tests"], rule.params["test_window_min"]) == (5, 10)
    assert rule.spec.consequence["actions"] == ["DECLINE_TRANSACTION"] and rule.params["block_over"] is None


def test_decision_rules_and_unknown_actions_are_refused(src, db):
    with pytest.raises(rules.NotADetector):
        _compile(src, "R1")
    with pytest.raises(ValueError):
        _compile(src, "R5", "Three or more small online authorizations on one card within an hour, followed by a larger purchase: "
                            "recommend `BLOCK_EVERYTHING`.")


# ------------------------------------------------------------------------------------------------ R5 fires where the words say
def _card(tests_minutes=(0, 10, 20), follow_after_h=2.0, follow_amt=50.0, test_amt=2.0):
    r = Rows()
    t0 = pd.Timestamp("2016-09-01 12:00")
    for i in range(6):
        r.add("C10000-K1", t0 - pd.Timedelta(days=10 - i), 40 + i, product="W")
    ids = [r.add("C10000-K1", t0 + pd.Timedelta(minutes=m), test_amt, product="C") for m in tests_minutes]
    last = t0 + pd.Timedelta(minutes=max(tests_minutes))
    f = r.add("C10000-K1", last + pd.Timedelta(hours=follow_after_h), follow_amt, product="C")
    h = r.frame().rename(columns={"TransactionAmt": "amt"})
    return h, ids, f


def test_r5_fires_on_the_written_shape(src, db):
    rule = _compile(src, "R5")
    h, ids, f = _card()
    anchor = h[h.TransactionID == f].iloc[0].to_dict()
    hit = rule.fire(h, anchor)
    assert hit and hit["tests"] == [str(i) for i in ids] and hit["follow"] == str(f)
    assert hit["actions"] == ["DECLINE_TRANSACTION", "STEP_UP_AUTH"] and not hit["cleared_over_branch"]
    # a cleared purchase over $100 adds the conditional BLOCK_CARD
    h2, _, f2 = _card(follow_amt=150.0)
    hit2 = rule.fire(h2, h2[h2.TransactionID == f2].iloc[0].to_dict())
    assert hit2["actions"][-1] == "BLOCK_CARD" and hit2["over_branch_ids"] == [str(f2)]
    # the array path used by the backtest gives the same answer
    assert rule.fire_arrays(rule.arrays(h), anchor) == hit


@pytest.mark.parametrize("kw", [dict(tests_minutes=(0, 40, 80)),          # three tests, but not within one hour
                                dict(tests_minutes=(0, 10)),               # only two tests
                                dict(follow_after_h=30.0),                 # the larger purchase comes after the 24-hour gap
                                dict(follow_amt=5.0),                      # the follow-up is itself small
                                dict(test_amt=20.0)])                      # the tests are not small
def test_r5_stays_silent_when_a_word_is_not_met(src, db, kw):
    rule = _compile(src, "R5")
    h, ids, f = _card(**kw)
    assert rule.fire(h, h[h.TransactionID == f].iloc[0].to_dict()) is None


def test_r5_respects_the_knowledge_cutoff_and_the_alternatives(src, db):
    rule = _compile(src, "R5")
    h, ids, f = _card(follow_after_h=2.0)
    a = h[h.TransactionID == f].iloc[0].to_dict()
    assert rule.fire(h, a, until=a["ts"] - pd.Timedelta(minutes=1)) is None      # the purchase was not visible yet
    # "within an hour" binding the whole sequence: the purchase 2 hours later no longer qualifies
    assert rule.fire(h, a, params={"window_binds": "sequence"}) is None
    # member scope: an alert on an unrelated transaction two days later does not fire, the 72-hour scope does
    later = h.iloc[0].to_dict()
    later.update(TransactionID=999, ts=a["ts"] + pd.Timedelta(hours=48))
    assert rule.fire(h, later) is not None and rule.fire(h, later, params={"scope": "member"}) is None


def test_variants_change_one_choice_at_a_time(src, db):
    rule = _compile(src, "R5")
    vs = rules._variants(rule.spec)
    assert vs[0]["name"] == "default" and all(len(v["params"]) == 1 for v in vs[1:])
    assert {v["choice"] for v in vs[1:]} >= {"small_amount", "window_binding", "follow_gap", "larger_purchase", "alert_scope", "one_card"}


# ------------------------------------------------------------------------------------------------ R6
def _ring_bank():
    """Four cards with an in-person history that each make a first online product-C purchase paying a rare recipient domain
    in one week (atypical: product unseen); a busy domain paid every week by many cards; an unrelated card."""
    r = Rows()
    t0 = pd.Timestamp("2016-10-20 12:00")
    for k in range(4):
        card = f"C2000{k}-K1"
        for i in range(8):
            r.add(card, t0 - pd.Timedelta(days=60 - i * 5), 30 + i, product="W", addr1=float(200 + k))
        r.add(card, t0 + pd.Timedelta(days=k), 300 + k, product="C", R_emaildomain="ring.example", addr1=float(200 + k))
    for w in range(20):                                      # busy.example: 6 new cards every week, each atypical too
        for j in range(6):
            card = f"C3{w:02d}{j:02d}-K1"
            for i in range(6):
                r.add(card, t0 - pd.Timedelta(days=140 - w * 7 + i), 20, product="W", addr1=300.0)
            r.add(card, t0 - pd.Timedelta(days=130 - w * 7), 99, product="C", R_emaildomain="busy.example", addr1=300.0)
    return r, t0


@pytest.fixture
def ring_db():
    r, t0 = _ring_bank()
    old = duckq._con
    duckq._con = _db(r)
    yield r, t0
    duckq._con = old


def test_r6_tokens_and_choices(src, db):
    rule = _compile(src, "R6")
    assert rule.spec.template == "cross_card_shared_element"
    assert rule.params["fields"] == ["device_key", "addr1", "R_emaildomain"] and rule.params["min_cards"] == 3
    ids = {c.id for c in rule.spec.choices}
    assert {"several", "show_fraud", "window", "common_element", "every_card", "billing_region"} <= ids
    assert rule.spec.consequence["outputs"] == ["shared_element"] and rule.spec.consequence["target_of_last_action"] == "all_sharing"


def test_r6_fires_on_a_rare_shared_recipient_and_not_on_a_busy_one(src, ring_db):
    r, t0 = ring_db
    rule = _compile(src, "R6")
    ix = rules.ElementIndex.build()
    df = r.frame()
    a = df[(df.card_id == "C20003-K1") & (df.R_emaildomain == "ring.example")].iloc[0].to_dict()
    hit = rule.fire(None, a, ix, until=a["ts"] + pd.Timedelta(hours=1))
    assert hit and hit["field"] == "R_emaildomain" and hit["value"] == "ring.example"
    assert sorted(hit["cards"]) == ["C20000-K1", "C20001-K1", "C20002-K1"] and hit["shared_element"] == "recipient email domain ring.example"
    assert hit["actions"] == ["CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS"]
    # the cutoff hides the other cards' later purchases: on the first card's purchase nobody else has shown fraud yet
    first = df[(df.card_id == "C20000-K1") & (df.R_emaildomain == "ring.example")].iloc[0].to_dict()
    assert rule.fire(None, first, ix, until=first["ts"] + pd.Timedelta(hours=1)) is None
    # "show fraud" read as closed-case precedent only: no case exists, so nothing fires
    assert rule.fire(None, a, ix, until=a["ts"] + pd.Timedelta(hours=1), params={"evidence": "precedent"}) is None
    # a busy domain: six atypical cards every week, so this week's six are no spike over its baseline
    b = df[df.R_emaildomain == "busy.example"].iloc[-1].to_dict()
    assert rule.fire(None, b, ix, until=b["ts"] + pd.Timedelta(hours=1)) is None
    assert rule.fire(None, b, ix, until=b["ts"] + pd.Timedelta(hours=1), params={"baseline_ratio": 0.0}) is not None


def test_population_sample_is_repeatable(ring_db):
    a, b = rules.population(50), rules.population(50)
    assert a == b and len(a) == 50
