"""kavach.schema_map without any model call: parsing, comment stripping, validation of proposed mappings, scoring."""
from kavach import schema_map as sm

DDL = """
# Expected mapping (ground truth):
#   Person      exact        Cust where IsCstmr = true
#   Account     exact        Acct
#   OWNS        exact        reverse_Acct_Owner
#   Device      unmapped
CREATE GRAPH G ()
USE GRAPH G
CREATE SCHEMA_CHANGE JOB j FOR GRAPH G {
  ADD VERTEX Cust (PRIMARY_ID CustId STRING, IsCstmr BOOL, NmHash STRING) WITH primary_id_as_attribute="true";
  ADD VERTEX Acct (PRIMARY_ID AcctId STRING, OpngDt DATETIME, Sts STRING) WITH primary_id_as_attribute="true";
  ADD VERTEX Pay (PRIMARY_ID PayId STRING, Amt DOUBLE, Ts STRING) WITH primary_id_as_attribute="true";
  ADD DIRECTED EDGE Acct_Owner (FROM Acct, TO Cust) WITH REVERSE_EDGE="reverse_Acct_Owner";
  ADD DIRECTED EDGE Pay_Debit (FROM Pay, TO Acct) WITH REVERSE_EDGE="reverse_Pay_Debit";
  ADD UNDIRECTED EDGE Cust_Link (FROM Cust, TO Cust);
}
"""


def test_parse_reads_ddl_only():
    s = sm.parse_gsql(DDL)
    assert s["graph"] == "G"
    assert set(s["vertices"]) == {"Cust", "Acct", "Pay"}
    assert s["vertices"]["Pay"]["attrs"]["Amt"] == "DOUBLE"
    assert s["edges"]["Acct_Owner"]["reverse"] == "reverse_Acct_Owner"
    assert s["edges"]["Cust_Link"]["directed"] is False
    assert "Expected mapping" not in sm.strip_comments(DDL)


def test_answer_key_is_read_from_the_comments():
    k = sm.answer_key(DDL)
    assert k["Person"][0] == "exact" and "IsCstmr" in k["Person"][1]
    assert k["Device"][0] == "unmapped"


def _proposal(**over):
    p = {"vertices": [{"canonical": "Person", "kind": "exact", "foreign": ["Cust"], "condition": "IsCstmr = true"},
                      {"canonical": "Account", "kind": "exact", "foreign": ["Acct"]},
                      {"canonical": "Transaction", "kind": "exact", "foreign": ["Pay"]}],
         "edges": [{"canonical": "OWNS", "kind": "exact", "path": ["reverse_Acct_Owner"]},
                   {"canonical": "INITIATED", "kind": "exact", "path": ["reverse_Pay_Debit"]}],
         "attributes": [{"canonical": "Transaction.amount", "kind": "exact", "foreign": "Pay.Amt"},
                        {"canonical": "Transaction.timestamp", "kind": "exact", "foreign": "Pay.Ts"}]}
    p.update(over)
    return p


def test_valid_paths_and_reverse_edges_pass():
    m = sm.validate(sm.parse_gsql(DDL), _proposal())
    assert m["edges"]["OWNS"]["kind"] == "exact" and m["edges"]["OWNS"]["path"] == ["reverse_Acct_Owner"]
    assert m["edges"]["INITIATED"]["kind"] == "exact"
    assert m["vertices"]["Device"]["kind"] == "unmapped"


def test_wrong_direction_and_unknown_names_are_rejected():
    bad = _proposal(edges=[{"canonical": "OWNS", "kind": "exact", "path": ["Acct_Owner"]},          # walks Acct -> Cust
                           {"canonical": "INITIATED", "kind": "exact", "path": ["Pay_Credit"]}])    # no such edge
    m = sm.validate(sm.parse_gsql(DDL), bad)
    assert m["edges"]["OWNS"]["kind"] == "unmapped" and "does not start" in m["edges"]["OWNS"]["rejected"]
    assert m["edges"]["INITIATED"]["kind"] == "unmapped" and "not in the schema" in m["edges"]["INITIATED"]["rejected"]
    m = sm.validate(sm.parse_gsql(DDL), _proposal(vertices=[{"canonical": "Person", "kind": "exact", "foreign": ["Nobody"]}]))
    assert m["vertices"]["Person"]["kind"] == "unmapped"


def test_string_dates_are_downgraded_and_score_counts():
    s = sm.parse_gsql(DDL)
    m = sm.validate(s, _proposal())
    assert m["attributes"]["Transaction.timestamp"]["kind"] == "approximate"     # Ts is a STRING
    sc = sm.score(m, sm.answer_key(DDL), s)
    assert sc["items"] == 4 and sc["target_ok"] == 4 and sc["both_ok"] == 4
