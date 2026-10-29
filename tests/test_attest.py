"""Attestation sidecars: build, write, re-run (pass), tamper (fail), no query (unverifiable), SAR sentence citations."""
import json

import pandas as pd
import pytest

from kavach import attest
from kavach.assess import Signal

CASE = "TEST-ATTEST"


@pytest.fixture(scope="module")
def r1():
    """A real Round 1 card and transaction from the index (the card is resolved from the transaction itself)."""
    try:
        from kavach import duckq
        row = duckq.case_pack(round=1).iloc[0].to_dict()
        txn = duckq.txn_detail(row["flagged_txn_id"])
    except Exception as e:  # pragma: no cover
        pytest.skip(f"DuckDB index not available: {e}")
    if not txn:
        pytest.skip("flagged transaction not in the index")
    return {"txn": str(int(txn["TransactionID"])), "card": txn["card_id"], "customer": txn["customer_id"], "ts": txn["ts"]}


def _answer(evidence, sar=None):
    return {"case_id": CASE, "case": {"evidence": evidence}, "sar": sar or {"file": False, "narrative": ""}}


def _sidecar(r1, tmp_path):
    claim = f"Synthetic: transaction {r1['txn']} is on card {r1['card']}"
    sigs = [
        Signal("synthetic_history", "sequence", 1.2, claim, [r1["txn"], r1["card"]], f"query:card_history(card_id={r1['card']})",
               "graph", how={"tool": "card_history", "params": {"card_id": r1["card"]}}),
        # a detector hit that did not make the evidence list; its how carries a timestamp parameter
        Signal("synthetic_window", "device", 0.6, "Synthetic window hit", [r1["txn"]], "query:card_window(...)", "graph",
               how={"tool": "card_window", "params": {"card_id": r1["card"], "t0": pd.Timestamp(r1["ts"]),
                                                       "hours_before": 1, "hours_after": 1}}),
        Signal("old_style", "region", 0.5, "Claim from a detector without a recorded query", [r1["txn"]],
               "query:region_activity(addr1=1)", "graph"),
    ]
    doc = attest._document("POLICY-R1")
    first = doc["text"].split(". ")[0].replace("**", "").replace("`", "").strip()
    evidence = [
        {"claim": claim, "source": "graph", "ref": sigs[0].ref, "entity_ids": [r1["txn"], r1["card"], r1["customer"]]},
        {"claim": "Claim from a detector without a recorded query", "source": "graph", "ref": "query:region_activity(addr1=1)",
         "entity_ids": [r1["txn"]]},
        {"claim": "Customer denied the transaction when asked", "source": "customer", "ref": "evidence_request:1", "entity_ids": []},
        {"claim": f"Policy applied, {doc['section']}: {first}.", "source": "document", "ref": "document:README#POLICY-R1",
         "entity_ids": []},
    ]
    sar_sent = [(f"This report concerns customer {r1['customer']}, card {r1['card']}.", [r1["customer"], r1["card"]])]
    side = attest.build(CASE, _answer(evidence), sigs, {}, "duck", None, sar_sentences=sar_sent)
    attest.write(CASE, side, tmp_path)
    return side


def test_split_ids_by_shape():
    ev = attest.split_ids(["3476602", "C07297-K1", "C07297", "CC-4124", "iOS Device | iOS 11 | safari | 1x1", "x1", 3476603.0])
    assert ev["txn_ids"] == ["3476602", "3476603"] and ev["card_ids"] == ["C07297-K1"] and ev["customer_ids"] == ["C07297"]
    assert ev["case_ids"] == ["CC-4124"] and ev["device_keys"] == ["iOS Device | iOS 11 | safari | 1x1"] and ev["other"] == ["x1"]


def test_build_write_verify_pass(r1, tmp_path):
    side = _sidecar(r1, tmp_path)
    assert (tmp_path / f"{CASE}.json").exists()
    by = {c["id"]: c for c in side["claims"]}
    assert by["E1"]["how"] == {"tool": "card_history", "params": {"card_id": r1["card"]}}
    assert by["E1"]["evidence"]["txn_ids"] == [r1["txn"]] and by["E1"]["evidence"]["customer_ids"] == [r1["customer"]]
    assert by["E1"]["when"]["dataset_version"] and by["E1"]["when"]["ts"].endswith("Z")
    assert by["D1"]["kind"] == "detector_hit" and isinstance(by["D1"]["how"]["params"]["t0"], str)
    assert by["S1"]["kind"] == "sar_sentence" and "E1" in by["S1"]["supported_by"]
    json.dumps(side)  # plain JSON throughout

    rows = {r["id"]: r for r in attest.verify(CASE, "duck", tmp_path)}
    assert rows["E1"]["status"] == "pass", rows["E1"]
    assert rows["D1"]["status"] == "pass", rows["D1"]           # timestamp parameter survives the JSON round trip
    assert rows["S1"]["status"] == "pass", rows["S1"]
    assert rows["E4"]["status"] == "pass", rows["E4"]           # document quote re-checked against the policy text
    assert rows["E2"]["status"] == "unverifiable" and rows["E2"]["reason"] == "unverifiable: no query recorded"
    assert rows["E3"]["status"] == "unverifiable" and "assumed customer response" in rows["E3"]["reason"]
    assert attest.main([CASE, "--dir", str(tmp_path)]) == 0


