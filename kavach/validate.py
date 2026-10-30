"""`python -m kavach check`: validate every answer file against the answer format, Fraud Policy v1.0 and the
organizer grader (grader/grade.py), whose hard and policy rules are restated here in our own words.

Round 1 checks are all kept. Round 2 adds:
  * grader scope: affected ids and first_suspicious_txn_id must exist in the file the organizers pass to
    grade.py --transactions (transactions_2017q1.csv); first is the earliest affected txn by TransactionDT;
    exposure within the grader tolerance max(0.02, 0.5 percent) of the sum (our own check stays at 0.011);
  * policy: 3a report and case rules, R9 triple, R8 escalation, R1 on final blocks, tip-off, SAR length,
    what_changed when the action sets differ;
  * warnings (never failing): heuristics the grader prints for a judge to read by hand, plus our own.

Expected answer files come from the case pack of the round, never from a hardcoded range.
"""
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from kavach.answer import Answer, route_for
from kavach.config import RAW, RAW2, ROOT

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RULE_RE = re.compile(r"\bR(10|[1-9])\b|§\s*3a|§\s*6|3a\b")
BLOCKS = {"BLOCK_CARD", "BLOCK_ALL_CARDS"}
DASHES = (chr(0x2013), chr(0x2014))
AUTO = set(("ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER",
                    "STEP_UP_AUTH", "GENERATE_REPORT", "CREATE_CASE", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"))
# copied verbatim from grader/grade.py: nothing the customer sees may match this
TIPOFF = re.compile(r"\b(sar|suspicious activity report|file[d]? a report|regulator|fincen|"
                    r"reported to the authorit|law enforcement)\b", re.I)
GRADER = ROOT / "grader" / "grade.py"
MIN_SAR_WORDS = 40

# per round: the case pack (defines the expected files), the transactions file the grader sees, the closed cases
ROUNDS = {
    1: {"pack": RAW / "case_pack.csv", "scope": None, "closed": RAW / "closed_cases_history.csv",
        "txn": RAW / "transactions.csv", "ident": RAW / "identity.csv"},
    2: {"pack": RAW2 / "cases" / "case_pack_final.csv", "scope": RAW2 / "transactions_2017q1.csv",
        "closed": RAW2 / "closed_cases_history.csv", "txn": RAW2 / "transactions_2017q1.csv",
        "ident": RAW2 / "identity_2017q1.csv"},
}


def _p(path: Path) -> str:
    return str(path).replace("\\", "/")


def load_pack(round: int = 2) -> dict:
    """case_id -> case-pack row (all strings), read straight from the organizer CSV of that round."""
    with open(ROUNDS[round]["pack"], newline="", encoding="utf-8") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


@dataclass
class IdIndex:
    txn: dict = field(default_factory=dict)  # TransactionID(str) -> amount
    cards: set = field(default_factory=set)
    customers: set = field(default_factory=set)
    closed: set = field(default_factory=set)
    devices: set = field(default_factory=set)
    cases: dict = field(default_factory=dict)  # case_id -> case-pack row dict (this round only)
    extra: set = field(default_factory=set)  # other entity ids that legitimately exist (emails, regions)
    round: int = 2
    scope: dict | None = None  # grader scope: TransactionID(str) -> (amount, TransactionDT); None = not enforced
    scope_name: str = ""
    source: str = ""

    def exists(self, x: str) -> bool:
        return (x in self.txn or x in self.cards or x in self.customers or x in self.closed
                or x in self.devices or x in self.extra or x in self.cases)

    @classmethod
    def from_duck(cls, round: int = 2, duck_path: Path | str | None = None) -> "IdIndex":
        """Build from the DuckDB index (read-only). For Round 2 the grader scope comes from the grader_scope table;
        when the index has no January data (the Round 1 build), the January ids are unioned in from the raw files."""
        import duckdb

        if duck_path is None:
            from kavach.data.duck import connect

            con = connect()
        else:
            con = duckdb.connect(str(duck_path), read_only=True)
        idx = cls(round=round)
        tables = {r[0] for r in con.execute("show tables").fetchall()}
        idx.txn = {str(a): float(b) for a, b in con.execute("select TransactionID, TransactionAmt from txn").fetchall()}
        idx.cards = {r[0] for r in con.execute("select card_id from card_map").fetchall()}
        idx.customers = {r[0] for r in con.execute("select distinct customer_id from card_map").fetchall()}
        idx.closed = {r[0] for r in con.execute("select case_id from closed").fetchall()}
        idx.devices = {r[0] for r in con.execute("select distinct device_key from ident2").fetchall()}
        emails = con.execute("select distinct P_emaildomain from txn union select distinct R_emaildomain from txn").fetchall()
        regions = con.execute("select distinct cast(addr1 as varchar) from txn").fetchall()
        idx.extra = {r[0] for r in emails + regions if r[0]}
        idx.source = str(duck_path or "kavach.data.duck.connect()")
        spec = ROUNDS[round]
        if spec["scope"] is not None:
            idx.scope_name = spec["scope"].name
            if "grader_scope" in tables:
                idx.scope = {str(i): (float(a), int(d)) for i, a, d in con.execute(
                    "select g.TransactionID, t.TransactionAmt, t.TransactionDT "
                    "from grader_scope g join txn t using (TransactionID)").fetchall()}
                idx.source += " (grader_scope table)"
            elif spec["scope"].exists():
                _union_raw_round(con, idx, spec)
                idx.source += f" + raw {spec['scope'].name} (index has no grader_scope table)"
        con.close()
        idx.cases = load_pack(round) if spec["pack"].exists() else {}
        for row in idx.cases.values():
            if row.get("card_id"):
                idx.cards.add(row["card_id"])
            if row.get("customer_id"):
                idx.customers.add(row["customer_id"])
        return idx


def _union_raw_round(con, idx: IdIndex, spec: dict) -> None:
    """The index lacks this round's transactions: read them from the organizer files once (DuckDB read_csv)."""
    t = _p(spec["txn"])
    rows = con.execute(f"select cast(TransactionID as varchar), TransactionAmt, TransactionDT, customer_id, "
                       f"P_emaildomain, R_emaildomain, cast(addr1 as varchar) "
                       f"from read_csv('{t}', header=true, sample_size=-1)").fetchall()
    idx.scope = {}
    for tid, amt, dt, cust, pe, re_, addr in rows:
        idx.scope[tid] = (float(amt), int(dt))
        idx.txn[tid] = float(amt)
        if cust:
            idx.customers.add(cust)
        idx.extra.update(x for x in (pe, re_, addr) if x)
    if spec["ident"].exists():
        # device key exactly as kavach/data/duck.py builds it: DeviceInfo | id_30 | id_31 | id_33, nulls as ''
        idx.devices.update(r[0] for r in con.execute(
            "select distinct coalesce(DeviceInfo, '') || ' | ' || coalesce(id_30, '') || ' | ' || coalesce(id_31, '') "
            f"|| ' | ' || coalesce(id_33, '') from read_csv('{_p(spec['ident'])}', header=true, sample_size=-1)").fetchall())
    if spec["closed"].exists():
        with open(spec["closed"], newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                idx.closed.add(r["case_id"])
                idx.cards.add(r["card_id"])
                idx.customers.add(r["customer_id"])
                idx.cards.update(x for x in (r.get("connected_card_ids") or "").split("|") if x)


@lru_cache(maxsize=4)
def load_index(round: int = 2) -> IdIndex:
    """Cached IdIndex for the default DuckDB index (several checks per process read it once)."""
    return IdIndex.from_duck(round=round)


def _has_dash(obj) -> bool:
    return any(d in json.dumps(obj, ensure_ascii=False) for d in DASHES)


def _tipoff(text: str) -> str | None:
    m = TIPOFF.search(text or "")
    return m.group(0) if m else None


def review(raw: dict, idx: IdIndex | None, fname: str | None = None, strict: bool = True,
           round: int | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors fail the check; warnings are printed and never fail."""
    errs: list[str] = []
    warns: list[str] = []
    try:
        a = Answer.model_validate(raw)
    except ValidationError as e:
        return [f"schema: {x['loc']}: {x['msg']}" for x in e.errors()], []
    c, nba, sar = a.case, a.next_best_actions, a.sar
    rnd = round if round is not None else (idx.round if idx is not None else 2)
    meta = (idx.cases.get(a.case_id) if idx is not None else None) or {}

    if fname and fname != f"{a.case_id}.json":
        errs.append(f"file name {fname} != case_id {a.case_id}")
    if _has_dash(raw):
        errs.append("contains an en or em dash")

    final_actions = [x.action for x in nba.final]
    init_actions = [x.action for x in nba.initial]
    fin, ini = set(final_actions), set(init_actions)
    # routes
    for phase, lst in (("initial", nba.initial), ("final", nba.final)):
        for x in lst:
            want = route_for(x.action, c.exposure_usd)
            if x.route != want:
                errs.append(f"{phase}: {x.action} route {x.route} != {want}")
            if x.action in AUTO and x.route != "auto":
                warns.append(f"{phase}: {x.action} is an auto action but is routed {x.route}")
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
    # 3b: if the recommendation moved, say why
    if ini and fin and ini != fin and not nba.what_changed.strip():
        errs.append("3b: what_changed is empty but the final action set differs from the initial one")

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
        n_words = len(sar.narrative.split())
        if n_words < MIN_SAR_WORDS:
            errs.append(f"sar narrative has {n_words} words (want at least {MIN_SAR_WORDS})")
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

    # policy: R1 / R10 (Round 1 rules)
    if BLOCKS & ini and c.fraud_probability < 0.70 and len(c.evidence) < 2:
        errs.append("R1: block in initial on a single weak signal with p < 0.70")
    for x in nba.initial + nba.final:
        if x.action == "BLOCK_ALL_CARDS" and "R10" not in x.reason:
            errs.append("BLOCK_ALL_CARDS without R10 justification")
    if c.status == "escalated" and "ESCALATE_TO_ANALYST" not in final_actions:
        errs.append("status escalated but no ESCALATE_TO_ANALYST in final")
    for r in a.evidence_requests:
        if r.asked_after_step < 0:
            errs.append("asked_after_step negative")

    # policy: the grader's rules, in our own words
    p, exp = c.fraud_probability, c.exposure_usd
    if c.verdict == "fraud":
        if (c.connected_card_ids or exp > 1000) and "FILE_REPORT" not in fin:
            errs.append("3a: fraud with connected cards or exposure over 1000 requires FILE_REPORT in final")
        if "CREATE_CASE" not in fin:
            errs.append("3a: fraud requires CREATE_CASE in final")
    if c.pattern == "undocumented":
        missing = sorted({"CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"} - fin)
        if missing:
            errs.append(f"R9: undocumented pattern requires {missing} in final")
    if c.verdict == "uncertain" and exp > 500 and "ESCALATE_TO_ANALYST" not in fin:
        errs.append(f"R8: uncertain with exposure {exp} over 500 requires ESCALATE_TO_ANALYST in final")
    final_blocks = sorted(BLOCKS & fin)
    if final_blocks and p < 0.70 and meta.get("trigger_type") != "customer_report":
        if not {"VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"} & ini:
            errs.append(f"R1: {final_blocks} in final at probability {p} on a non-dispute trigger needs "
                        f"VERIFY_WITH_CUSTOMER or STEP_UP_AUTH in initial")
    # tip-off: nothing the customer sees (and, by our stricter rule, no summary or evidence claim) mentions a report
    spots = [("evidence_requests", json.dumps(raw.get("evidence_requests") or []))]
    spots += [(f"{ph} WARN_CUSTOMER reason", x.reason) for ph, lst in (("initial", nba.initial), ("final", nba.final))
              for x in lst if x.action == "WARN_CUSTOMER"]
    for where, text in spots:                    # customer-facing text: a tip-off is an error (the grader checks the same)
        hit = _tipoff(text)
        if hit:
            errs.append(f"tip-off: {where} mentions a report or regulator ({hit!r})")
    # internal text (case summary, evidence claims) never reaches the customer; flagged as a warning so a human can check
    # that nothing internal is copied into a customer message
    internal = [("summary", c.summary)] + [(f"evidence claim {i + 1}", e.claim) for i, e in enumerate(c.evidence)]
    internal_hits = [(w, _tipoff(t)) for w, t in internal if _tipoff(t)]

    # warnings (never failing)
    for w, hit in internal_hits:
        warns.append(f"internal {w} mentions a report ({hit!r}); fine internally, never copy it into a customer message")
    if "BLOCK_ALL_CARDS" in fin:
        warns.append("BLOCK_ALL_CARDS in final: the grader raises policy finding r10_block_all every time")
    if c.verdict == "fraud" and p < 0.5:
        warns.append(f"verdict fraud with probability {p} below 0.5")
    if c.verdict == "legitimate" and p > 0.5:
        warns.append(f"verdict legitimate with probability {p} above 0.5")
    flagged = str(meta.get("flagged_txn_id") or "")
    if c.verdict == "fraud" and flagged and c.affected_txn_ids and flagged not in c.affected_txn_ids:
        warns.append(f"flagged txn {flagged} is not among affected_txn_ids")
    if "FILE_REPORT" in fin and exp <= 1000 and not c.connected_card_ids and c.pattern != "undocumented":
        warns.append(f"FILE_REPORT with exposure {exp} (not over 1000) and no connected cards: 3a may not require it")

    # ids + exposure
    if idx is not None:
        case_row = idx.cases.get(a.case_id)
        if case_row is None:
            errs.append(f"case_id {a.case_id} not in case pack")
        missing = [t for t in c.affected_txn_ids if t not in idx.txn]
        if missing:
            errs.append(f"unknown txn ids: {missing}")
        else:
            s = round_(sum(abs(idx.txn[t]) for t in c.affected_txn_ids))
            if abs(s - exp) > 0.011:
                errs.append(f"exposure {exp} != sum(abs(amt)) {s}")
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
        if sar.file and case_row and abs(sar.total_amount_usd - exp) > 0.011:
            errs.append("sar.total_amount_usd != exposure_usd")
        # grader scope (Round 2): the organizers grade with the January file alone
        if rnd == 2 and idx.scope is not None:
            sc, name = idx.scope, idx.scope_name
            for t in c.affected_txn_ids:
                if t not in sc:
                    errs.append(f"grader scope: affected txn {t} is not in {name}")
            first = c.first_suspicious_txn_id
            if first and first not in sc:
                errs.append(f"grader scope: first_suspicious_txn_id {first} is not in {name}")
            known = [t for t in c.affected_txn_ids if t in sc]
            if known:
                calc = round_(sum(sc[t][0] for t in known))
                tol = max(0.02, calc * 0.005)
                if abs(calc - exp) > tol:
                    errs.append(f"grader: exposure_usd {exp} != sum of affected amounts {calc} (tolerance {tol:.2f})")
                if first and first in sc:
                    earliest = min(known, key=lambda t: sc[t][1])
                    if earliest != first:
                        errs.append(f"grader: first_suspicious_txn_id {first} is not the earliest affected txn "
                                    f"by TransactionDT ({earliest})")
    return errs, warns


def round_(x: float) -> float:
    return round(x, 2)


def validate_answer(raw: dict, idx: IdIndex | None, fname: str | None = None, strict: bool = True,
                    round: int | None = None) -> list[str]:
    """Return a list of human-readable errors (empty = valid). Warnings: see `review`."""
    return review(raw, idx, fname, strict, round)[0]


def run_grader(cases_dir: Path, round: int = 2, only_present: bool = False, out: Path | None = None,
               quiet: bool = False) -> dict:
    """Run the organizers' grader/grade.py exactly as they do, on a temp copy teams/kavach/cases.

    only_present=True writes a case pack restricted to the answer files present (used by tests on a single file);
    the default is the full pack, so a missing file is a hard finding. Returns the parsed report.json plus
    `summary_line` (the grader's own line for our team) and `team` (report['teams']['kavach'])."""
    spec = ROUNDS[round]
    cases_dir = Path(cases_dir)
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    py = py if py.exists() else Path(sys.executable)
    with tempfile.TemporaryDirectory(prefix="kavach_grade_") as tmp:
        tmp = Path(tmp)
        dst = tmp / "teams" / "kavach" / "cases"
        dst.mkdir(parents=True)
        present = []
        for f in sorted(cases_dir.glob("*.json")):
            shutil.copy2(f, dst / f.name)
            present.append(f.stem)
        pack = spec["pack"]
        if only_present:
            with open(pack, newline="", encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                rows = [r for r in rd if r["case_id"] in present]
                cols = rd.fieldnames
            pack = tmp / pack.name
            with open(pack, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
        txn = spec["scope"]
        if txn is None:
            txn = _slim_round1_txn(tmp)
        report = tmp / "report.json"
        cmd = [str(py), "-I", str(GRADER), "--teams-dir", str(tmp / "teams"), "--case-pack", str(pack),
               "--transactions", str(txn), "--out", str(report)]
        proc = subprocess.run(cmd, cwd=str(tmp), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"grader failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")
        rep = json.loads(report.read_text(encoding="utf-8"))
        if out is not None:
            Path(out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("kavach ")), proc.stdout.strip())
    rep["summary_line"] = line.strip()
    rep["team"] = rep["teams"]["kavach"]
    rep["transactions_file"] = str(txn)
    if not quiet:
        print(f"grader/grade.py --transactions {Path(str(txn)).name}: {rep['summary_line']}")
    return rep


def _slim_round1_txn(tmp: Path) -> Path:
    """Round 1 has no separate grader file; write TransactionID, TransactionAmt, TransactionDT of every Round 1 txn
    from the index (the only columns grade.py reads) rather than parsing the 700 MB CSV row by row."""
    from kavach.data.duck import connect

    con = connect()
    cols = {r[0] for r in con.execute("describe txn").fetchall()}
    where = "where round = 1" if "round" in cols else ""
    path = tmp / "transactions_round1_slim.csv"
    con.execute(f"copy (select TransactionID, TransactionAmt, TransactionDT from txn {where}) "
                f"to '{_p(path)}' (header, delimiter ',')")
    con.close()
    return path


def check(cases_dir: Path, strict_ids: bool = True, round: int = 2, grader: bool = False) -> bool:
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    con = Console()
    cases_dir = Path(cases_dir)
    idx = load_index(round) if strict_ids else None
    pack = idx.cases if idx is not None else load_pack(round)
    expected = sorted(f"{cid}.json" for cid in pack)
    files = sorted(cases_dir.glob("*.json"))
    names = [f.name for f in files]
    ok = True
    missing = sorted(set(expected) - set(names))
    extra = sorted(set(names) - set(expected))
    if missing or extra:
        con.print(f"[red]expected exactly the {len(expected)} files of the round {round} case pack "
                  f"({expected[0]}..{expected[-1]}); missing {missing}; not in the pack {extra}")
        ok = False
    if idx is not None and round == 2 and idx.scope is None:
        con.print("[yellow]grader scope unavailable (no grader_scope table and no January file): scope rule skipped")
    t = Table(title=f"kavach check (round {round}): {cases_dir}")
    for h in ("case", "verdict", "p", "pattern", "status", "exposure", "SAR", "final actions", "graph", "errors"):
        t.add_column(h)
    all_warns: list[tuple[str, str]] = []
    for f in files:
        if f.name not in expected:
            continue
        raw = json.loads(f.read_text(encoding="utf-8"))
        errs, warns = review(raw, idx, f.name, round=round)
        all_warns += [(f.stem, w) for w in warns]
        ok &= not errs
        try:
            c = raw["case"]
            fa = ",".join(x["action"] for x in raw["next_best_actions"]["final"])
            t.add_row(raw["case_id"], c["verdict"], f"{c['fraud_probability']:.2f}", c["pattern"], c["status"],
                      f"{c['exposure_usd']:.2f}", str(raw["sar"]["file"]), fa, str(c["written_to_graph"]),
                      "[green]ok" if not errs else "[red]" + escape("; ".join(errs)))
        except Exception:
            t.add_row(f.name, *[""] * 8, "[red]" + escape("; ".join(errs)))
    con.print(t)
    for cid, w in all_warns:
        con.print(f"[yellow]warning {cid}: {escape(w)}")
    if grader:
        rep = run_grader(cases_dir, round=round, quiet=True)
        team = rep["team"]
        con.print(f"[bold]organizer grader:[/bold] {escape(rep['summary_line'])}")
        for cid, per in team["cases"].items():
            for cls, code, msg in per["findings"]:
                colour = "yellow" if cls == "warning" else "red"
                con.print(f"[{colour}]grader {cls} {cid} {code}: {escape(msg)}")
        if team["hard"] or team["policy"]:
            ok = False
    con.print("[green]ALL VALID" if ok else "[red]VALIDATION FAILED")
    return ok


def main(argv: list[str] | None = None) -> int:
    """`python -m kavach.validate [cases_dir] [--round 2] [--grader] [--no-ids]` (same as `kavach check`)."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m kavach.validate")
    ap.add_argument("cases_dir", nargs="?", default="cases")
    ap.add_argument("--round", type=int, default=2, choices=sorted(ROUNDS))
    ap.add_argument("--grader", action="store_true")
    ap.add_argument("--no-ids", action="store_true")
    a = ap.parse_args(argv)
    d = Path(a.cases_dir)
    d = d if d.is_absolute() else ROOT / d
    return 0 if check(d, strict_ids=not a.no_ids, round=a.round, grader=a.grader) else 1


if __name__ == "__main__":
    raise SystemExit(main())
