from types import SimpleNamespace

import pandas as pd

from kavach.detectors import Findings
from kavach.episode import build, pattern_for


def ctx(channel="online", id_15="New", addr1=264, history_regions=(264,) * 30):
    t0 = pd.Timestamp("2016-12-01 12:00")
    h = pd.DataFrame({
        "TransactionID": list(range(1, len(history_regions) + 1)) + [100, 101],
        "ts": [t0 - pd.Timedelta(days=40 - i) for i in range(len(history_regions))] + [t0 - pd.Timedelta(minutes=10), t0],
        "amt": [50.0] * len(history_regions) + [480.0, 490.0],
        "addr1": list(history_regions) + [addr1, addr1],
        "device_key": [None] * (len(history_regions) + 2),
        "channel": ["in_person"] * len(history_regions) + [channel, channel],
    })
    f = {"TransactionID": 101, "channel": channel, "id_15": id_15, "addr1": addr1, "amt": 490.0}
    return SimpleNamespace(h=h, f=f, t0=t0, opened=t0 + pd.Timedelta(hours=1), device=None, device_pop=0)


def test_legitimate_has_empty_episode():
    e = build(ctx(), Findings(), "legitimate")
    assert e.txn_ids == [] and e.first == "" and e.exposure == 0


def test_episode_from_detector_and_exposure():
    fnd = Findings()
    fnd.episode["sub_threshold_burst"] = [100, 101]
    e = build(ctx(), fnd, "fraud")
    assert e.txn_ids == ["100", "101"] and e.first == "100" and e.exposure == 970.0


def test_pattern_rules():
    assert pattern_for(ctx(), Findings(), "legitimate") == "none"
    assert pattern_for(ctx(id_15="New"), Findings(), "fraud") == "card_not_present_new_device"
    assert pattern_for(ctx(id_15="Found"), Findings(), "fraud") == "card_not_present_fraud"
    # card present in the card's main region reads as account takeover, in a rarely used region as out-of-region use
    assert pattern_for(ctx(channel="in_person", id_15=None, addr1=264), Findings(), "fraud") == "account_takeover"
    assert pattern_for(ctx(channel="in_person", id_15=None, addr1=999), Findings(), "fraud") == "out_of_region_use"
    f = Findings()
    f.coordinated_undocumented = True
    f.vote("undocumented", 2.6)
    assert pattern_for(ctx(), f, "fraud") == "undocumented"
