"""Someone else's schema: value profiling, reshape candidates, the approval gate, and a round trip
(canonical -> variant -> propose -> approve -> apply -> canonical) on a small synthetic bank, no real data needed."""
import json
import random

import numpy as np
import pandas as pd
import pytest

from kavach import mapper as M
from kavach.harness.faults import Forbidden

TOK = {"analyst": "analyst-token-0123456789", "L1": "l1-token-0123456789abc", "L2": "l2-token-0123456789abc"}
SMALL_TXN = [c for c in M.TXN_COLS if not c.startswith("V")] + ["V1", "V2", "V3"]


# ---------------------------------------------------------------------------------------------- helpers
def test_shape_and_profile():
    assert M.shape("C01234-K2") == "A9-A9"
    assert M.shape("2017-01-02 03:04:05") == "9-9-9 9:9:9"
    assert M.shape("gmail.com") == "a.a"
    p = M.profile(pd.Series(["W", "C", "W", "H", None] * 20))
    assert p["kind"] == "enum" and p["null"] == pytest.approx(0.2)
    q = M.profile(pd.Series([str(x) for x in np.linspace(1, 500, 200)]))
    assert q["kind"] == "num" and q["min"] == 1.0


def test_name_similarity_families_and_synonyms():
    assert M.name_sim("cnt_07", "C7") >= 0.9 and M.name_sim("cnt_07", "C8") == 0.0
    assert M.name_sim("days_15", "D15") >= 0.9 and M.name_sim("ident_31", "id_31") >= 0.9
    assert M.name_sim("purchaser_email_domain", "P_emaildomain") == 1.0
    assert M.name_sim("loss_amount", "exposure_usd") > M.name_sim("loss_amount", "analyst_notes")


def test_reshape_candidates():
    df = pd.DataFrame({
        "event_ms": [str((M.EPOCH0 + 3600 * i) * 1000) for i in range(30)],
        "d": ["2017-01-%02d" % (i % 28 + 1) for i in range(30)], "t": ["03:04:%02d" % i for i in range(30)],
        "cents": [str(1000 + 17 * i) for i in range(30)],
        "card": [f"{1000 + i}|111.0||visa|226.0|debit" for i in range(30)],
        "lst": [";".join(f"{3000000 + i + k}" for k in range(i % 3 + 1)) for i in range(30)],
        "opened": ["%02d/01/2017 05:06:07" % (i % 28 + 1) for i in range(30)],
    })
    c = {x["id"]: x for x in M.candidates(df)}
    assert c["event_ms@epoch_ms"]["values"][0] == "2016-07-02 00:00:00"
    assert c["d+t"]["values"][0] == "2017-01-01 03:04:00"
    assert c["cents@div100"]["values"][0] == "10.0"
    assert c["card[0]"]["values"][0] == "1000" and c["card[3]"]["values"][0] == "visa" and c["card[2]"]["values"][0] is None
    assert c["lst@list"]["values"][2] == "3000002|3000003|3000004"
    assert c["opened@ts"]["transform"]["format"] == "%d/%m/%Y %H:%M:%S" and c["opened@ts"]["values"][13] == "2017-01-14 05:06:07"


def test_recodes():
    ref_tf = M.profile(pd.Series(["T"] * 40 + ["F"] * 10))
    assert M._recode_for(pd.Series(["Y"] * 30 + ["N"] * 5), ref_tf) == {"Y": "T", "N": "F"}
    ref_yn = M.profile(pd.Series(["Yes"] * 5 + ["No"] * 45))
    assert M._recode_for(pd.Series(["1"] * 5 + ["0"] * 45), ref_yn) == {"1": "Yes", "0": "No"}
    ref_ch = M.profile(pd.Series(["online"] * 10 + ["in_person"] * 30))
    assert M._recode_for(pd.Series(["ONLINE"] * 10 + ["IN_PERSON"] * 30), ref_ch) == {"ONLINE": "online", "IN_PERSON": "in_person"}
    assert M._recode_for(pd.Series(["T", "F"] * 10), ref_tf) is None


