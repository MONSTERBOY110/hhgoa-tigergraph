"""`python -m kavach check`: validate every answer file against the README format and Fraud Policy."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from kavach.answer import Answer, route_for

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RULE_RE = re.compile(r"\bR(10|[1-9])\b|§\s*3a|§\s*6|3a\b")
BLOCKS = {"BLOCK_CARD", "BLOCK_ALL_CARDS"}
DASHES = (chr(0x2013), chr(0x2014))


@dataclass
class IdIndex:
    txn: dict = field(default_factory=dict)  # TransactionID(str) -> amount
    cards: set = field(default_factory=set)
    customers: set = field(default_factory=set)
    closed: set = field(default_factory=set)
    devices: set = field(default_factory=set)
    cases: dict = field(default_factory=dict)  # case_id -> row dict
    extra: set = field(default_factory=set)  # other entity ids that legitimately exist (emails, regions)

    def exists(self, x: str) -> bool:
        return (x in self.txn or x in self.cards or x in self.customers or x in self.closed
                or x in self.devices or x in self.extra)

    @classmethod
    def from_duck(cls) -> "IdIndex":
        from kavach.data.duck import connect

        con = connect()
        idx = cls()
        idx.txn = {str(a): float(b) for a, b in con.execute("select TransactionID, TransactionAmt from txn").fetchall()}
        idx.cards = {r[0] for r in con.execute("select card_id from card_map").fetchall()}
        idx.customers = {r[0] for r in con.execute("select distinct customer_id from card_map").fetchall()}
        idx.closed = {r[0] for r in con.execute("select case_id from closed").fetchall()}
        idx.devices = {r[0] for r in con.execute("select distinct device_key from ident2").fetchall()}
        emails = con.execute("select distinct P_emaildomain from txn union select distinct R_emaildomain from txn").fetchall()
        regions = con.execute("select distinct cast(addr1 as varchar) from txn").fetchall()
        idx.extra = {r[0] for r in emails + regions if r[0]}
        cols = [d[0] for d in con.execute("select * from cases limit 0").description]
        idx.cases = {r[0]: dict(zip(cols, r)) for r in con.execute("select * from cases").fetchall()}
        con.close()
        return idx


def _has_dash(obj) -> bool:
    return any(d in json.dumps(obj, ensure_ascii=False) for d in DASHES)


def validate_answer(raw: dict, idx: IdIndex | None, fname: str | None = None, strict: bool = True) -> list[str]:
    """Return a list of human-readable errors (empty = valid)."""
    errs: list[str] = []
    try:
        a = Answer.model_validate(raw)
    except ValidationError as e:
        return [f"schema: {x['loc']}: {x['msg']}" for x in e.errors()]
    c, nba, sar = a.case, a.next_best_actions, a.sar

    if fname and fname != f"{a.case_id}.json":
        errs.append(f"file name {fname} != case_id {a.case_id}")
    if _has_dash(raw):
        errs.append("contains an en or em dash")

    final_actions = [x.action for x in nba.final]
    # routes
    for phase, lst in (("initial", nba.initial), ("final", nba.final)):
        for x in lst:
            want = route_for(x.action, c.exposure_usd)
            if x.route != want:
                errs.append(f"{phase}: {x.action} route {x.route} != {want}")
            if strict and not RULE_RE.search(x.reason):
                errs.append(f"{phase}: {x.action} reason cites no rule: {x.reason!r}")
        acts = [x.action for x in lst]
        if len(acts) != len(set(acts)):
            errs.append(f"{phase}: duplicate actions")
        if not lst:
            errs.append(f"{phase}: empty action list")

    # evidence requests vs initial/final
    if not a.evidence_requests:
        if nba.final != nba.initial:
            errs.append("no evidence requested but final != initial")
        if nba.what_changed != "nothing":
            errs.append("no evidence requested but what_changed != 'nothing'")

    # SAR consistency
    if sar.file != ("FILE_REPORT" in final_actions):
        errs.append("sar.file disagrees with FILE_REPORT in final")
    if not sar.file:
        if sar.narrative or sar.subjects or sar.total_amount_usd or sar.activity_dates:
            errs.append("sar.file false but narrative/subjects/amount/dates not empty")
    else:
        if not sar.narrative:
            errs.append("sar.file true but narrative empty")
        n_sent = len([s for s in re.split(r"(?<=[.!?])\s+", sar.narrative.strip()) if s])
        if not 6 <= n_sent <= 12:
            errs.append(f"sar narrative has {n_sent} sentences (want 6-12)")
        if len(sar.activity_dates) != 2 or not all(DATE_RE.match(d) for d in sar.activity_dates):
            errs.append(f"sar.activity_dates bad: {sar.activity_dates}")
        elif sar.activity_dates[0] > sar.activity_dates[1]:
            errs.append("sar.activity_dates out of order")
        if not sar.subjects:
            errs.append("sar.subjects empty")
    if "FILE_REPORT" in final_actions and "CREATE_CASE" not in final_actions:
        errs.append("FILE_REPORT without CREATE_CASE (a report always has a case behind it)")

    # verdict consistency
    if c.verdict == "legitimate":
        if c.affected_txn_ids or c.first_suspicious_txn_id or c.exposure_usd or sar.file:
            errs.append("legitimate but episode/exposure/sar not empty")
        if c.pattern != "none":
            errs.append("legitimate but pattern != none")
    else:
        if c.verdict == "fraud" and not c.affected_txn_ids:
            errs.append("fraud with no affected txns")
    if c.affected_txn_ids and c.first_suspicious_txn_id not in c.affected_txn_ids:
        errs.append("first_suspicious_txn_id not in affected_txn_ids")
    if c.pattern == "undocumented" and not c.pattern_description.strip():
        errs.append("undocumented without pattern_description")
    if c.pattern != "undocumented" and c.pattern_description:
        errs.append("pattern_description must be empty unless undocumented")
    if c.written_to_graph != bool(c.graph_case_id):
        errs.append("written_to_graph and graph_case_id disagree")
    if len(set(c.affected_txn_ids)) != len(c.affected_txn_ids):
        errs.append("duplicate affected_txn_ids")

    # policy: R1 / R10
    init_actions = [x.action for x in nba.initial]
    groups = {e.source for e in c.evidence}
    if BLOCKS & set(init_actions) and c.fraud_probability < 0.70 and len(c.evidence) < 2:
        errs.append("R1: block in initial on a single weak signal with p < 0.70")
    for x in nba.initial + nba.final:
        if x.action == "BLOCK_ALL_CARDS" and "R10" not in x.reason:
            errs.append("BLOCK_ALL_CARDS without R10 justification")
    if c.status == "escalated" and "ESCALATE_TO_ANALYST" not in final_actions:
        errs.append("status escalated but no ESCALATE_TO_ANALYST in final")
    for r in a.evidence_requests:
        if r.asked_after_step < 0:
            errs.append("asked_after_step negative")
    _ = groups

    # ids + exposure
    if idx is not None:
        case_row = idx.cases.get(a.case_id)
        if case_row is None:
            errs.append(f"case_id {a.case_id} not in case pack")
        missing = [t for t in c.affected_txn_ids if t not in idx.txn]
        if missing:
            errs.append(f"unknown txn ids: {missing}")
        else:
            s = round(sum(abs(idx.txn[t]) for t in c.affected_txn_ids), 2)
            if abs(s - c.exposure_usd) > 0.011:
                errs.append(f"exposure {c.exposure_usd} != sum(abs(amt)) {s}")
        if c.first_suspicious_txn_id and c.first_suspicious_txn_id not in idx.txn:
            errs.append("unknown first_suspicious_txn_id")
        for x in c.connected_card_ids:
            if x not in idx.cards:
                errs.append(f"unknown card {x}")
        for x in c.connected_device_profiles:
            if x not in idx.devices:
                errs.append(f"unknown device profile {x!r}")
        for x in c.similar_prior_cases:
            if x not in idx.closed:
                errs.append(f"unknown closed case {x}")
        for e in c.evidence:
            for x in e.entity_ids:
                if not idx.exists(x):
                    errs.append(f"evidence entity id not in dataset: {x}")
        for x in sar.subjects:
            if not idx.exists(x):
                errs.append(f"sar subject not in dataset: {x}")
        if sar.file and case_row and abs(sar.total_amount_usd - c.exposure_usd) > 0.011:
            errs.append("sar.total_amount_usd != exposure_usd")
    return errs


def check(cases_dir: Path, strict_ids: bool = True) -> bool:
    from rich.console import Console
    from rich.table import Table

    con = Console()
    idx = IdIndex.from_duck() if strict_ids else None
    files = sorted(cases_dir.glob("*.json"))
    expected = [f"HHG-{i:03d}.json" for i in range(1, 21)]
    ok = True
    names = [f.name for f in files]
    if names != expected:
        con.print(f"[red]expected exactly {expected[0]}..{expected[-1]}, got {names}")
        ok = False
    t = Table(title=f"kavach check: {cases_dir}")
    for h in ("case", "verdict", "p", "pattern", "status", "exposure", "SAR", "final actions", "graph", "errors"):
        t.add_column(h)
    for f in files:
        raw = json.loads(f.read_text(encoding="utf-8"))
        errs = validate_answer(raw, idx, f.name)
        ok &= not errs
        try:
            c = raw["case"]
            fa = ",".join(x["action"] for x in raw["next_best_actions"]["final"])
            t.add_row(raw["case_id"], c["verdict"], f"{c['fraud_probability']:.2f}", c["pattern"], c["status"],
                      f"{c['exposure_usd']:.2f}", str(raw["sar"]["file"]), fa, str(c["written_to_graph"]),
                      "[green]ok" if not errs else "[red]" + "; ".join(errs))
        except Exception:
            t.add_row(f.name, *[""] * 8, "[red]" + "; ".join(errs))
    con.print(t)
    con.print("[green]ALL VALID" if ok else "[red]VALIDATION FAILED")
    return ok
