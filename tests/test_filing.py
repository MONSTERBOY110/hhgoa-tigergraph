"""Challenge 4, evidence to SAR (kavach.filing): section 3a gate, citations on every sentence, fail closed, verify.

Answers come from kavach.harness.runner.run_case with KAVACH_NO_LLM=1 and KAVACH_RECORD=0, written to a temp folder
with their attestation sidecars; nothing is written to cases/, attestations/, sar/ or data/."""
import copy
import json
import os
import re

import pytest

from kavach import filing
from kavach.config import DUCK_PATH
from kavach.llm import DASH_RE

pytestmark = pytest.mark.skipif(not DUCK_PATH.exists(), reason="DuckDB index not built")

# cases that filed a report in the dry run of 30 Oct 00:12 (decisions belong to the agent; the fixture takes whichever file)
CANDIDATES = ("HHF-006", "HHF-003", "HHF-001", "HHF-002")
ENV = {"KAVACH_NO_LLM": "1", "KAVACH_RECORD": "0", "KAVACH_CASEFILE_EVENTS": "0"}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """{case_id: (answer, sidecar)} for every candidate case that files a report today."""
    from kavach import duckq
    from kavach.harness.runner import run_case
    base = tmp_path_factory.mktemp("filing")
    env = {**ENV, "KAVACH_INCIDENTS_DIR": str(base / "incidents"), "KAVACH_APPROVALS_FILE": str(base / "approvals.jsonl")}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        rows = {r["case_id"]: r for r in duckq.case_pack(2).to_dict("records")}
        out = {}
        for cid in CANDIDATES:
            if cid not in rows:
                continue
            o = run_case(rows[cid], previous={}, quiet=True)
            if o.answer is not None and o.answer["sar"]["file"]:
                out[cid] = (o.answer, o.trace["attestation"])
        yield {"base": base, "cases": out}
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _stage(tmp_path, answer, sidecar):
    cd, ad, od = tmp_path / "cases", tmp_path / "att", tmp_path / "sar"
    cd.mkdir(exist_ok=True)
    ad.mkdir(exist_ok=True)
    cid = answer["case_id"]
    (cd / f"{cid}.json").write_text(json.dumps(answer), encoding="utf-8")
    if sidecar is not None:
        (ad / f"{cid}.json").write_text(json.dumps(sidecar, default=str), encoding="utf-8")
    return cd, ad, od


def _one(run, connected=None):
    for cid, (a, s) in run["cases"].items():
        if connected is None or bool(a["case"]["connected_card_ids"]) == connected:
            return copy.deepcopy(a), copy.deepcopy(s)
    if connected is False:
        # no filing without connected cards today: the same answer with its connected cards removed still needs a
        # report on exposure (> $1,000) and exercises the no-ring layout
        for cid, (a, s) in run["cases"].items():
            if a["case"]["exposure_usd"] > filing.SAR_EXPOSURE:
                a = copy.deepcopy(a)
                a["case"]["connected_card_ids"], a["case"]["connected_device_profiles"] = [], []
                return a, copy.deepcopy(s)
    pytest.skip("no case in the pack files a report of this kind today")


# ---------------------------------------------------------------- section 3a
def _ans(verdict="fraud", p=0.9, exposure=100.0, conn=(), devs=(), pattern="card_not_present_fraud", file=False, final=None):
    final = final if final is not None else (["CREATE_CASE", "FILE_REPORT"] if file else ["CREATE_CASE"])
    return {"case_id": "X-1", "case": {"verdict": verdict, "fraud_probability": p, "exposure_usd": exposure,
                                        "connected_card_ids": list(conn), "connected_device_profiles": list(devs), "pattern": pattern},
            "sar": {"file": file, "narrative": "Some text." if file else "", "reason": "R2 and §3a"},
            "next_best_actions": {"final": [{"action": x, "route": "auto"} for x in final]}}


@pytest.mark.parametrize("kw,required", [
    (dict(verdict="legitimate", p=0.03, exposure=0.0), False),
    (dict(exposure=23.99), False),                                     # fraud, small, documented, alone: case only
    (dict(exposure=23.0, conn=["C00001-K1"]), True),                   # the $23 loss that connects to other cards
    (dict(exposure=1000.0), False),                                    # "over $1,000" is strict
    (dict(exposure=1000.01), True),
    (dict(verdict="uncertain", p=0.72, exposure=1500.0), True),        # strongly suspected
    (dict(verdict="uncertain", p=0.61, exposure=8551.47), False),      # not strongly suspected: no report
    (dict(exposure=100.0, pattern="undocumented"), True),              # R9
    (dict(exposure=100.0, devs=["A | B | C | D"]), True),              # shared device profile
])
def test_section_3a(kw, required):
    s = filing.section_3a(_ans(**kw))
    assert s["required"] is required
    assert s["reason"].startswith("§3a: report owed" if required else "§3a: no report")


