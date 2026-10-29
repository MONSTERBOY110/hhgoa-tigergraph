"""Static viewer export: build() runs on the files on disk and writes valid script assignments; a case with no trace,
no attestation sidecar and no case file still exports, with honest not-available states; the page itself names no
case id, loads nothing from a CDN and carries no em or en dash."""
import json
import re
from pathlib import Path

from kavach import site
from kavach.config import ROOT

HEAD = re.compile(r"^window\.KAVACH = (.*);\n$", re.S)
DETAIL = re.compile(r'^window\.KAVACH_DETAIL = window\.KAVACH_DETAIL \|\| \{\};\nwindow\.KAVACH_DETAIL\[("[^"]+")\] = (.*);\n$', re.S)


def _cases_js(out: Path) -> dict:
    m = HEAD.match((out / "cases.js").read_text(encoding="utf-8"))
    assert m, "cases.js is not a single window.KAVACH assignment"
    return json.loads(m.group(1))


def _detail_js(path: Path) -> tuple[str, dict]:
    m = DETAIL.match(path.read_text(encoding="utf-8"))
    assert m, f"{path.name} is not a window.KAVACH_DETAIL assignment"
    return json.loads(m.group(1)), json.loads(m.group(2))


def _answer(case_id: str) -> dict:
    return {"case_id": case_id,
            "case": {"status": "closed_legitimate", "verdict": "legitimate", "fraud_probability": 0.05, "pattern": "none",
                     "pattern_description": "", "affected_txn_ids": [], "first_suspicious_txn_id": "",
                     "connected_card_ids": [], "connected_device_profiles": [], "exposure_usd": 0.0,
                     "evidence": [{"claim": "A synthetic evidence row", "source": "graph", "ref": "query:none", "entity_ids": []}],
                     "similar_prior_cases": [], "summary": "Synthetic case for the viewer export test.",
                     "written_to_graph": False, "graph_case_id": ""},
            "evidence_requests": [], "next_best_actions": {"initial": [], "final": [], "what_changed": "nothing"},
            "sar": {"file": False, "reason": "no fraud"}, "stop_reason": "test", "tool_calls": 0, "tokens": 0, "latency_s": 0.0}


def test_build_on_current_files(tmp_path):
    out = tmp_path / "site"
    summary = site.build(out=out, verify=False, quiet=True)
    data = _cases_js(out)
    assert set(data) >= {"meta", "rounds", "cases", "monitor", "detectors"}
    on_disk = set()
    for src in site.discover_sources():
        d = Path(src["dir"])
        for f in d.glob("*.json") if d.exists() else []:
            a = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(a, dict) and "case_id" in a and isinstance(a.get("case"), dict):
                on_disk.add(a["case_id"])
    ids = [a["case_id"] for a in data["cases"]]
    assert set(ids) == on_disk and len(ids) == len(set(ids)) == summary["cases"]
    assert sum(r["n"] for r in data["rounds"]) == len(ids)
    files = sorted((out / "data").glob("*.js"))
    assert {p.stem for p in files} == set(ids)
    for p in files:
        cid, d = _detail_js(p)
        assert cid == p.stem
        for k in ("replay", "attestation", "verify", "casefile"):
            assert isinstance(d[k], dict) and "available" in d[k]
            if not d[k]["available"]:
                assert d[k].get("reason"), f"{cid} {k}: a missing piece must say why"
        assert d["ring"] is None or "available" in d["ring"]
    for a in data["cases"]:
        assert set(a["_avail"]) >= {"replay", "attestation", "casefile", "ring"}
        assert len(a["_evidence_claims"]) == len(a["case"]["evidence"])


def test_case_without_trace_or_sidecar_still_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("KAVACH_CASEFILE_DIR", str(tmp_path / "casefiles"))
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "TEST-SITE-001.json").write_text(json.dumps(_answer("TEST-SITE-001")), encoding="utf-8")
    (cases / "notes.json").write_text(json.dumps({"not": "an answer"}), encoding="utf-8")
    src = [{"dir": cases, "traces": tmp_path / "traces", "attest": tmp_path / "attestations", "round": 2}]
    out = tmp_path / "site"
    (out / "data").mkdir(parents=True)
    (out / "data" / "GONE-001.js").write_text("window.KAVACH_DETAIL = {};\n", encoding="utf-8")

    summary = site.build(out=out, sources=src, verify=True, quiet=True)
    assert summary["cases"] == 1 and summary["replays"] == 0 and summary["attestations"] == 0
    data = _cases_js(out)
    (a,) = data["cases"]
    assert a["case_id"] == "TEST-SITE-001" and a["_round"] == 2
    assert a["_avail"]["replay"] is False and a["_avail"]["attestation"] is False and a["_avail"]["casefile"] is False
    assert a["_avail"]["ring"] is None and a["_evidence_claims"] == [None]
    cid, d = _detail_js(out / "data" / "TEST-SITE-001.js")
    assert "no trace" in d["replay"]["reason"]
    assert "no attestation sidecar" in d["attestation"]["reason"]
    assert d["verify"]["available"] is False
    assert "no case file" in d["casefile"]["reason"]
    assert d["ring"] is None
    assert not (out / "data" / "GONE-001.js").exists(), "stale detail files of vanished cases are removed"


def test_ring_frames_only_for_rings():
    assert site.ring_frames(_answer("TEST-SITE-002")) is None


def test_page_is_data_driven_and_offline():
    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1]
    assert not re.search(r"\b(HHF|HHG|MON)-\d{3}\b", script), "the viewer logic must not name a case id"
    assert not re.search(r"<script[^>]+src=\"https?:", html) and "fonts.googleapis" not in html
    assert '<script src="cases.js"></script>' in html
    for p in [ROOT / "site" / "index.html", ROOT / "kavach" / "site.py", Path(__file__)]:
        text = p.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, f"{p.name} contains an em or en dash"
