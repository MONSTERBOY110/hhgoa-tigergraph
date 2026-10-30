"""Containment planner (kavach/containment.py) on a synthetic ring: classification, greedy plan with its stop rule, routes,
named legitimate customers, alternatives, hindsight and the re-run check. No dependency on the real index."""
import pandas as pd
import pytest

from kavach import containment as K
from kavach import duckq
from tests.test_policy_rules import Rows, _db

DEV = "RingPhone X | Android 7.0 | chrome 62.0 | 1080x1920"
AS_OF = pd.Timestamp("2016-11-10 12:00")


def _bank():
    r = Rows()

    def history(card, n=8, amt=30.0):
        for i in range(n):
            r.add(card, pd.Timestamp("2016-08-01") + pd.Timedelta(days=7 * i), amt + i, product="W")

    for c in ("C40000-K1", "C40001-K1", "C40003-K1", "C40004-K1"):
        history(c)
    # A, the case card: two first-ever online C purchases on the ring device (product unseen: atypical)
    r.add("C40000-K1", "2016-11-08 10:00", 400, product="C", device_key=DEV)
    flagged = r.add("C40000-K1", "2016-11-09 10:00", 500, product="C", device_key=DEV)
    # B: larger, own exposure over $2,500 (L2)
    r.add("C40001-K1", "2016-11-07 09:00", 1500, product="C", device_key=DEV)
    r.add("C40001-K1", "2016-11-09 09:00", 1600, product="C", device_key=DEV)
    # C: legitimate collateral, has used this device for months at the same small amounts
    for i in range(8):
        r.add("C40002-K1", pd.Timestamp("2016-08-05") + pd.Timedelta(days=9 * i), 50, product="C", device_key=DEV)
    r.add("C40002-K1", "2016-11-08 15:00", 50, product="C", device_key=DEV)
    # D: the ring reaches it only after the decision
    r.add("C40003-K1", "2016-11-12 08:00", 700, product="C", device_key=DEV)
    # E: compromised early with two tiny charges: forward exposure under the stop rule
    r.add("C40004-K1", "2016-11-01 08:00", 5, product="C", device_key=DEV)
    r.add("C40004-K1", "2016-11-02 08:00", 5, product="C", device_key=DEV)
    # F: carries the rare device once, nothing atypical (product and amounts seen before), never used it before: unclear
    for i in range(8):
        r.add("C40005-K1", pd.Timestamp("2016-08-01") + pd.Timedelta(days=7 * i), 900 if i == 0 else 200, product="C")
    r.add("C40005-K1", "2016-11-02 20:00", 100, product="C", device_key=DEV)
    # an outsider who uses the device too (an element rule would decline them)
    for i in range(6):
        r.add("C49999-K1", pd.Timestamp("2016-08-01") + pd.Timedelta(days=7 * i), 25, product="W")
    r.add("C49999-K1", "2016-11-09 12:00", 60, product="C", device_key=DEV)
    return r, flagged


CARDS = ["C40000-K1", "C40001-K1", "C40002-K1", "C40003-K1", "C40004-K1", "C40005-K1"]


@pytest.fixture
def bank():
    r, flagged = _bank()
    con = _db(r)
    con.execute("""create table cases as select 'TST-001' case_id, '2016-11-10 12:00:00' opened_at, 'analyst_request' trigger_type,
                   ? flagged_txn_id, 'C40000-K1' card_id, 'C40000' customer_id, '0.1' risk_score, 2 round""", [str(flagged)])
    old = duckq._con
    duckq._con = con
    yield r, flagged
    duckq._con = old


def _ring():
    return K.ring_from_dict({"case_id": "TST-001", "cards": CARDS, "element": {"field": "device_key", "value": DEV}, "as_of": AS_OF,
                             "verdict": "fraud", "actions": ["BLOCK_CARD", "CREATE_CASE", "FILE_REPORT"]})


def test_cards_are_classified_from_their_own_history(bank):
    f = K.card_facts(_ring())
    by = {c["card"]: c for c in f["cards"]}
    assert by["C40000-K1"]["class"] == "compromised" and by["C40000-K1"]["realised"] == 900.0
    assert by["C40001-K1"]["class"] == "compromised" and by["C40001-K1"]["realised"] == 3100.0
    assert by["C40002-K1"]["class"] == "likely_legitimate" and by["C40002-K1"]["prior_element_uses"] == 8
    assert by["C40003-K1"]["class"] == "not_yet_active" and by["C40003-K1"]["hindsight"]["element_after"] == 700.0
    assert by["C40004-K1"]["class"] == "compromised" and by["C40004-K1"]["forward"] < K.STOP_USD
    assert by["C40005-K1"]["class"] == "unclear"
    assert {c["customer"] for c in f["cards"]} == {c.split("-")[0] for c in CARDS}
    assert f["first_element_txn"] and f["ring_level"]["outsiders_before"][0]["card"] == "C49999-K1"


