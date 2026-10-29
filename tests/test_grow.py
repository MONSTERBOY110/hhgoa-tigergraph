"""Self-growing detective on a synthetic bank: two planted patterns, the miner must find both blind; the approval
gate must refuse a non-L2 token; approved specs load and fire at runtime through the tool registry interface."""
import json
import math
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

import duckdb
import numpy as np
import pandas as pd
import pytest

from kavach.grow import miner, registry, templates

T0 = datetime(2016, 7, 1)


# ----------------------------------------------------------------------------------------------------------------
# synthetic bank

def _bank(tmp_path):
    rng = np.random.default_rng(7)
    rows, tid = [], [5000000]

    def add(card, ts, amt, channel="online", prod="W", remail="gmail.com", dev=None):
        tid[0] += 1
        rows.append({"TransactionID": tid[0], "card_id": card, "customer_id": card[:6], "ts": ts, "TransactionAmt": round(float(amt), 2),
                     "ProductCD": prod, "channel": channel, "device_key": dev or f"dev-{card}", "id_15": "Found", "id_23": None,
                     "P_emaildomain": "gmail.com", "R_emaildomain": remail if channel == "online" else None,
                     "addr1": float(100 + int(card[1:6]) % 20), "holder_key": f"{card}|{100 + int(card[1:6]) % 20}|1"})
        return tid[0]

    cards = [f"C{i:05d}-K1" for i in range(300)]
    for c in cards:  # background: ordinary spending over 120 days
        for _ in range(25):
            ts = T0 + timedelta(days=float(rng.uniform(0, 120)), minutes=float(rng.uniform(0, 1440)))
            add(c, ts, rng.lognormal(3.5, 0.8) % 300 + 1, "online" if rng.random() < 0.5 else "in_person",
                remail=["gmail.com", "yahoo.com", "hotmail.com"][int(rng.integers(0, 3))])
    closed, k = [], [0]

    def case(outcome, pattern, ids):
        k[0] += 1
        closed.append({"case_id": f"CC-{k[0]:04d}", "customer_id": "C00000", "card_id": "C00000-K1",
                       "opened_at": str(T0 + timedelta(days=121)), "closed_at": "", "outcome": outcome, "pattern": pattern,
                       "first_fraud_txn_id": str(ids[0]) if outcome == "confirmed_fraud" else None, "txn_ids": "|".join(map(str, ids)),
                       "n_txns": str(len(ids)), "exposure_usd": "0", "connected_card_ids": None, "actions_taken": "",
                       "report_filed": "No", "analyst_notes": "synthetic"})

    band_cases, fan_cases = [], []
    for i in range(30):  # planted 1: four online purchases just under $500 within 20 minutes
        c = cards[i]
        ts = T0 + timedelta(days=70 + i, hours=3)
        ids = [add(c, ts + timedelta(minutes=5 * j), rng.uniform(455, 499)) for j in range(4)]
        case("confirmed_fraud", "card_not_present_fraud", ids)
        band_cases.append(closed[-1]["case_id"])
    for i in range(30):  # planted 2: a never-seen recipient domain paid by four different cards on one day
        day = T0 + timedelta(days=72 + i, hours=10)
        ids = [add(cards[100 + 4 * (i % 25) + j], day + timedelta(minutes=30 * j), rng.uniform(40, 200), remail="evil-recipient.net")
               for j in range(4)]
        case("confirmed_fraud", "card_not_present_fraud", [ids[0]])
        fan_cases.append(closed[-1]["case_id"])
    df = pd.DataFrame(rows)
    plain = df[~df.TransactionID.isin([int(x) for c in closed for x in c["txn_ids"].split("|")])]
    for t in plain.sample(20, random_state=1).TransactionID:
        case("confirmed_fraud", "card_not_present_fraud", [int(t)])
    for t in plain.sample(40, random_state=2).TransactionID:
        case("cleared", "none", [int(t)])
    path = tmp_path / "bank.duckdb"
    con = duckdb.connect(str(path))
    con.register("df", df)
    con.execute("create table txn as select * from df")
    cl = pd.DataFrame(closed).astype(object)
    con.register("cl", cl)
    con.execute("create table closed as select * from cl")
    con.close()
    csv = tmp_path / "closed.csv"
    cl.to_csv(csv, index=False)
    return path, csv, df, band_cases, fan_cases


