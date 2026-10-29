"""Data-quality report and per-investigation taint flags on a tiny DuckDB index."""
from datetime import datetime, timedelta

import duckdb
import pytest

from kavach import dq


def _txn(tid, ts, card, cust, card6="credit", amt=50.0, addr1=100.0, hk="1|100|5", score=0.1, dinfo=None, id30=None, id31=None):
    return (tid, ts, "W", amt, addr1, card, cust, card6, hk, score, dinfo, id30, id31, None if dinfo is None else f"{dinfo} | {id30}")


@pytest.fixture
def duck(tmp_path):
    d0 = datetime(2016, 7, 1)
    rows, ident = [], []
    # card A-K1: 8 clean transactions with identity, flagged one in December
    for i in range(8):
        rows.append(_txn(3000001 + i, d0 + timedelta(days=20 * i), "A-K1", "A", dinfo="Pixel 2", id30="Android 8", id31="chrome 62.0"))
        ident.append(3000001 + i)
    # customer B: credit card K1 with one txn; debit card K2 with one December txn and the flagged January txn (no identity,
    # no region, unusable holder key); the alert names B-K1
    rows.append(_txn(3100001, datetime(2016, 9, 1), "B-K1", "B"))
    rows.append(_txn(3100002, datetime(2016, 12, 20), "B-K2", "B", card6="debit", addr1=None, hk="7||"))
    rows.append(_txn(3600001, datetime(2017, 1, 5), "B-K2", "B", card6="debit", addr1=None, hk="7||", score=0.02))
    # card C-K1: 6 prior txns, then a January txn on a placeholder device; one duplicate pair and one zero amount
    for i in range(6):
        rows.append(_txn(3200001 + i, d0 + timedelta(days=30 * i), "C-K1", "C"))
    rows.append(_txn(3600002, datetime(2017, 1, 6, 10), "C-K1", "C", dinfo="Windows", id30=None, id31="other"))
    ident.append(3600002)
    rows.append(_txn(3600003, datetime(2017, 1, 7, 9), "C-K1", "C", amt=20.0))
    rows.append(_txn(3600004, datetime(2017, 1, 7, 9), "C-K1", "C", amt=20.0))
    rows.append(_txn(3600005, datetime(2017, 1, 8), "C-K1", "C", amt=0.0))
    # card D-K1: one prior txn on the card, but its holder key has 6 earlier txns on a sibling card (not thin)
    for i in range(6):
        rows.append(_txn(3300001 + i, d0 + timedelta(days=10 * i), "D-K2", "D", hk="9|200|3"))
    rows.append(_txn(3300010, datetime(2016, 11, 1), "D-K1", "D", hk="9|200|3"))
    rows.append(_txn(3300011, datetime(2016, 11, 2), "D-K1", "D", hk="9|200|3"))
    path = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""create table txn (TransactionID bigint, ts timestamp, ProductCD varchar, TransactionAmt double, addr1 double,
                   card_id varchar, customer_id varchar, card6 varchar, holder_key varchar, risk_score double, DeviceInfo varchar,
                   id_30 varchar, id_31 varchar, device_key varchar)""")
    con.executemany("insert into txn values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("create table ident (TransactionID bigint)")
    con.executemany("insert into ident values (?)", [(i,) for i in ident])
    con.execute("create table closed_txn as select * from (values (3000003::bigint, 'confirmed_fraud')) t(TransactionID, outcome)")
    con.execute("""create table cases as select * from (values
        ('HHG-001', 'risk_score', '3000008', 'A-K1', 'A', 1),
        ('HHF-001', 'risk_score', '3600001', 'B-K1', 'B', 2),
        ('HHF-002', 'risk_score', '3600002', 'C-K1', 'C', 2),
        ('HHF-003', 'analyst_request', '999', 'A-K1', 'A', 2),
        ('HHF-004', 'customer_report', '3300011', 'D-K1', 'D', 2)) t(case_id, trigger_type, flagged_txn_id, card_id, customer_id, round)""")
    con.close()
    return path


def _flags(fl):
    return {f["flag"]: f for f in fl}


def test_taint_mismatch_identity_region_thin_january(duck):
    fl = _flags(dq.taint({"case_id": "HHF-001", "flagged_txn_id": "3600001", "card_id": "B-K1", "customer_id": "B"}, duck))
    assert set(fl) == {"alert_card_mismatch", "no_identity_record", "empty_billing_region", "thin_history", "january_score"}
    assert fl["alert_card_mismatch"]["record_ids"] == ["3600001", "B-K1", "B-K2"]
    assert fl["thin_history"]["record_ids"] == ["B-K2", "3100002"]
    for f in fl.values():
        assert f["record_ids"] and f["consequence"] and f["detail"]
    assert fl["thin_history"]["caps_probability"] and not fl["january_score"]["caps_probability"]
    flags = list(fl.values())
    assert dq.probability_cap(flags) == 0.60
    assert dq.probability_cap(flags, ["network"]) is None and dq.probability_cap(flags, ["history", "device"]) is None
    assert dq.probability_cap([fl["no_identity_record"]]) is None


def test_taint_clean_placeholder_holder_and_missing(duck):
    assert dq.taint({"flagged_txn_id": "3000008", "card_id": "A-K1", "customer_id": "A"}, duck) == []
    fl = _flags(dq.taint({"flagged_txn_id": "3600002", "card_id": "C-K1", "customer_id": "C"}, duck))
    assert set(fl) == {"placeholder_device", "january_score"}
    assert "Windows" in fl["placeholder_device"]["detail"] and "id_31 'other'" in fl["placeholder_device"]["detail"]
    # one prior txn on the card, six on the holder key: enough history, not thin
    assert "thin_history" not in _flags(dq.taint({"flagged_txn_id": "3300011", "card_id": "D-K1", "customer_id": "D"}, duck))
    missing = dq.taint({"flagged_txn_id": "999", "card_id": "A-K1", "customer_id": "A"}, duck)
    assert [f["flag"] for f in missing] == ["flagged_txn_missing"]


def test_report(duck, tmp_path):
    md = tmp_path / "dq.md"
    rep = dq.report(duck, out_md=md)
    assert rep["totals"]["txns"] == 29 and rep["totals"]["empty_addr1"] == 2
    jan_w = next(r for r in rep["no_identity"] if r["month"] == "2017-01")
    assert jan_w["txns"] == 5 and jan_w["no_identity"] == 4 and 3600001 in jan_w["sample_ids"]
    ph = {p["value"]: p for p in rep["placeholder_device"]}
    assert ph["DeviceInfo = Windows"]["sample_ids"] == [3600002] and "id_31 = other" in ph and "id_30 empty (identity present)" in ph
    assert rep["duplicates"]["same_transaction_id"]["ids"] == 0
    assert rep["duplicates"]["same_card_ts_amount_product"]["groups"] == 1
    assert rep["non_positive_amounts"]["non_positive"] == 1 and rep["non_positive_amounts"]["sample_ids"] == [3600005]
    assert rep["thin_cards"]["thin_cards"] == 3           # B-K1 (1 txn), B-K2 and D-K1 (2 txns each)
    jul = next(r for r in rep["risk_by_month"] if r["month"] == "2016-07")
    assert jul["labelled_fraud"] == 0 and next(r for r in rep["risk_by_month"] if r["month"] == "2016-08")["labelled_fraud"] == 1
    assert rep["pack_round"] == 2 and [t["case_id"] for t in rep["taint"]] == ["HHF-001", "HHF-002", "HHF-003", "HHF-004"]
    assert next(t for t in rep["taint"] if t["case_id"] == "HHF-001")["cap_rule_applies"]
    text = md.read_text(encoding="utf-8")
    assert "HHF-001" in text and "alert_card_mismatch" in text and "3600005" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text
