from types import SimpleNamespace

import numpy as np
import pandas as pd

from kavach.assess import Signal, assess, settled
from kavach.cardfeat import card_features
from kavach.detectors import Findings
from kavach.simulate import customer_reply
from kavach.tools import ToolRegistry


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
    # a customer who already disputed is NOT assumed to repeat the denial without corroborating evidence
    assert customer_reply(0.5, 0, False, "customer_report").kind == "no_reply"
    assert customer_reply(0.6, 1, False, "customer_report").kind == "deny"
    assert customer_reply(0.1, 0, False, "customer_report").kind == "confirm"
    # a single very strong group (e.g. a device ring) is enough to expect a denial
    assert customer_reply(0.6, 1, False, "risk_score", strongest_group=2.4).kind == "deny"


def test_never_deny_when_the_profile_fits():
    # two independent legitimate groups and a balance that does not lean to fraud: no denial, whatever the trigger
    for trig in ("risk_score", "customer_report", "analyst_request"):
        r = customer_reply(0.5, 1, False, trig, strongest_group=2.0, n_legit_groups=2)
        assert r.kind in ("confirm", "no_reply")
    # low probability without the customer but a fraud-leaning signal: no reply, never a confirmation
    assert customer_reply(0.3, 0, False, "customer_report", n_fraud_signals=1).kind == "no_reply"
    assert customer_reply(0.3, 1, False, "risk_score").kind == "no_reply"
    # two weak profile matches do not outweigh a strong precedent: the balance still leans to fraud
    assert customer_reply(0.82, 1, False, "risk_score", strongest_group=2.5, n_legit_groups=2).kind == "deny"
    # a dispute with an anomaly (e.g. amount far above the account maximum) and p above CONFIRM_P is a denial
    assert customer_reply(0.6, 0, False, "customer_report", n_fraud_signals=1).kind == "deny"


def test_replies_are_deterministic():
    args = (0.47, 1, False, "customer_report")
    assert len({customer_reply(*args, n_legit_groups=1).text for _ in range(5)}) == 1


def test_score_never_settles_alone():
    alone = assess([S("score", -3.0)])
    assert alone.legit_groups == [] and not settled(alone)
    with_one = assess([S("score", -2.5), S("device", -0.5)])
    assert with_one.legit_groups == ["device"] and settled(with_one)


# ---------------------------------------------------------------- onecard: profile fit, January clamp, attestation
def _ctx(flag_ts, risk=0.9, pack_score=None, trigger="risk_score", n_prior=30, flag_amt=60.0, proxy_prior=False, proxy_now=False,
         product="W", channel="in_person"):
    from kavach.detectors import onecard  # noqa: F401
    t_first = pd.Timestamp(flag_ts) - pd.Timedelta(days=n_prior + 10)
    rows = []
    for i in range(n_prior):
        rows.append(dict(TransactionID=1000 + i, ts=t_first + pd.Timedelta(days=i, hours=0.1 * (i % 3)), amt=50.0 + i, ProductCD=product,
                         channel=channel, addr1=264.0, device_key=None, P_emaildomain="gmail.com", R_emaildomain=None, risk_score=0.1,
                         holder_key="15868|264.0|50", id_15=None, id_23="IP_PROXY:ANONYMOUS" if proxy_prior else None,
                         M4=None, M5=None, M6=None, dist1=np.nan, C1=1.0, C2=1.0, card_id="C1-K1"))
    rows.append(dict(TransactionID=5000, ts=pd.Timestamp(flag_ts), amt=flag_amt, ProductCD=product, channel=channel, addr1=264.0,
                     device_key=None, P_emaildomain="gmail.com", R_emaildomain=None, risk_score=risk, holder_key="15868|264.0|50"
                     if pd.Timestamp(flag_ts) < pd.Timestamp("2017-01-01") else "15868|264.0|-999", id_15=None,
                     id_23="IP_PROXY:ANONYMOUS" if proxy_now else None, M4=None, M5=None, M6=None, dist1=np.nan, C1=1.0, C2=1.0,
                     card_id="C1-K1"))
    h = card_features(pd.DataFrame(rows))
    f = h[h.TransactionID == 5000].iloc[0].to_dict()
    t0 = pd.Timestamp(flag_ts)
    case = {"case_id": "TEST-1", "trigger_type": trigger, "card_id": "C1-K1", "customer_id": "C1", "risk_score": pack_score}
    c = SimpleNamespace(h=h, f=f, t0=t0, opened=t0 + pd.Timedelta(hours=1), case=case, card_id="C1-K1", customer_id="C1",
                        trigger=trigger, tools=SimpleNamespace(ref=ToolRegistry.ref, step=0), refs={"history": "query:card_history(C1-K1)",
                        "holder": "query:holder", "closed": "query:closed"}, device=None, device_pop=0, holder_hist=None, closed_hits=None,
                        dq_notes=[])
    c.window = lambda hb=48, ha=48: h[(h.ts >= t0 - pd.Timedelta(hours=hb)) & (h.ts <= t0 + pd.Timedelta(hours=ha))]
    return c