@pytest.fixture(scope="module")
def bank(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("grow")
    path, csv, df, band, fan = _bank(tmp)
    cands = miner.mine(duck_path=path, labels_path=csv, sample=5000, forward=False, verbose=False)
    return SimpleNamespace(path=path, csv=csv, df=df, band=band, fan=fan, cands=cands)


# ----------------------------------------------------------------------------------------------------------------
# a ToolRegistry stand-in over the synthetic index (same call(name, **kw) interface, traced)

class FakeTools:
    backend = "fake"

    def __init__(self, path):
        self.con = duckdb.connect(str(path), read_only=True)
        self._extra, self._impl, self.trace, self.calls = {}, self, [], 0

    COLS = """TransactionID, card_id, ts, TransactionAmt amt, ProductCD, channel, device_key, id_15, id_23, P_emaildomain,
              R_emaildomain, cast(addr1 as integer) addr1, holder_key"""

    def call(self, name, **kw):
        self.calls += 1
        self.trace.append({"tool": name, "params": kw})
        return getattr(self, name)(**kw)

    def register(self, name, fn):
        self._extra[name] = fn

    def card_window(self, card_id, t0, hours_before=48, hours_after=48):
        t0 = pd.Timestamp(t0).to_pydatetime()
        return self.con.execute(f"select {self.COLS} from txn where card_id = ? and ts between ? and ? order by ts",
                                [card_id, t0 - timedelta(hours=hours_before), t0 + timedelta(hours=hours_after)]).df()

    def card_history(self, card_id):
        return self.con.execute(f"select {self.COLS} from txn where card_id = ? order by ts", [card_id]).df()

    def device_txns(self, device_key, t0, days=30):
        t0 = pd.Timestamp(t0).to_pydatetime()
        return self.con.execute(f"select {self.COLS} from txn where device_key = ? and ts between ? and ? order by ts",
                                [device_key, t0 - timedelta(days=days), t0 + timedelta(days=days)]).df()

    def grow_entity_txns(self, entity, value, t0, days=7):
        t0 = pd.Timestamp(t0).to_pydatetime()
        col = "cast(addr1 as integer)" if entity == "addr1" else entity
        return self.con.execute(f"select {self.COLS} from txn where {col} = ? and ts between ? and ? order by ts",
                                [value, t0 - timedelta(days=days), t0 + timedelta(days=days)]).df()

    def grow_entity_popularity(self, entity, value):
        col = "cast(addr1 as integer)" if entity == "addr1" else entity
        return int(self.con.execute(f"select count(distinct card_id) from txn where {col} = ?", [value]).fetchone()[0])

    def grow_entity_daily_cards(self, entity, value, d_from, d_to):
        col = "cast(addr1 as integer)" if entity == "addr1" else entity
        return self.con.execute(f"""select distinct cast(ts as date) d, card_id from txn where {col} = ?
                                    and cast(ts as date) between ? and ? order by 1, 2""",
                                [value, pd.Timestamp(d_from).date(), pd.Timestamp(d_to).date()]).df()

    def grow_date_range(self):
        d0, d1 = self.con.execute("select cast(min(ts) as date), cast(max(ts) as date) from txn").fetchone()
        return {"d0": str(d0), "d1": str(d1)}


def _ctx(bank, tid, tools=None):
    r = bank.df[bank.df.TransactionID == tid].iloc[0].to_dict()
    r["amt"] = r["TransactionAmt"]
    r["addr1"] = int(r["addr1"])
    return SimpleNamespace(f=r, card_id=r["card_id"], t0=pd.Timestamp(r["ts"]), device=r["device_key"], device_pop=1,
                           tools=tools or FakeTools(bank.path))


# ----------------------------------------------------------------------------------------------------------------
# fake approvals (kavach.harness.approvals is built by another stream; the contract is approve(...) -> dict or Forbidden)

class Forbidden(Exception):
    pass


@pytest.fixture
def approvals(monkeypatch, tmp_path):
    log = []
    tokens = {"analyst-token": "analyst", "l1-token": "L1", "l2-token": "L2"}
    route = {"FILE_REPORT": "L2", "CLOSE_NO_FRAUD": "auto"}

    def approve(case_id, action, token, exposure=0.0, note=""):
        role, need = tokens.get(token), route[action]
        ok = role == "L2" if need == "L2" else role is not None
        log.append({"case_id": case_id, "action": action, "role": role, "ok": ok})
        if not ok:
            raise Forbidden(f"{action} on {case_id} needs {need}, token role is {role}")
        return {"id": f"APR-{len(log):04d}", "case_id": case_id, "action": action, "route": need, "role": role,
                "ts": "2026-10-29T12:00:00Z", "approver": "abc123", "hash": f"h{len(log)}"}

    mod = types.ModuleType("kavach.harness.approvals")
    mod.approve = approve
    pkg = sys.modules.get("kavach.harness")
    if pkg is None:
        try:
            import kavach.harness as pkg  # noqa: F811
        except ImportError:
            pkg = types.ModuleType("kavach.harness")
            pkg.__path__ = []
            monkeypatch.setitem(sys.modules, "kavach.harness", pkg)
    monkeypatch.setattr(pkg, "approvals", mod, raising=False)
    monkeypatch.setitem(sys.modules, "kavach.harness.approvals", mod)
    monkeypatch.setenv("KAVACH_DETECTORS_DIR", str(tmp_path / "detectors"))
    monkeypatch.setenv("KAVACH_NO_LLM", "1")
    return log


def _find(cands, cases, family=None):
    best = None
    for c in cands:
        cov = len(set(c.fraud_case_ids) & set(cases)) / len(cases)
        if (family is None or c.family in family) and (best is None or cov > best[1]):
            best = (c, cov)
    return best


# ----------------------------------------------------------------------------------------------------------------

def test_miner_finds_both_planted_patterns_blind(bank):
    band = _find(bank.cands, bank.band, ("band_burst",))
    fan = _find(bank.cands, bank.fan, ("fanin", "shared_entity"))
    assert band and band[1] >= 0.9, [(c.name, c.support) for c in bank.cands]
    assert band[0].params["hi"] == 500.0 and band[0].support >= 15 and band[0].lift >= 3 and band[0].cleared_hits <= 3
    assert fan and fan[1] >= 0.9, [(c.name, c.support) for c in bank.cands]
    assert fan[0].params["entity"] == "R_emaildomain" and fan[0].support >= 15 and fan[0].lift >= 3
    # nothing accepted is a near-duplicate of another accepted candidate
    for i, a in enumerate(bank.cands):
        for b in bank.cands[:i]:
            ov = len(set(a.fraud_case_ids) & set(b.fraud_case_ids)) / max(len(a.fraud_case_ids), 1)
            assert ov < miner.NOVELTY


def test_backtest_numbers_are_consistent(bank):
    c = _find(bank.cands, bank.band, ("band_burst",))[0]
    bt = c.backtest()
    assert bt["support"] == len(c.fraud_case_ids) and bt["cleared_hits"] == len(bt["cleared_case_ids"])
    assert math.isclose(bt["precision"], c.support / (c.support + c.cleared_hits), abs_tol=1e-3)
    assert all(str(x).isdigit() for x in bt["example_txn_ids"])
    assert c.status == "rediscovery" and c.rediscovers == "sub_threshold_burst"   # the hand-written detector's shape


def test_runtime_apply_matches_sql_backtest(bank):
    con = duckdb.connect()
    templates.prepare(con, bank.path)
    tools = FakeTools(bank.path)
    sample = list(bank.df.TransactionID.sample(40, random_state=3)) + list(bank.df.TransactionID[bank.df.R_emaildomain == "evil-recipient.net"][:8])
    for fam_name, cases in (("band_burst", bank.band), ("fanin", bank.fan)):
        c = _find(bank.cands, cases, (fam_name, "shared_entity"))[0]
        fam = templates.FAMILIES[c.family]
        fired = fam.fire(con, c.params, sample).set_index("TransactionID").fired
        spec = {"name": c.name, "family": c.family, "params": c.params, "backtest": c.backtest(), "approval": None}
        for tid in sample:
            got = bool(fam.apply(_ctx(bank, int(tid), tools), spec))
            assert got == bool(fired[int(tid)]), (c.name, tid)
    con.close()


def test_approval_gate_and_runtime_signals(bank, approvals, monkeypatch):
    band = _find(bank.cands, bank.band, ("band_burst",))[0]
    fan = _find(bank.cands, bank.fan, ("fanin", "shared_entity"))[0]
    paths = registry.propose([band, fan], include_rediscoveries=True, duck_path=bank.path)
    assert len(paths) == 2
    spec = json.loads(paths[0].read_text(encoding="utf-8"))
    assert spec["status"] == "proposed" and spec["approval"] is None and spec["description_source"] == "template"
    assert spec["backtest"]["support"] == band.support and spec["dataset_version"]["index"] == "bank.duckdb"
    assert chr(0x2014) not in spec["description"] and chr(0x2013) not in spec["description"]

    # analyst and L1 tokens are refused: FILE_REPORT routes to L2; nothing moves
    for tok in ("analyst-token", "l1-token", "forged"):
        with pytest.raises(Forbidden):
            registry.approve(band.name, tok)
    assert registry.load_approved(verbose=False) == []
    assert (registry._dir("proposed") / f"{band.name}.json").exists()

    rec = registry.approve(band.name, "l2-token", note="reviewed backtest")
    assert rec["role"] == "L2" and rec["id"].startswith("APR-")
    assert approvals[-1]["action"] == "FILE_REPORT" and approvals[-1]["case_id"] == f"DETECTOR-{band.name}"
    loaded = registry.load_approved(verbose=False)
    assert [s["name"] for s in loaded] == [band.name]

    # the rejected one goes to rejected/ with the reason, recorded through the same chain
    registry.reject(fan.name, "analyst-token", "too close to an existing typology")
    assert (registry._dir("rejected") / f"{fan.name}.json").exists()

    # runtime: the approved detector fires on a planted burst transaction, cites its approval and backtest
    first_case = pd.read_csv(bank.csv, dtype=str).set_index("case_id").loc[bank.band[0]]
    tid = int(first_case.first_fraud_txn_id)
    ctx = _ctx(bank, tid)
    sigs = registry.signals_for(ctx)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.name == f"grown:{band.name}" and s.group == "sequence"
    assert s.llr == round(min(2.0, 0.5 * math.log(band.lift)), 2)
    assert rec["id"] in s.claim and f"{band.support} confirmed-fraud hits" in s.claim
    assert s.how["tool"] == "card_window" and s.how["params"]["card_id"] == ctx.card_id
    assert str(tid) in s.entity_ids
    assert ctx.tools.trace[-1]["tool"] == "card_window"

    # a background transaction does not fire
    plain = int(bank.df[(bank.df.TransactionAmt < 300) & (bank.df.R_emaildomain == "gmail.com")].TransactionID.iloc[0])
    assert registry.signals_for(_ctx(bank, plain)) == []

    # KAVACH_GROWN=0 disables grown detectors (the ablation's off arm)
    monkeypatch.setenv("KAVACH_GROWN", "0")
    assert registry.signals_for(_ctx(bank, tid)) == []
    monkeypatch.setenv("KAVACH_GROWN", "1")

    # an approved file edited after approval, or one without an L2 approval, is refused at load
    p = registry._dir("approved") / f"{band.name}.json"
    good = p.read_text(encoding="utf-8")
    bad = json.loads(good)
    bad["params"]["n"] = 1
    p.write_text(json.dumps(bad), encoding="utf-8")
    assert registry.load_approved(verbose=False) == []
    bad = json.loads(good)
    bad["approval"]["role"] = "analyst"
    p.write_text(json.dumps(bad), encoding="utf-8")
    assert registry.load_approved(verbose=False) == []
    p.write_text(good, encoding="utf-8")
    assert len(registry.load_approved(verbose=False)) == 1


def test_missing_tool_on_other_backend_is_recorded_not_silent(bank, approvals):
    fan = _find(bank.cands, bank.fan, ("fanin",))
    if fan is None:
        pytest.skip("fan-in found by shared_entity only")
    c = fan[0]
    spec = {"name": c.name, "family": c.family, "params": c.params, "backtest": c.backtest(), "approval": {"id": "APR-1", "role": "L2"}}
    tools = FakeTools(bank.path)
    tools._impl = object()                       # a backend without the grow_* tools
    ctx = _ctx(bank, int(bank.df[bank.df.R_emaildomain == "evil-recipient.net"].TransactionID.iloc[2]), tools)
    with pytest.raises(templates.ToolUnavailable):
        templates.apply(ctx, spec)


def test_seed_clustering_maps_analyst_language_to_families():
    notes = pd.DataFrame({"case_id": [f"CC-{i:04d}" for i in range(6)], "pattern": "undocumented", "analyst_notes": [
        "cardholder C00001 reported four online purchases within forty minutes, each just under $500",
        "cardholder C00002 reported four online purchases within forty minutes, each just under $500",
        "cardholder C00003 reported four online purchases within forty minutes, each just under $500",
        "purchases came from a Samsung device behind an anonymous proxy, a device never seen on this account",
        "purchases came from a Samsung device behind an anonymous proxy, a device never seen on this account",
        "purchases came from a Samsung device behind an anonymous proxy, a device never seen on this account"]})
    from kavach import vectors
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=384), rng.normal(size=384)
    vecs = np.vstack([a + 0.01 * rng.normal(size=384) for _ in range(3)] + [b + 0.01 * rng.normal(size=384) for _ in range(3)])
    vecs = (vecs / np.linalg.norm(vecs, axis=1, keepdims=True)).astype(np.float32)
    old = vectors._index
    vectors._index = {"case_ids": np.array(notes.case_id), "case_vecs": vecs}
    try:
        seeds = miner.seed_from_notes(notes)
    finally:
        vectors._index = old
    assert len(seeds) == 2
    fams = {tuple(sorted(s["case_ids"])): s["families"][0]["family"] for s in seeds}
    assert fams[("CC-0000", "CC-0001", "CC-0002")] == "band_burst"
    assert fams[("CC-0003", "CC-0004", "CC-0005")] == "new_device_proxy"
    assert all("<ID>" in s["exemplar"] or "Samsung" in s["exemplar"] for s in seeds)