def test_long_format_detection_and_pivot():
    rows = [(str(3000000 + i), a, v) for i in range(40) for a, v in (("ident_15", "New"), ("device_info", "Windows"), ("ident_01", "-5.0"))]
    df = pd.DataFrame(rows, columns=["txn_ref", "attribute", "attr_value"])
    spec = M.detect_long(df)
    assert spec == {"id": "txn_ref", "key": "attribute", "value": "attr_value"}
    wide = M.pivot_long(df, spec)
    assert set(wide.columns) == {"txn_ref", "ident_15", "device_info", "ident_01"} and len(wide) == 40
    assert M.detect_long(pd.DataFrame({"a": range(60), "b": range(60), "c": range(60)})) is None


# ---------------------------------------------------------------------------------------------- synthetic bank
def _bank(tmp_path, n=400, seed=3):
    rng = random.Random(seed)
    nr = np.random.default_rng(seed)
    cust = [f"C{rng.randint(1, 900):05d}" for _ in range(n)]
    dt = np.sort(nr.integers(100, 16_000_000, n))
    t = {}
    t["TransactionID"] = [str(3000000 + i) for i in range(n)]
    t["TransactionDT"] = [str(int(x)) for x in dt]
    t["TransactionAmt"] = [f"{x:.2f}" for x in np.round(nr.lognormal(4, 1, n), 2)]
    prod = [rng.choice("WWWWCHRS") for _ in range(n)]
    t["ProductCD"] = prod
    t["card1"] = [str(rng.randint(10001, 23553)) for _ in range(n)]
    t["card2"] = [rng.choice(["", f"{rng.randint(100, 600)}.0"]) for _ in range(n)]
    t["card3"] = [rng.choice(["150.0", "185.0", "144.0"]) for _ in range(n)]
    t["card4"] = [rng.choice(["visa", "visa", "mastercard", "discover", "american express"]) for _ in range(n)]
    t["card5"] = [f"{rng.randint(100, 237)}.0" for _ in range(n)]
    t["card6"] = [rng.choice(["debit", "debit", "credit"]) for _ in range(n)]
    t["addr1"] = [rng.choice(["", f"{rng.randint(100, 540)}.0"]) for _ in range(n)]
    t["addr2"] = [rng.choice(["", "87.0", "60.0"]) for _ in range(n)]
    t["dist1"] = [rng.choice(["", str(rng.randint(0, 3000)) + ".0"]) for _ in range(n)]
    t["dist2"] = [rng.choice(["", "", "", str(rng.randint(0, 9000)) + ".0"]) for _ in range(n)]
    t["P_emaildomain"] = [rng.choice(["gmail.com", "yahoo.com", "hotmail.com", "anonymous.com", ""]) for _ in range(n)]
    t["R_emaildomain"] = [rng.choice(["", "", "gmail.com", "outlook.com"]) for _ in range(n)]
    for i in range(1, 15):
        t[f"C{i}"] = [f"{x}.0" for x in nr.poisson(i % 4 + 1, n)]
    for i in range(1, 16):
        t[f"D{i}"] = [rng.choice(["", f"{x}.0"]) for x in nr.integers(-100 if i == 15 else 0, 40 * i + 20, n)]
    for i in range(1, 10):
        t[f"M{i}"] = ([rng.choice(["M0", "M1", "M2", ""]) for _ in range(n)] if i == 4 else
                      [rng.choice(["T", "T", "F", ""]) for _ in range(n)])
    for i in range(1, 340):
        t[f"V{i}"] = [rng.choice(["", f"{nr.integers(0, i % 7 + 2)}.0"]) for _ in range(n)]
    t["customer_id"] = cust
    t["ts"] = [pd.Timestamp(M.EPOCH0 + int(x), unit="s").strftime(M.TS_FMT) for x in dt]
    t["channel"] = ["in_person" if p == "W" else "online" for p in prod]
    t["risk_score"] = [f"{x:.2f}" for x in nr.uniform(0, 1, n)]
    tx = pd.DataFrame(t)[M.TXN_COLS]
    idn = {"TransactionID": t["TransactionID"][: n // 2]}
    for i in range(1, 39):
        k = f"id_{i:02d}"
        if i in (12, 16, 27, 28, 29):
            idn[k] = [rng.choice(["Found", "NotFound", ""]) for _ in range(n // 2)]
        elif i == 15:
            idn[k] = [rng.choice(["New", "Found", "Unknown"]) for _ in range(n // 2)]
        elif i == 23:
            idn[k] = [rng.choice(["", "", "", "IP_PROXY:TRANSPARENT", "IP_PROXY:ANONYMOUS"]) for _ in range(n // 2)]
        elif i == 30:
            idn[k] = [rng.choice(["Windows 10", "iOS 11.2.1", "Android 7.0", ""]) for _ in range(n // 2)]
        elif i == 31:
            idn[k] = [rng.choice(["chrome 62.0", "mobile safari 11.0", "edge 16.0"]) for _ in range(n // 2)]
        elif i == 33:
            idn[k] = [rng.choice(["1920x1080", "1366x768", "2208x1242", ""]) for _ in range(n // 2)]
        elif i == 34:
            idn[k] = [rng.choice(["match_status:2", "match_status:1", ""]) for _ in range(n // 2)]
        elif i >= 35:
            idn[k] = [rng.choice(["T", "F"]) for _ in range(n // 2)]
        else:
            idn[k] = [rng.choice(["", f"{x}.0"]) for x in nr.integers(-100 * (i % 3), 100 * i, n // 2)]
    idn["DeviceType"] = [rng.choice(["desktop", "mobile"]) for _ in range(n // 2)]
    idn["DeviceInfo"] = [rng.choice(["Windows", "iOS Device", "MacOS", "SM-G935F Build/NRD90M", "Trident/7.0"]) for _ in range(n // 2)]
    ident = pd.DataFrame(idn)[M.IDENT_COLS]
    cl = []
    for i in range(120):
        r = rng.randrange(n)
        fraud = rng.random() < 0.7
        ids = [t["TransactionID"][r]] + ([t["TransactionID"][min(r + 1, n - 1)]] if fraud and rng.random() < 0.5 else [])
        cl.append({"case_id": f"CC-{i + 1:04d}", "customer_id": cust[r], "card_id": f"{cust[r]}-K1",
                   "opened_at": t["ts"][r], "closed_at": t["ts"][min(r + 5, n - 1)],
                   "outcome": "confirmed_fraud" if fraud else "cleared", "pattern": rng.choice(["card_testing", "account_takeover"]) if fraud else "none",
                   "first_fraud_txn_id": ids[0] if fraud else "", "txn_ids": "|".join(ids), "n_txns": str(len(ids)),
                   "exposure_usd": t["TransactionAmt"][r], "connected_card_ids": "|".join(f"C{rng.randint(1, 900):05d}-K1" for _ in range(4)) if i % 9 == 0 else "",
                   "actions_taken": "CREATE_CASE|BLOCK_CARD" if fraud else "VERIFY_WITH_CUSTOMER|CLOSE_NO_FRAUD",
                   "report_filed": rng.choice(["Yes", "No", "No"]), "analyst_notes": f"Case CC-{i + 1:04d}: cardholder {cust[r]} note {rng.random():.6f}"})
    closed = pd.DataFrame(cl)[M.CLOSED_COLS]
    pk = []
    for i in range(12):
        r = rng.randrange(n)
        pk.append({"case_id": f"HHX-{i + 1:03d}", "opened_at": t["ts"][r], "trigger_type": rng.choice(["risk_score", "customer_report", "analyst_request"]),
                   "trigger_text": f"Real-time model scored transaction {t['TransactionID'][r]} at {t['risk_score'][r]}. Review and decide.",
                   "flagged_txn_id": t["TransactionID"][r], "card_id": f"{cust[r]}-K1", "customer_id": cust[r], "risk_score": t["risk_score"][r]})
    pack = pd.DataFrame(pk)[M.PACK_COLS]
    d = tmp_path / "bank"
    d.mkdir()
    files = {"transactions": [d / "transactions.csv"], "identity": [d / "identity.csv"], "closed_cases": [d / "closed.csv"],
             "case_pack": [d / "pack.csv"]}
    for df, role in ((tx, "transactions"), (ident, "identity"), (closed, "closed_cases"), (pack, "case_pack")):
        df.to_csv(files[role][0], index=False)
    return files


@pytest.fixture
def bank(tmp_path, monkeypatch):
    files = _bank(tmp_path)
    monkeypatch.setattr(M, "REF_FILES", files)
    monkeypatch.setattr(M, "TXN_COLS", SMALL_TXN)
    monkeypatch.setitem(M.CANON, "transactions", SMALL_TXN)
    monkeypatch.setattr(M, "_REF", {})
    monkeypatch.setenv("KAVACH_APPROVALS_FILE", str(tmp_path / "approvals.jsonl"))
    for role, tok in TOK.items():
        monkeypatch.setenv({"analyst": "KAVACH_TOKEN_ANALYST", "L1": "KAVACH_TOKEN_L1", "L2": "KAVACH_TOKEN_L2"}[role], tok)
    # the generator reads the full canonical header; give it a source with only the reduced columns
    small = tmp_path / "bank" / "transactions_small.csv"
    pd.read_csv(files["transactions"][0], dtype=str, keep_default_na=False)[SMALL_TXN].to_csv(small, index=False)
    src = dict(files, transactions=[small])
    return {"files": files, "src": src, "tmp": tmp_path}


@pytest.fixture
def proposed(bank):
    out = bank["tmp"] / "variant"
    truth = M.make_variant(seed=11, out_dir=out, src=bank["src"])
    files = M.variant_files(out)
    mapping = M.propose(files, n=400)
    path = M.save(mapping, out / "mapping.json")
    return {**bank, "out": out, "truth": truth, "mapping": mapping, "path": path, "vfiles": files}


def test_variant_is_renamed_and_reshaped(proposed):
    names = {p.name for p in proposed["vfiles"]}
    assert names == {"ledger_hist.csv", "device_attrs_hist.csv", "case_outcomes.csv", "alerts_q4.csv"}
    hdr = pd.read_csv(proposed["out"] / "ledger_hist.csv", nrows=2, dtype=str).columns
    assert "TransactionID" not in hdr and "txn_date" in hdr and "txn_time" in hdr
    assert list(pd.read_csv(proposed["out"] / "device_attrs_hist.csv", nrows=1).columns)[1:] == ["attribute", "attr_value"]


def test_propose_recovers_the_mapping(proposed):
    m, truth = proposed["mapping"], proposed["truth"]
    r = M.score(m, truth)
    assert all(x["ok"] for x in r["roles"]), r["roles"]
    assert r["accuracy"] >= 0.97, r["wrong"]
    assert r["needed_correct"] == r["needed"], [w for w in r["wrong"]]
    tx = [t for t in m["files"] if t["role"] == "transactions"][0]
    cols = {c["target"]: c for c in tx["columns"]}
    assert cols["TransactionAmt"]["transform"]["kind"] == "scale"
    assert cols["card4"]["transform"] == {"kind": "split", "delim": cols["card4"]["transform"]["delim"], "index": 3, "parts": 6}
    assert cols["M1"]["transform"]["recode"] == {"Y": "T", "N": "F"}
    assert any(d["target"] == "TransactionDT" for d in tx["derived"])
    for c in tx["columns"]:       # every choice carries the sample records that justify it
        assert c["samples"] and "canonical_value" in c["samples"][0] and c["confidence"] > 0
    assert tx["key"]["join_rate"] == 1.0


def test_propose_without_join_still_maps_most_columns(proposed):
    m = M.propose(proposed["vfiles"], n=400, align=False)
    r = M.score(m, proposed["truth"])
    assert all(x["ok"] for x in r["roles"])
    assert r["accuracy"] >= 0.6
    for t in m["files"]:          # nothing silently guessed: every canonical column is mapped, derived or listed
        listed = {c["target"] for c in t["columns"]} | {d["target"] for d in t["derived"]} | {u["target"] for u in t["unresolved"]}
        assert listed == set(M.CANON[t["role"]])


def test_apply_needs_an_approval_from_a_real_role(proposed):
    with pytest.raises(M.MappingNotApproved):
        M.apply(proposed["path"], proposed["tmp"] / "canon")
    with pytest.raises(Forbidden):
        M.approve(proposed["path"], "not-a-token-0123456789")
    rec = M.approve(proposed["path"], TOK["analyst"])
    assert rec["role"] == "analyst" and rec["status"] == "approved" and rec["case_id"].startswith("MAP-")
    m = json.loads(proposed["path"].read_text())
    assert M.approval_status(m, proposed["vfiles"]) == ""
    m["files"][0]["columns"][0]["source"] = ["something_else"]          # a change after approval voids it
    assert "changed" in M.approval_status(m)


def test_apply_writes_the_canonical_files(proposed):
    M.approve(proposed["path"], TOK["L2"])
    man = M.apply(proposed["path"], proposed["tmp"] / "canon")
    roles = {f["role"]: f for f in man["files"]}
    assert set(roles) == {"transactions", "identity", "closed_cases", "case_pack"}
    got = pd.read_csv(roles["transactions"]["path"], dtype=str, keep_default_na=False).set_index("TransactionID")
    want = pd.read_csv(proposed["src"]["transactions"][0], dtype=str, keep_default_na=False).set_index("TransactionID")
    assert list(got.columns) == SMALL_TXN[1:]
    for c in ("TransactionDT", "ts", "card1", "card4", "card6", "M1", "M4", "channel", "customer_id", "P_emaildomain", "C1", "D15"):
        a = got[c].map(M.norm_value).fillna("")
        b = want.loc[got.index, c].map(M.norm_value).fillna("")
        assert (a == b).all(), (c, got[c].head(3).tolist(), want[c].head(3).tolist())
    assert np.allclose(got.TransactionAmt.astype(float), want.loc[got.index].TransactionAmt.astype(float))
    cl = pd.read_csv(roles["closed_cases"]["path"], dtype=str, keep_default_na=False).set_index("case_id")
    wc = pd.read_csv(proposed["src"]["closed_cases"][0], dtype=str, keep_default_na=False).set_index("case_id")
    for c in ("txn_ids", "connected_card_ids", "actions_taken", "report_filed", "opened_at", "customer_id", "card_id"):
        assert (cl[c] == wc.loc[cl.index, c]).all(), c
    idn = pd.read_csv(roles["identity"]["path"], dtype=str, keep_default_na=False).set_index("TransactionID")
    wi = pd.read_csv(proposed["src"]["identity"][0], dtype=str, keep_default_na=False).set_index("TransactionID")
    for c in ("id_15", "id_31", "id_33", "DeviceType", "DeviceInfo"):
        assert (idn[c] == wi.loc[idn.index, c]).all(), c
    pk = pd.read_csv(roles["case_pack"]["path"], dtype=str, keep_default_na=False).set_index("case_id")
    wp = pd.read_csv(proposed["src"]["case_pack"][0], dtype=str, keep_default_na=False).set_index("case_id")
    assert (pk.opened_at == wp.loc[pk.index, "opened_at"]).all()


def test_build_index_refuses_the_production_file(tmp_path):
    with pytest.raises(ValueError):
        M.build_index({"files": []}, M.DATA / "kavach_r2.duckdb")


def test_recode_learned_from_one_value_is_completed():
    ref = M.profile(pd.Series(["T"] * 40 + ["F"] * 2))
    assert M._recode_for(pd.Series(["Y"] * 30), ref, pd.Series(["T"] * 30)) == {"Y": "T", "N": "F"}


def test_apply_refuses_values_a_recode_does_not_cover(proposed):
    m = json.loads(proposed["path"].read_text())
    tx = [t for t in m["files"] if t["role"] == "transactions"][0]
    col = [c for c in tx["columns"] if c["target"] == "M2"][0]
    col["transform"]["recode"] = {"Y": "T"}          # an incomplete recode would leave N in a T/F column
    M.save(m, proposed["path"])
    M.approve(proposed["path"], TOK["analyst"])
    with pytest.raises(M.MappingIncomplete):
        M.apply(proposed["path"], proposed["tmp"] / "canon")
