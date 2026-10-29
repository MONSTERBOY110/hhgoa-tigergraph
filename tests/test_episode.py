from types import SimpleNamespace

import pandas as pd

from kavach.detectors import Findings
from kavach.detectors.network import generic_device, promote_rings
from kavach.episode import build, pattern_for


def ctx(channel="online", id_15="New", addr1=264, history_regions=(264,) * 30, t0=pd.Timestamp("2016-12-01 12:00")):
    n = len(history_regions)
    h = pd.DataFrame({
        "TransactionID": list(range(1, n + 1)) + [100, 101],
        "ts": [t0 - pd.Timedelta(days=40 - i) for i in range(n)] + [t0 - pd.Timedelta(minutes=10), t0],
        "amt": [50.0] * n + [480.0, 490.0],
        "addr1": list(history_regions) + [addr1, addr1],
        "device_key": [None] * (n + 2),
        "channel": ["in_person"] * n + [channel, channel],
        "ProductCD": ["W"] * n + ["C", "C"],
        "id_15": [None] * n + [id_15, id_15],
        "risk_score": [0.1] * n + [0.5, 0.5],
        "holder_key": ["1|264|5"] * (n + 2),
    })
    f = h.iloc[-1].to_dict()
    return SimpleNamespace(h=h, f=f, t0=t0, opened=t0 + pd.Timedelta(hours=1), device=None, device_pop=0, card_id="C1-K1")


def test_legitimate_has_empty_episode():
    e = build(ctx(), Findings(), "legitimate")
    assert e.txn_ids == [] and e.first == "" and e.exposure == 0


def test_episode_from_detector_and_exposure():
    fnd = Findings()
    fnd.episode["sub_threshold_burst"] = [100, 101]
    e = build(ctx(), fnd, "fraud")
    assert e.txn_ids == ["100", "101"] and e.first == "100" and e.exposure == 970.0
    assert e.source == "sub_threshold_burst"          # self-delimiting: not extended by the model


def test_model_extension_keeps_flagged_and_lists_candidates():
    c = ctx()
    e = build(c, Findings(), "fraud")
    assert "101" in e.txn_ids and e.source.endswith("+model")
    assert 101 in e.candidates_all and 100 in e.candidates_all
    assert all(int(t) in e.candidates_all for t in e.txn_ids)
    assert e.lookback_days == 3
    # candidates never start before the look-back or end after the case opened
    assert min(e.candidates_all) >= 1 and 1 not in e.candidates_all


def test_long_lookback_for_prior_fraud_account():
    fnd = Findings()
    fnd.notes["prior_fraud_account"] = True
    e = build(ctx(), fnd, "fraud")
    assert e.lookback_days == 30 and len(e.candidates_all) > 2


def test_scope_filters_affected_ids():
    fnd = Findings()
    fnd.episode["sub_threshold_burst"] = [100, 101]
    e = build(ctx(), fnd, "fraud", scope={101})
    assert e.txn_ids == ["101"] and e.first == "101" and e.exposure == 490.0
    assert 100 in e.candidates_all                      # older context is still listed as considered


def test_pattern_rules_follow_the_built_episode():
    assert pattern_for(ctx(), Findings(), "legitimate") == "none"
    assert pattern_for(ctx(id_15="New"), Findings(), "fraud") == "card_not_present_new_device"
    assert pattern_for(ctx(id_15="Found"), Findings(), "fraud") == "card_not_present_fraud"
    # any member marked New makes it the new-device pattern even when the flagged one is Found
    c = ctx(id_15="Found")
    c.h.loc[c.h.TransactionID == 100, "id_15"] = "New"
    fnd = Findings()
    fnd.episode["sub_threshold_burst"] = [100, 101]
    ep = build(c, fnd, "fraud")
    assert pattern_for(c, fnd, "fraud", ep) == "card_not_present_new_device"
    # in person: the FIRST member in the card's modal region reads as account takeover, elsewhere as out-of-region
    assert pattern_for(ctx(channel="in_person", id_15=None, addr1=264), Findings(), "fraud") == "account_takeover"
    assert pattern_for(ctx(channel="in_person", id_15=None, addr1=999), Findings(), "fraud") == "out_of_region_use"
    c = ctx(channel="in_person", id_15=None, addr1=999)
    c.h.loc[c.h.TransactionID == 101, "addr1"] = 264      # flagged at home, but the episode started away
    fnd = Findings()
    fnd.episode["duplicate_amount_burst"] = [100, 101]
    ep = build(c, fnd, "fraud", use_model=False)
    assert pattern_for(c, fnd, "fraud", ep) == "out_of_region_use"
    # mixed channels are account takeover
    c = ctx(channel="online", id_15="Found")
    c.h.loc[c.h.TransactionID == 100, "channel"] = "in_person"
    fnd = Findings()
    fnd.episode["duplicate_amount_burst"] = [100, 101]
    ep = build(c, fnd, "fraud", use_model=False)
    assert pattern_for(c, fnd, "fraud", ep) == "account_takeover"


def test_pattern_priority_undocumented_and_testing():
    f = Findings()
    f.coordinated_undocumented = True
    f.vote("undocumented", 2.6)
    assert pattern_for(ctx(), f, "fraud") == "undocumented"
    assert pattern_for(ctx(), f, "uncertain") != "undocumented"     # R9 report only behind a fraud verdict
    t = Findings()
    t.card_testing = True
    assert pattern_for(ctx(id_15="New"), t, "fraud") == "card_testing"
    t = Findings()
    t.notes["testing_ladder"] = [100, 101]
    assert pattern_for(ctx(id_15="New"), t, "fraud") == "card_testing"


def test_generic_device_profiles_link_nothing():
    assert generic_device(" |  |  | ", 3)
    assert generic_device("Windows |  | chrome 61.0 | ", 3)              # family name, no screen
    assert not generic_device("Windows | other | chrome 61.0 | 1280x720", 5)
    assert generic_device("Windows | other | chrome 61.0 | 1280x720", 40)
    assert generic_device("Trident/7.0 | Windows 7 | ie 11.0 for desktop | ", 2)
    assert not generic_device("SM-G610F Build/NRD90M |  | chrome 66.0 for android | ", 4)
    assert generic_device("SM-G610F Build/NRD90M |  | chrome 66.0 for android | ", 150)


def test_related_cards_need_precedent_to_become_connected():
    f = Findings()
    f.notes["related_cards"] = {"C2-K1": "shares device D", "C3-K1": "shares device D"}
    f.notes["related_element"] = "device profile D"
    promote_rings(f)
    assert f.connected_cards == {} and f.shared_element == ""
    f.linked_fraud = True                         # the device is in confirmed closed cases
    promote_rings(f)
    assert set(f.connected_cards) == {"C2-K1", "C3-K1"} and f.shared_element == "device profile D"
    g = Findings()
    g.coordinated_undocumented = True
    g.notes["template_cards"] = ["C7-K1", "C8-K2"]
    g.notes["threshold"] = 500.0
    promote_rings(g)
    assert set(g.connected_cards) == {"C7-K1", "C8-K2"} and "$500" in g.shared_element
    promote_rings(g)                              # idempotent
    assert len(g.connected_cards) == 2
