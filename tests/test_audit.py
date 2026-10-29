"""Audit replay: renders a Round 1 trace, and places snapshots, digests and claim links on a synthetic trace."""
import json

import pytest

from kavach import audit
from kavach.config import CASES_DIR, LOGS, ROOT


def _round1(case_id="HHG-006"):
    for traces, cases in ((LOGS / "traces", CASES_DIR), (ROOT / "round1" / "logs" / "traces", ROOT / "round1" / "cases")):
        if (traces / f"{case_id}.json").exists():
            return traces, cases
    pytest.skip("Round 1 trace not found")


def test_replay_renders_round1_trace(tmp_path):
    traces, cases = _round1()
    out = audit.replay("HHG-006", traces, cases, tmp_path)
    assert "Replay HHG-006" in out and "ASSESS0 after detection" in out
    assert "card_history" in out and "sub_threshold_burst" in out and "Why it stopped" in out
    lines = out.splitlines()
    first_recall = next(i for i, l in enumerate(lines) if " RECALL " in l)
    assert next(i for i, l in enumerate(lines) if "ASSESS0" in l) < first_recall


def _write(d, name, obj):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(obj), encoding="utf-8")


def test_replay_synthetic_trace(tmp_path):
    cid = "TEST-AUDIT"
    trace = {
        "log": ["gathered: 3 card txns"],
        "tools": [
            {"step": 1, "tool": "card_history", "params": {"card_id": "C00001-K1"}, "ms": 3.2, "n_rows": 3,
             "result_digest": {"n_rows": 3, "ids": ["3000001", "3000002", "3000003"], "sha1": "abcdef0123456789"}},
            {"step": 4, "tool": "device_neighbors", "params": {"device_key": "X | Y", "t0": "2016-11-21 20:30:00", "days": "30"},
             "ms": 5.0, "n_rows": 2},
            {"step": 10, "tool": "similar_cases", "params": {"k": "3"}, "ms": 1.0, "n_rows": 3},
        ],
        "signals": [
            {"name": "device_shared", "group": "device", "llr": 1.1, "claim": "Device shared by 2 cards", "entity_ids": ["C00002-K1"],
             "ref": "query:device_neighbors(...)", "source": "graph", "how": {}},
            {"name": "customer_denies", "group": "customer", "llr": 2.2, "claim": "Customer denied", "entity_ids": [],
             "ref": "evidence_request:1", "source": "customer", "how": {}},
        ],
        "assess0": {"p": 0.6, "verdict": "uncertain", "fraud_groups": ["device"], "legit_groups": [], "by_group": {"device": 1.1}},
        "assess1": {"p": 0.9, "verdict": "fraud", "fraud_groups": ["device", "customer"], "legit_groups": [],
                    "by_group": {"device": 1.1, "customer": 2.2}},
    }
    answer = {"case_id": cid, "case": {"verdict": "fraud", "fraud_probability": 0.9, "pattern": "card_not_present_fraud", "status": "open"},
              "evidence_requests": [{"type": "customer_validation", "asked_after_step": 6, "assumed_response": "denied"}],
              "next_best_actions": {"initial": [], "final": [{"action": "BLOCK_CARD", "route": "L1", "reason": "R2"}],
                                    "what_changed": "The denial raised p."},
              "stop_reason": "settled"}
    side = {"backend": "duck", "dataset_version": {"version": "v123"},
            "claims": [{"id": "E1", "how": {"tool": "card_history", "params": {"card_id": "C00001-K1"}}},
                       {"id": "E2", "how": {"tool": "device_neighbors", "params": {"device_key": "X | Y", "t0": "2016-11-21 20:30:00", "days": 30}}},
                       {"id": "E3", "how": {"tool": "card_history", "params": {"card_id": "C99999-K1"}}}]}
    _write(tmp_path / "t", f"{cid}.json", trace)
    _write(tmp_path / "c", f"{cid}.json", answer)
    _write(tmp_path / "a", f"{cid}.json", side)

    d = audit.replay_data(cid, tmp_path / "t", tmp_path / "c", tmp_path / "a")
    kinds = [(r["type"], r.get("tool") or r.get("label")) for r in d["rows"]]
    assert kinds == [("tool", "card_history"), ("tool", "device_neighbors"), ("assessment", "ASSESS0 after detection"),
                     ("assessment", "ASSESS1 after the reply"), ("tool", "similar_cases")]
    tool_rows = [r for r in d["rows"] if r["type"] == "tool"]
    assert tool_rows[0]["claims"] == ["E1"] and tool_rows[1]["claims"] == ["E2"]
    a1 = d["rows"][3]
    assert a1["p_before"] == 0.6 and a1["moved"] == [{"group": "customer", "before": 0.0, "after": 2.2}]
    assert [s["name"] for s in a1["signals"]] == ["customer_denies"]

    out = audit.replay(cid, tmp_path / "t", tmp_path / "c", tmp_path / "a")
    for needle in ("3000001, 3000002, 3000003 sha1:abcdef01 -> E1", "dataset v123", "moved: customer +0.00 -> +2.20",
                   "evidence request: customer_validation after step 6", "What changed: The denial raised p."):
        assert needle in out, needle


def test_replay_missing_trace(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit.replay_data("NOPE-000", tmp_path, tmp_path, tmp_path)