def _detect(c):
    from kavach.detectors import onecard
    return {s.name: s for s in onecard.detect(c, Findings())}


def test_january_score_is_untrusted_and_clamped():
    hi = _detect(_ctx("2017-01-10 12:00", risk=0.95, product="C", channel="online"))
    s = hi["risk_score_untrusted"]
    assert "risk_score_calibrated" not in hi and -0.10 <= s.llr <= 0.30 and "not retrained" in s.claim
    assert s.how == {"tool": "txn_detail", "params": {"txn_id": 5000}}
    lo = _detect(_ctx("2017-01-10 12:00", risk=0.01, product="C", channel="online"))["risk_score_untrusted"]
    assert lo.llr >= -0.10
    # before January the calibrated reading is unchanged
    assert "risk_score_calibrated" in _detect(_ctx("2016-11-10 12:00", risk=0.95, product="C", channel="online"))


def test_alert_score_mismatch_is_a_data_quality_note():
    c = _ctx("2017-01-10 12:00", risk=0.11, pack_score="0.81")
    sig = _detect(c)["risk_score_untrusted"]
    assert any(n["flag"] == "alert_score_mismatch" and "5000" in n["record_ids"] for n in c.dq_notes)
    assert "0.81" in sig.claim and "0.11" in sig.claim


def test_profile_fit_negatives_and_no_january_continuity():
    pre = _detect(_ctx("2016-12-10 12:00", risk=0.2))
    assert pre["profile_fit"].llr <= -1.0 and pre["profile_fit"].group == "sequence"
    assert "email_familiar" not in pre                       # folded into the conjunction, never double counted
    jan = _detect(_ctx("2017-01-10 12:00", risk=0.2))
    assert "profile_fit" in jan and "account_continuity" not in jan
    assert "card C1-K1" in jan["profile_fit"].claim             # January baseline is the card's own history
    big = _detect(_ctx("2016-12-10 12:00", risk=0.2, flag_amt=5000.0))
    assert "profile_fit" not in big and "amount_within_max" not in big


def test_recent_activity_is_not_profile():
    # the whole history sits inside the last 7 days: nothing can claim that the purchase fits a settled profile
    c = _ctx("2016-12-10 12:00", risk=0.2, n_prior=30)
    c.h["ts"] = [pd.Timestamp("2016-12-10 11:00") - pd.Timedelta(minutes=5 * (len(c.h) - i)) for i in range(len(c.h))]
    c.h.loc[c.h.TransactionID == 5000, "ts"] = pd.Timestamp("2016-12-10 12:00")
    sigs = _detect(c)
    assert not {"profile_fit", "product_familiar", "amount_within_max", "amount_seen_before"} & set(sigs)


def test_prior_proxy_use_is_not_new():
    s = _detect(_ctx("2016-12-10 12:00", risk=0.2, proxy_prior=True, proxy_now=True))
    assert s["proxy_seen_before"].llr < 0 and s["proxy_seen_before"].group == "device"
    assert "anonymous_proxy" not in s


def test_dispute_weight_and_every_signal_attested():
    s = _detect(_ctx("2016-12-10 12:00", risk=0.2, trigger="customer_report"))
    assert s["customer_dispute"].llr == 0.9 and s["customer_dispute"].source == "customer"
    for sig in s.values():
        assert sig.how.get("tool") and isinstance(sig.how.get("params"), dict), sig.name