def test_decide_refuses_flags_missing_and_checks_actions():
    assert filing.decide(_ans(exposure=23.99, file=True))[0] == filing.REFUSED
    st, why, _ = filing.decide(_ans(exposure=5000.0, file=False))
    assert st == filing.MISSING and "requires a report but sar.file is false" in why
    st, why, _ = filing.decide(_ans(exposure=5000.0, file=True, final=["CREATE_CASE"]))
    assert st == filing.REFUSED and "FILE_REPORT is not in the final actions" in why
    st, why, _ = filing.decide(_ans(exposure=5000.0, file=True, final=["FILE_REPORT"]))
    assert st == filing.REFUSED and "CREATE_CASE" in why
    assert filing.decide(_ans(exposure=5000.0, file=True))[0] == filing.FILED
    assert filing.decide(_ans(verdict="legitimate", p=0.02, exposure=0.0))[0] == filing.NOT_REQUIRED


# ---------------------------------------------------------------- filings
def _check_outputs(od, cid, answer):
    md = (od / f"{cid}.md").read_text(encoding="utf-8")
    page = (od / f"{cid}.html").read_text(encoding="utf-8")
    assert not DASH_RE.search(md) and not DASH_RE.search(page)
    assert filing.NO_TIPOFF[:60] in md
    # every narrative sentence ends in a footnote and every footnote names a claim and ids
    narr = filing._section(md, "4. Narrative").strip().split("\n\n")[0]
    pieces = filing.MARK.split(narr)
    assert pieces[-1].strip() == "" and all(x.strip() for x in pieces[:-1:2])
    for n, body in filing.FN_MD.findall(md):
        assert filing.CLAIM_MD.findall(body) and filing.TICK.findall(body), (n, body)
    # timeline: exactly the affected transactions
    tl = re.findall(r"^\| `(\d+)`", filing._section(md, "5. Transaction timeline"), re.M)
    assert sorted(tl) == sorted(answer["case"]["affected_txn_ids"])
    # self-contained page with an inline SVG built around the case card
    assert "<svg" in page and "<style>" in page and not re.search(r'(?:src|href)="(?!#)|<link\b|<script\b', page)
    card = answer["case"]["affected_txn_ids"] and re.search(r"Card</th><td><code>([^<]+)</code>", page).group(1)
    assert f'class="t-case">{card}<' in page
    for c in answer["case"]["connected_card_ids"][: filing.CARDS_IN_GRAPH]:
        assert f">{c}</text>" in page
    return md, page


@pytest.mark.parametrize("connected", [True, False])
def test_build_and_verify(run, tmp_path, connected):
    answer, side = _one(run, connected)
    cid = answer["case_id"]
    cd, ad, od = _stage(tmp_path, answer, side)
    row = filing.build(cid, cd, ad, od)
    assert row["status"] == filing.FILED, row
    assert row["sentences"] >= 6 and row["dropped"] == 0 and row["checks_failed"] == []
    _check_outputs(od, cid, answer)
    res = filing.verify(cid, od, ad, cd, rerun=True)
    assert [r for r in res if r["status"] == "fail"] == []
    assert any(r["check"].startswith("re-run S") for r in res)


def test_connected_cards_carry_their_link(run, tmp_path):
    answer, side = _one(run, True)
    cd, ad, od = _stage(tmp_path, answer, side)
    f = filing.model(answer["case_id"], answer, side, filing.section_3a(answer))
    assert f["link"] and f["link"]["per_card"]
    linked = [x for x in f["connected"] if x["n_link"]]
    assert linked and all(x["via"] == f["link"]["value"] and x["first_txn"] for x in linked)
    g = filing.graph(f)
    # a card hangs off the linking element only when the data shows it shares that element
    for cd_, x in zip(g["cards"], f["connected"]):
        assert (cd_["element"] == 0) == bool(x["n_link"])