def test_tampered_id_fails(r1, tmp_path):
    _sidecar(r1, tmp_path)
    p = tmp_path / f"{CASE}.json"
    side = json.loads(p.read_text(encoding="utf-8"))
    side["claims"][0]["evidence"]["txn_ids"] = ["9999999"]
    p.write_text(json.dumps(side), encoding="utf-8")
    rows = {r["id"]: r for r in attest.verify(CASE, "duck", tmp_path)}
    assert rows["E1"]["status"] == "fail" and rows["E1"]["missing"] == ["9999999"]
    assert attest.main([CASE, "--dir", str(tmp_path)]) == 1


def test_tampered_document_quote_fails(r1, tmp_path):
    _sidecar(r1, tmp_path)
    p = tmp_path / f"{CASE}.json"
    side = json.loads(p.read_text(encoding="utf-8"))
    e4 = next(c for c in side["claims"] if c["id"] == "E4")
    e4["what"] = "Policy applied, R1: the bank always blocks every card on the first alert without asking anyone."
    p.write_text(json.dumps(side), encoding="utf-8")
    rows = {r["id"]: r for r in attest.verify(CASE, "duck", tmp_path)}
    assert rows["E4"]["status"] == "fail"


def test_missing_sidecar_is_a_failure(tmp_path):
    assert attest.main(["NOPE-000", "--dir", str(tmp_path)]) == 1


def test_files_version_shape():
    v = attest.files_version()
    assert v["version"].startswith("files-") and isinstance(v["files"], dict)


# ---------------------------------------------------------------------------------------------------------------
def _facts(n_connected=2):
    return {
        "customer_id": "C07297", "card_id": "C07297-K1", "card_desc": "mastercard debit", "date_from": "2016-11-21",
        "date_to": "2016-11-21", "n_txns": 2, "exposure": 961.07, "first_txn": "3476602",
        "where": "online, product code(s) C, billing region(s) 264, from device profile(s) Trident/7.0 | Windows 7 | ie 11.0 for desktop | 1920x1080",
        "how": "a burst of online purchases priced just under a round limit", "pattern": "card_testing",
        "top_fraud": "2 online purchases just under $500 (3476602, 3476633)", "n_connected": n_connected,
        "shared": "device profile", "connected": ["C00466-K1", "C01890-K1"][:n_connected], "prior_cases": ["CC-4124"],
        "customer_statement": "The cardholder reported the disputed transaction to the bank and denied making it.",
        "containment": "BLOCK_CARD", "rule": "R2, §3a",
        "episode_txns": [{"id": "3476602", "device": "Trident/7.0 | Windows 7 | ie 11.0 for desktop | 1920x1080"},
                         {"id": "3476633", "device": None}],
    }


@pytest.mark.parametrize("n_connected", [0, 2])
def test_sar_sentences_mirror_the_template(n_connected):
    from kavach.narrative import sar_template
    f = _facts(n_connected)
    sents = attest.sar_sentences_template(f)
    assert " ".join(s for s, _ in sents) == sar_template(f)
    assert all(ids for _, ids in sents)                       # every sentence cites at least one id
    ids = attest.template_ids(sents)
    assert {"C07297", "C07297-K1", "3476602", "3476633", "CC-4124"} <= ids
    assert ("C00466-K1" in ids) == bool(n_connected)
    assert "Trident/7.0 | Windows 7 | ie 11.0 for desktop | 1920x1080" in ids


def test_check_llm_sar():
    sents = attest.sar_sentences_template(_facts())
    ok = "Card C07297-K1 of customer C07297 was used for transactions 3476602 and 3476633, linked to C00466-K1 and case CC-4124."
    assert attest.check_llm_sar(ok, sents)
    assert not attest.check_llm_sar(ok + " Card C99999-K1 was also involved.", sents)
    assert not attest.check_llm_sar(ok + " See transaction 3999999.", sents)
    assert not attest.check_llm_sar("", sents)


def test_cite_sentences_inherits_closest_template_ids():
    sents = attest.sar_sentences_template(_facts())
    text = ("Card C07297-K1 was used without authorization. The activity is assessed as card testing with purchases just "
            "under $500.")
    cited = attest.cite_sentences(text, sents)
    assert cited[0][1] == ["C07297-K1"]
    assert cited[1][1] and set(cited[1][1]) <= attest.template_ids(sents)
