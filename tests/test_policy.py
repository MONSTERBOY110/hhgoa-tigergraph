from kavach.answer import route_for
from kavach.policy import PolicyInput, final_actions, initial_actions, sar_required, status_for


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
    assert a[-1] == "CLOSE_NO_FRAUD" and "BLOCK_CARD" not in a and "ALLOW_TRANSACTION" in a


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