def test_every_family_grid_is_small_and_slugs_are_unique():
    for fam in templates.FAMILIES.values():
        g = fam.grid()
        assert 20 <= len(g) <= 60, (fam.name, len(g))
        slugs = [fam.slug(p) for p in g]
        assert len(set(slugs)) == len(slugs), fam.name
        assert all(set(s) <= set("abcdefghijklmnopqrstuvwxyz0123456789_") for s in slugs)


def test_real_approvals_chain_gate(bank, monkeypatch, tmp_path):
    """Same gate against the harness's real approvals module (role from token, hash chain), when it is present."""
    approvals = pytest.importorskip("kavach.harness.approvals")
    from kavach.harness.faults import Forbidden as RealForbidden
    monkeypatch.setenv("KAVACH_APPROVALS_FILE", str(tmp_path / "approvals.jsonl"))
    monkeypatch.setenv("KAVACH_DETECTORS_DIR", str(tmp_path / "detectors"))
    monkeypatch.setenv("KAVACH_NO_LLM", "1")
    monkeypatch.setenv("KAVACH_TOKEN_ANALYST", "analyst-token-0123456789")
    monkeypatch.setenv("KAVACH_TOKEN_L1", "l1-token-0123456789abcd")
    monkeypatch.setenv("KAVACH_TOKEN_L2", "l2-token-0123456789abcd")
    c = _find(bank.cands, bank.band, ("band_burst",))[0]
    registry.propose([c], include_rediscoveries=True, duck_path=bank.path)
    for tok in ("analyst-token-0123456789", "l1-token-0123456789abcd", "not-a-token"):
        with pytest.raises(RealForbidden):
            registry.approve(c.name, tok)
    assert registry.load_approved(verbose=False) == []
    rec = registry.approve(c.name, "l2-token-0123456789abcd", note="test")
    assert rec["role"] == "L2" and rec["route"] == "L2"
    assert [s["name"] for s in registry.load_approved(verbose=False)] == [c.name]
    recs = approvals.records(f"DETECTOR-{c.name}")
    assert [r["status"] for r in recs] == ["denied", "denied", "denied", "approved"]
    assert approvals.verify()[0]