def test_plan_is_greedy_stops_and_names_the_legitimate_customer(bank):
    p = K.plan(_ring())
    assert p["blocked"] == ["C40000-K1", "C40001-K1"]
    steps = [s["card"] for s in p["greedy_steps"] if s["taken"]]
    assert steps == ["C40001-K1", "C40000-K1"]                       # largest forward exposure first
    blocks = {a["card"]: a for a in p["actions"] if a["action"] == "BLOCK_CARD"}
    assert blocks["C40001-K1"]["route"] == "L2" and blocks["C40000-K1"]["route"] == "L1"
    assert not any(a["action"] == "BLOCK_ALL_CARDS" for a in p["actions"])
    stepup = {a["card"] for a in p["actions"] if a["action"] == "STEP_UP_AUTH"}
    assert stepup == {"C40004-K1", "C40005-K1"}
    mon = {a["card"] for a in p["actions"] if a["action"] == "MONITOR_CONNECTED_CARDS"}
    assert mon == set(CARDS) - {"C40000-K1", "C40001-K1"}
    legit = p["legitimate_customers_touched"]
    assert [(x["customer"], x["card"]) for x in legit] == [("C40002", "C40002-K1")] and legit[0]["plan_action"].startswith("MONITOR")
    assert not p["staged"]


def test_alternatives_are_scored_with_the_penalty(bank):
    p = K.plan(_ring())
    alt = p["alternatives"]
    assert alt["monitor_all"]["exposure_stopped"] == 0 and alt["monitor_all"]["score"] == 0
    assert alt["block_all"]["legit_customers_acted_against"] == ["C40002"]
    assert alt["block_all"]["unverified_customers_acted_against"] == ["C40005"]          # unclear: penalised like a legitimate one
    assert alt["block_all"]["score"] == pytest.approx(alt["block_all"]["exposure_stopped"] - 2 * K.PENALTY_USD)
    assert alt["plan"]["legit_customers_acted_against"] == [] and alt["plan"]["score"] > alt["block_all"]["score"]
    assert alt["plan"]["exposure_stopped"] + alt["plan"]["residual"] == pytest.approx(p["total_forward_at_risk"])
    er = alt["plan_plus_element_rule"]
    assert er["legit_customers_acted_against"] == ["C49999"] and er["hindsight"]["ring_spend_stopped"] >= 700.0


def test_stop_rule_and_penalty_are_parameters(bank):
    p = K.plan(_ring(), stop_usd=1e9)
    assert p["blocked"] == [] and "below" in p["stop_reason"]
    q = K.plan(_ring(), penalty=0.0)
    assert q["alternatives"]["block_all"]["score"] == q["alternatives"]["block_all"]["exposure_stopped"]


def test_plan_is_attested_and_re_runs_identically(bank):
    p = K.plan(_ring())
    for c in p["claims"]:
        assert c["what"] and c["evidence"] and c["how"]["query"]["sql"].startswith("select") and c["when"]["dataset_version"]
    assert any("C40002" in c["evidence"].get("customer_ids", []) for c in p["claims"])
    assert K.verify(p)["identical"]


def test_ring_from_answer_infers_the_element_and_skips_answers_without_ring(bank):
    r, flagged = bank
    ans = {"case_id": "TST-001", "case": {"connected_card_ids": CARDS[1:], "verdict": "uncertain"},
           "next_best_actions": {"final": [{"action": "ESCALATE_TO_ANALYST"}]}}
    ring = K.ring_from_answer(ans, signals=[])
    assert ring.case_card == "C40000-K1" and ring.element["field"] == "device_key" and ring.element["value"] == DEV
    assert ring.element["source"].startswith("inferred")
    assert K.plan(ring)["staged"]                                   # an uncertain case only stages its blocks
    sig = [{"name": "device_ring", "entity_ids": CARDS[1:4], "how": {"tool": "device_txns", "params": {"device_key": DEV}}}]
    assert K.ring_from_answer(ans, signals=sig).element["source"].startswith("named by the agent")
    assert K.ring_from_answer({**ans, "case": {"connected_card_ids": []}}) is None
