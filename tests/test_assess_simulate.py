from kavach.assess import Signal, assess, settled
from kavach.simulate import customer_reply


def S(group, llr, name="x"):
    return Signal(name, group, llr, "claim")


def test_strongest_signal_per_group_no_double_counting():
    a = assess([S("device", 1.0), S("device", 0.9), S("device", 0.8)])
    b = assess([S("device", 1.0)])
    assert a.p == b.p


def test_fraud_needs_two_groups():
    one = assess([S("network", 3.0)])
    assert one.p >= 0.9 and one.verdict == "uncertain"
    two = assess([S("network", 2.0), S("sequence", 1.0)])
    assert two.verdict == "fraud"


def test_score_never_counts_as_a_group():
    a = assess([S("score", 3.0), S("sequence", 1.0)])
    assert a.fraud_groups == ["sequence"] and a.verdict == "uncertain"


def test_legit_and_stop_rule():
    a = assess([S("region", -1.5), S("sequence", -1.5)])
    assert a.verdict == "legitimate" and settled(a)
    b = assess([S("region", -2.5)])
    assert b.p <= 0.15 and not settled(b)


def test_probability_clamped():
    assert assess([S("sequence", 20), S("network", 20)]).p == 0.97
    assert assess([S("sequence", -20), S("network", -20)]).p == 0.03


def test_simulated_replies_follow_the_evidence():
    assert customer_reply(0.8, 2, False, "risk_score").kind == "deny"
    assert customer_reply(0.2, 0, False, "risk_score").kind == "confirm"
    assert customer_reply(0.5, 1, False, "risk_score").kind == "no_reply"
    assert customer_reply(0.5, 0, True, "customer_report").kind == "recognise_recurring"
    # a customer who already disputed re-confirms unless the evidence clearly says otherwise
    assert customer_reply(0.5, 0, False, "customer_report").kind == "deny"
    assert customer_reply(0.1, 0, False, "customer_report").kind == "confirm"
    # a single very strong group (e.g. a device ring) is enough to expect a denial
    assert customer_reply(0.6, 1, False, "risk_score", strongest_group=2.4).kind == "deny"


def test_score_never_settles_alone():
    alone = assess([S("score", -3.0)])
    assert alone.legit_groups == [] and not settled(alone)
    with_one = assess([S("score", -2.5), S("device", -0.5)])
    assert with_one.legit_groups == ["device"] and settled(with_one)