def test_refused_filing_writes_nothing_and_logs_why(run, tmp_path):
    answer, side = _one(run)
    cid = answer["case_id"]
    c = answer["case"]
    c.update({"pattern": "card_not_present_fraud", "connected_card_ids": [], "connected_device_profiles": [],
              "exposure_usd": 500.0})
    cd, ad, od = _stage(tmp_path, answer, side)
    od.mkdir()
    (od / f"{cid}.md").write_text("stale", encoding="utf-8")            # a stale filing must not survive
    row = filing.build(cid, cd, ad, od)
    assert row["status"] == filing.REFUSED and "does not require a report" in row["reason"]
    assert not (od / f"{cid}.md").exists() and not (od / f"{cid}.html").exists()
    m = json.loads(filing.write_manifest([row], od).read_text(encoding="utf-8"))
    assert m["cases"][0]["status"] == filing.REFUSED and m["counts"]["refused"] == 1


def test_fail_closed_without_sidecar_or_with_stale_sidecar(run, tmp_path):
    answer, side = _one(run)
    cid = answer["case_id"]
    cd, ad, od = _stage(tmp_path, answer, None)
    row = filing.build(cid, cd, ad, od)
    assert row["status"] == filing.ERROR and "no attestation sidecar" in row["reason"]
    assert not (od / f"{cid}.html").exists()
    answer["sar"]["narrative"] = answer["sar"]["narrative"] + " An extra sentence nobody attested."
    cd, ad, od = _stage(tmp_path, answer, side)
    row = filing.build(cid, cd, ad, od)
    assert row["status"] == filing.ERROR and "do not reproduce the answer's narrative" in row["reason"]


def test_affected_txn_missing_from_index_fails_closed(run, tmp_path):
    answer, side = _one(run)
    answer["case"]["affected_txn_ids"] = answer["case"]["affected_txn_ids"] + ["3999999"]
    cd, ad, od = _stage(tmp_path, answer, side)
    row = filing.build(answer["case_id"], cd, ad, od)
    assert row["status"] == filing.ERROR and "3999999" in row["reason"]


def test_sentence_without_ids_is_rewritten_from_the_template(run, tmp_path):
    answer, side = _one(run)
    cid = answer["case_id"]
    s_claims = [c for c in side["claims"] if c["kind"] == "sar_sentence"]
    target = next(c for c in s_claims if c["evidence"]["customer_ids"] and not c["evidence"]["txn_ids"])
    target["evidence"] = {k: [] for k in target["evidence"]}            # this sentence now cites nothing
    cd, ad, od = _stage(tmp_path, answer, side)
    row = filing.build(cid, cd, ad, od)
    assert row["status"] == filing.FILED and row["rewritten"] == 1, row
    md, _ = _check_outputs(od, cid, answer)
    assert f"Rewritten from the template: the recorded sentence **{target['id']}** cited no ids" in md
    assert [r for r in filing.verify(cid, od, ad, cd, rerun=False) if r["status"] == "fail"] == []


def test_verify_catches_a_tampered_footnote(run, tmp_path):
    answer, side = _one(run)
    cid = answer["case_id"]
    cd, ad, od = _stage(tmp_path, answer, side)
    assert filing.build(cid, cd, ad, od)["status"] == filing.FILED
    p = od / f"{cid}.md"
    md = p.read_text(encoding="utf-8")
    first_txn = answer["case"]["affected_txn_ids"][0]
    p.write_text(md.replace(f"[^2]: ", f"[^2]: Transactions: `3999999`. ", 1), encoding="utf-8")
    bad = {r["check"] for r in filing.verify(cid, od, ad, cd, rerun=False) if r["status"] == "fail"}
    assert {"footnote 2", "footnote ids in the index", "html footnotes match markdown"} <= bad
    p.write_text(md.replace("[^1]", "", 1), encoding="utf-8")         # sentence 1 loses its citation
    bad = {r["check"] for r in filing.verify(cid, od, ad, cd, rerun=False) if r["status"] == "fail"}
    assert "markers and footnotes agree" in bad
    p.write_text(md.replace(f"`{first_txn}`", "`3660000`", 1), encoding="utf-8")
    assert any(r["status"] == "fail" for r in filing.verify(cid, od, ad, cd, rerun=False))


def test_cli(run, tmp_path, capsys):
    answer, side = _one(run)
    cid = answer["case_id"]
    cd, ad, od = _stage(tmp_path, answer, side)
    args = ["--cases-dir", str(cd), "--attest-dir", str(ad), "--out", str(od)]
    assert filing.main([cid] + args) == 0
    assert filing.main(["verify", cid, "--no-rerun"] + args) == 0
    assert filing.main(["--all"] + args) == 0
    out = capsys.readouterr().out
    assert f"{cid}: filed" in out and "0 failed check(s)" in out
    assert json.loads((od / "manifest.json").read_text(encoding="utf-8"))["counts"]["filed"] == 1
