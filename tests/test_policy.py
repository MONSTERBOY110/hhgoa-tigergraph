from kavach.answer import route_for
import itertools
import re

from kavach.policy import LEGIT, PolicyInput, final_actions, initial_actions, sar_required, status_for


def acts(lst):
    return [a["action"] for a in lst]


def test_routes():
    assert route_for("DECLINE_TRANSACTION", 10) == "L1"
    assert route_for("BLOCK_CARD", 2500) == "L1"
    assert route_for("BLOCK_CARD", 2500.01) == "L2"
    assert route_for("BLOCK_ALL_CARDS", 1) == "L2"
    assert route_for("FILE_REPORT", 1) == "L2"
    for a in ("ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER",
              "STEP_UP_AUTH", "GENERATE_REPORT", "CREATE_CASE", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"):
        assert route_for(a, 99999) == "auto"


def test_r1_single_signal_verifies_never_blocks():
    x = PolicyInput(p=0.45, verdict="uncertain", pattern="card_not_present_fraud", exposure=300, trigger="risk_score", single_signal=True)
    a = acts(initial_actions(x, will_request=True))
    assert "VERIFY_WITH_CUSTOMER" in a and "BLOCK_CARD" not in a
    assert "CREATE_CASE" in a  # 3a: requesting evidence opens a case


def test_r2_deny_blocks_and_cases():
    x = PolicyInput(p=0.9, verdict="fraud", pattern="card_not_present_fraud", exposure=300, trigger="risk_score", reply="deny")
    a = acts(final_actions(x))
    assert a[:2] == ["BLOCK_CARD", "CREATE_CASE"] and "FILE_REPORT" not in a


def test_r2_report_over_1000_or_shared_device():
    x = PolicyInput(p=0.9, verdict="fraud", pattern="card_not_present_fraud", exposure=1500, trigger="risk_score", reply="deny")
    assert "FILE_REPORT" in acts(final_actions(x))
    y = PolicyInput(p=0.9, verdict="fraud", pattern="card_not_present_new_device", exposure=100, trigger="risk_score",
                    reply="deny", shared_element="device profile X", connected_cards=["C1-K1"])
    a = acts(final_actions(y))
    assert "FILE_REPORT" in a and "MONITOR_CONNECTED_CARDS" in a


def test_r3_confirm_closes():
    x = PolicyInput(p=0.1, verdict="legitimate", pattern="none", exposure=0, trigger="risk_score", reply="confirm")
    a = acts(final_actions(x))
    assert a == list(LEGIT)          # the bank's cleared set, in policy order, never CREATE_CASE


def test_r4_no_reply():
    x = PolicyInput(p=0.5, verdict="uncertain", pattern="card_not_present_fraud", exposure=600, trigger="risk_score", reply="no_reply")
    a = acts(final_actions(x))
    assert {"MONITOR_CARD", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST"} <= set(a)
    x.exposure = 200
    assert "ESCALATE_TO_ANALYST" not in acts(final_actions(x))


def test_r5_card_testing():
    x = PolicyInput(p=0.9, verdict="fraud", pattern="card_testing", exposure=60, trigger="risk_score", card_testing=True)
    a = acts(initial_actions(x, will_request=False))
    assert a[:2] == ["DECLINE_TRANSACTION", "STEP_UP_AUTH"] and "BLOCK_CARD" not in a
    x.cleared_over_100 = True
    assert "BLOCK_CARD" in acts(initial_actions(x, will_request=False))


def test_r6_shared_origin():
    x = PolicyInput(p=0.92, verdict="fraud", pattern="card_not_present_new_device", exposure=400, trigger="analyst_request",
                    shared_element="device profile D", connected_cards=["C2-K1", "C3-K1"])
    a = acts(initial_actions(x, will_request=False))
    assert {"CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS"} <= set(a)


def test_r7_recurring_dispute():
    x = PolicyInput(p=0.1, verdict="legitimate", pattern="none", exposure=0, trigger="customer_report", recurring=True)
    a = acts(initial_actions(x, will_request=True))
    assert set(a) == {"CREATE_CASE", "VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER"}
    x.reply = "recognise_recurring"
    f = acts(final_actions(x))
    assert "BLOCK_CARD" not in f and "CLOSE_NO_FRAUD" in f


def test_r8_uncertain_exposed_escalates():
    x = PolicyInput(p=0.5, verdict="uncertain", pattern="card_not_present_fraud", exposure=900, trigger="risk_score")
    assert "ESCALATE_TO_ANALYST" in acts(initial_actions(x, will_request=False))


def test_r9_undocumented():
    x = PolicyInput(p=0.88, verdict="fraud", pattern="undocumented", exposure=200, trigger="analyst_request",
                    coordinated_undocumented=True)
    a = acts(initial_actions(x, will_request=False))
    assert {"CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"} <= set(a)


def test_r10_block_all_only_with_condition():
    x = PolicyInput(p=0.95, verdict="fraud", pattern="account_takeover", exposure=300, trigger="customer_report", reply="deny")
    assert "BLOCK_ALL_CARDS" not in acts(final_actions(x))
    x.n_cards_confirmed_fraud = 2
    assert "BLOCK_ALL_CARDS" in acts(final_actions(x))


def test_3a_case_only_under_threshold():
    x = PolicyInput(p=0.9, verdict="fraud", pattern="card_not_present_fraud", exposure=200, trigger="customer_report")
    assert sar_required(x)[0] is False
    x.verdict, x.p = "legitimate", 0.1
    assert sar_required(x)[0] is False


def test_ordering_and_dedupe():
    x = PolicyInput(p=0.9, verdict="fraud", pattern="card_testing", exposure=3000, trigger="risk_score", card_testing=True,
                    cleared_over_100=True, shared_element="device D", connected_cards=["C9-K1"])
    a = final_actions(PolicyInput(**{**x.__dict__, "reply": "deny"}))
    names = acts(a)
    assert len(names) == len(set(names))
    assert names.index("BLOCK_CARD") < names.index("CREATE_CASE") < names.index("FILE_REPORT") < names.index("MONITOR_CONNECTED_CARDS")
    assert [r["route"] for r in a if r["action"] == "BLOCK_CARD"] == ["L2"]


def test_status():
    assert status_for("fraud", [{"action": "BLOCK_CARD"}]) == "closed_fraud"
    assert status_for("uncertain", [{"action": "ESCALATE_TO_ANALYST"}]) == "escalated"
    assert status_for("legitimate", [{"action": "CLOSE_NO_FRAUD"}]) == "closed_legitimate"


def test_every_reason_cites_rule():
    import re
    rule = re.compile(r"\bR(10|[1-9])\b|§")
    for reply in ("deny", "confirm", "no_reply", "recognise_recurring"):
        x = PolicyInput(p=0.6, verdict="fraud", pattern="card_not_present_fraud", exposure=1200, trigger="customer_report",
                        reply=reply, shared_element="device D", connected_cards=["C9-K1"], card_testing=True)
        for a in final_actions(x) + initial_actions(x, True) + initial_actions(x, False):
            assert rule.search(a["reason"]), a


def test_legitimate_is_the_bank_cleared_set_for_every_trigger():
    for trig in ("risk_score", "customer_report", "analyst_request"):
        x = PolicyInput(p=0.05, verdict="legitimate", pattern="none", exposure=0, trigger=trig)
        a = initial_actions(x, will_request=False)
        assert acts(a) == ["ALLOW_TRANSACTION", "VERIFY_WITH_CUSTOMER", "MONITOR_CARD", "CLOSE_NO_FRAUD"]
        assert all(re.search(r"R3", r["reason"]) or "§6" in r["reason"] for r in a)
        assert "without a case" in a[-1]["reason"] and "§3a" in a[-1]["reason"]
        x.reply = "confirm"
        assert acts(final_actions(x)) == list(LEGIT)


def test_recurring_close_is_cleared_set_plus_warning():
    x = PolicyInput(p=0.1, verdict="legitimate", pattern="none", exposure=0, trigger="customer_report", recurring=True,
                    reply="recognise_recurring")
    assert acts(final_actions(x)) == ["ALLOW_TRANSACTION", "VERIFY_WITH_CUSTOMER", "MONITOR_CARD", "WARN_CUSTOMER", "CLOSE_NO_FRAUD"]


def test_sar_rule_exposure_or_connected_only():
    base = dict(p=0.95, verdict="fraud", pattern="card_not_present_new_device", trigger="customer_report")
    # a shared element without closed-case precedent (related cards) is monitored but not reported
    x = PolicyInput(exposure=59.67, related_cards=["C1-K1", "C2-K1"], related_element="a fixed-amount template", **base)
    a = acts(final_actions(PolicyInput(**{**x.__dict__, "reply": "deny"})))
    assert "FILE_REPORT" not in a and "MONITOR_CONNECTED_CARDS" in a and sar_required(x)[0] is False
    assert "related" not in sar_required(x)[1] or "not listed" in sar_required(x)[1]
    # linked fraud alone (e.g. a holder with earlier cases) no longer files
    assert sar_required(PolicyInput(exposure=300, linked_fraud=True, **base))[0] is False
    # connected cards (precedent) file under R6, exposure over 1000 files under R2
    f, r = sar_required(PolicyInput(exposure=300, connected_cards=["C9-K1"], shared_element="device D", **base))
    assert f and r.startswith("R6")
    f, r = sar_required(PolicyInput(exposure=2372.80, **base))
    assert f and r.startswith("R2")
    # strongly suspected (p >= 0.70) counts as suspected unless cleared
    assert sar_required(PolicyInput(p=0.75, verdict="uncertain", pattern="none", exposure=1500, trigger="risk_score"))[0]
    assert not sar_required(PolicyInput(p=0.65, verdict="uncertain", pattern="none", exposure=1500, trigger="risk_score"))[0]
    assert not sar_required(PolicyInput(p=0.9, verdict="legitimate", pattern="none", exposure=1500, trigger="risk_score"))[0]


def test_fraud_without_reply_after_request_still_files():
    x = PolicyInput(p=0.9, verdict="fraud", pattern="card_testing", exposure=2896.04, trigger="customer_report", reply="no_reply")
    a = acts(final_actions(x))
    assert {"BLOCK_CARD", "CREATE_CASE", "FILE_REPORT"} <= set(a)


GRID = dict(
    verdict=("fraud", "uncertain", "legitimate"), p=(0.2, 0.6, 0.75, 0.95), exposure=(0.0, 120.0, 700.0, 1500.0, 3000.0),
    trigger=("risk_score", "customer_report", "analyst_request"), reply=(None, "deny", "confirm", "no_reply", "recognise_recurring"),
    pattern=("card_not_present_fraud", "undocumented", "card_testing"), conn=(False, True), related=(False, True),
)
TIPOFF = re.compile(r"\b(sar|suspicious activity report|file[d]? a report|regulator|fincen|reported to the authorit|law enforcement)\b", re.I)


def _cases():
    keys = list(GRID)
    for vals in itertools.product(*GRID.values()):
        g = dict(zip(keys, vals))
        if g["verdict"] == "legitimate" and (g["exposure"] or g["pattern"] != "card_not_present_fraud" or g["conn"] or g["related"]):
            continue
        if g["verdict"] == "fraud" and g["exposure"] == 0:
            continue
        x = PolicyInput(p=g["p"], verdict=g["verdict"], pattern="none" if g["verdict"] == "legitimate" else g["pattern"],
                        exposure=g["exposure"], trigger=g["trigger"], reply=g["reply"],
                        card_testing=g["pattern"] == "card_testing", cleared_over_100=g["exposure"] > 100,
                        coordinated_undocumented=g["pattern"] == "undocumented" and g["verdict"] == "fraud",
                        connected_cards=["C9-K1"] if g["conn"] else [], shared_element="device D" if g["conn"] else "",
                        related_cards=["C8-K1"] if g["related"] else [], related_element="template T" if g["related"] else "",
                        recurring=g["reply"] == "recognise_recurring")
        yield x


def test_grader_rules_hold_on_every_combination():
    n = 0
    for x in _cases():
        if x.reply is None:
            ini = fin = initial_actions(x, will_request=False)
        else:
            ini, fin = initial_actions(x, will_request=True), final_actions(x)
        I, F = set(acts(ini)), set(acts(fin))
        tag = (x.verdict, x.p, x.exposure, x.trigger, x.reply, x.pattern, bool(x.connected_cards))
        if x.verdict == "fraud":
            assert "CREATE_CASE" in F, tag                                          # r3a_no_case
            if x.exposure > 1000 or x.connected_cards:
                assert "FILE_REPORT" in F, tag                                      # r3a_report_required
        if x.pattern == "undocumented" and x.verdict != "legitimate":
            assert {"CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"} <= F, tag  # r9_undocumented
        if x.verdict == "uncertain" and x.exposure > 500:
            assert "ESCALATE_TO_ANALYST" in F, tag                                  # r8_no_escalation
        if F & {"BLOCK_CARD", "BLOCK_ALL_CARDS"} and x.p < 0.70 and x.trigger != "customer_report":
            assert I & {"VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"}, tag                # r1_block_on_weak_signal
        assert "BLOCK_ALL_CARDS" not in F                                           # r10 (no R10 inputs in the grid)
        if x.verdict == "legitimate":
            assert not F & {"BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT", "DECLINE_TRANSACTION"}, tag
        for a in ini + fin:
            assert re.search(r"\bR(10|[1-9])\b|§", a["reason"]), a
            if a["action"] == "WARN_CUSTOMER":
                assert not TIPOFF.search(a["reason"]), a
            assert "\u2014" not in a["reason"] and "\u2013" not in a["reason"]
        n += 1
    assert n > 500
