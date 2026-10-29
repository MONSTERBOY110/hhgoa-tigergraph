"""Schema drift survival (brief challenge 12): detect what changed in a data delivery, explain what it breaks in this
codebase, propose the re-mapping, and gate ingestion on a recorded human approval.

    python -m kavach.drift detect              # compare data/raw (Round 1) with data/raw/round2, write the report
    python -m kavach.drift approve --token X   # record an approval through kavach.harness.approvals (role from the token)
    python -m kavach.drift status              # exit 0 only when the current Round 2 files carry an approval

Read only: raw files are read with DuckDB read_csv, the Round 1 index is attached read-only. Nothing is ingested here;
`kavach index` asks `is_approved()` before it builds over the Round 2 files. The approval is bound to a fingerprint
(sha256 over the Round 2 file names and sizes), so a changed delivery needs a fresh detect and a fresh approval.
"""
import argparse
import hashlib
import importlib
import json
import sys
import time
from pathlib import Path

import duckdb

from kavach.config import DATA, RAW, RAW2, ROOT

DRIFT_JSON = DATA / "drift.json"
REPORT_MD = ROOT / "docs" / "round2" / "drift-report.md"
R1_INDEX = DATA / "kavach.duckdb"          # the Round 1 index (July to December 2016), source of the frozen card_map

# logical input -> file name patterns, searched recursively under each round folder (first match by depth wins)
ROLES = {
    "transactions": ["transactions*.csv"],
    "identity": ["identity*.csv"],
    "closed_cases": ["closed_cases*.csv"],
    "case_pack": ["case_pack*.csv"],
    "policy": ["*policy*.md", "README.md"],
    "answer_format": ["answer_format*.md"],
    "grader": ["grade*.py"],
}
CSV_ROLES = ("transactions", "identity", "closed_cases", "case_pack")
LIST_MAX = 50


def _p(path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def _files(root: Path, exclude: Path | None = None) -> list[Path]:
    out = []
    for f in sorted(Path(root).rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        if exclude is not None and Path(exclude).exists() and Path(exclude).resolve() in f.resolve().parents:
            continue
        out.append(f)
    return out


def _find(files: list[Path], root: Path, patterns: list[str]) -> Path | None:
    for pat in patterns:
        hits = sorted([f for f in files if f.match(pat)], key=lambda f: (len(f.relative_to(root).parts), str(f)))
        if hits:
            return hits[0]
    return None


def fingerprint(round2_dir=None) -> str:
    """sha256 over the Round 2 file names (relative, posix) and sizes."""
    root = Path(round2_dir or RAW2)
    h = hashlib.sha256()
    for f in _files(root):
        h.update(f"{f.relative_to(root).as_posix()}:{f.stat().st_size}\n".encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# detect

def _schema(con, path: Path, sample_size: int) -> list[list[str]]:
    return [[r[0], r[1]] for r in con.execute(
        f"describe select * from read_csv('{_p(path)}', header=true, sample_size={int(sample_size)})").fetchall()]


def _schema_diff(con, s1, s2, path2: Path) -> dict:
    c1, c2 = [c for c, _ in s1], [c for c, _ in s2]
    t1, t2 = dict(s1), dict(s2)
    common1 = [c for c in c1 if c in t2]
    common2 = [c for c in c2 if c in t1]
    moved = [c for c in common1 if common1.index(c) != common2.index(c)]
    types = []
    for c in common1:
        if t1[c] != t2[c]:
            # would casting the new file's values to the Round 1 type lose anything?
            lost = con.execute(f"""select count(*) from read_csv('{_p(path2)}', header=true, all_varchar=true)
                                   where "{c}" is not null and try_cast("{c}" as {t1[c]}) is null""").fetchone()[0]
            types.append({"column": c, "round1": t1[c], "round2": t2[c], "cast_loss": int(lost)})
    return {"n_cols": [len(c1), len(c2)], "added": [c for c in c2 if c not in t1], "removed": [c for c in c1 if c not in t2],
            "order_same": not moved and c1 == c2, "moved": moved[:LIST_MAX], "type_changes": types}


def _col(cols, name: str, cast: str) -> str:
    return f'try_cast("{name}" as {cast}) as "{name}"' if name in cols else f'cast(null as {cast}) as "{name}"'


def _load_txn(con, table: str, path: Path, cols: list[str]) -> None:
    sel = ", ".join([_col(cols, "TransactionID", "bigint"), _col(cols, "TransactionDT", "bigint"), _col(cols, "ts", "timestamp"),
                     _col(cols, "customer_id", "varchar"), _col(cols, "card6", "varchar"), _col(cols, "risk_score", "double")])
    con.execute(f"create or replace temp table {table} as select {sel} from read_csv('{_p(path)}', header=true, all_varchar=true)")


def _txn_stats(con, table: str) -> dict:
    r = con.execute(f"""select count(*), min(ts), max(ts), min(TransactionID), max(TransactionID), min(TransactionDT),
                        max(TransactionDT), count(distinct customer_id), avg(risk_score), quantile_cont(risk_score, 0.9)
                        from {table}""").fetchone()
    months = dict(con.execute(f"select strftime(ts, '%Y-%m') m, count(*) from {table} where ts is not null group by 1 order by 1").fetchall())
    return {"rows": r[0], "ts": [str(r[1]), str(r[2])], "TransactionID": [r[3], r[4]], "TransactionDT": [r[5], r[6]],
            "customers": r[7], "risk_mean": None if r[8] is None else round(r[8], 4),
            "risk_p90": None if r[9] is None else round(r[9], 4), "by_month": months}


def _card_maps(con, r1_index) -> str:
    con.execute("""create or replace temp table cm_union as
                   with g as (select distinct customer_id, coalesce(card6, '') k from txu)
                   select customer_id, k, customer_id || '-K' || row_number() over (partition by customer_id order by k) card_id from g""")
    if r1_index and Path(r1_index).exists():
        try:
            con.execute(f"attach '{_p(r1_index)}' as r1idx (read_only)")
            con.execute("create or replace temp table cm_r1 as select customer_id, k, card_id from r1idx.card_map")
            con.execute("detach r1idx")
            return f"{Path(r1_index).name} card_map (read-only)"
        except duckdb.Error:
            pass
    con.execute("""create or replace temp table cm_r1 as
                   with g as (select distinct customer_id, coalesce(card6, '') k from tx1)
                   select customer_id, k, customer_id || '-K' || row_number() over (partition by customer_id order by k) card_id from g""")
    return "recomputed from the Round 1 transactions file"


def _card_map_report(con, source: str) -> dict:
    shifted = con.execute("""select u.customer_id, u.k, m.card_id, u.card_id from cm_union u join cm_r1 m using (customer_id, k)
                             where u.card_id <> m.card_id order by 1, 2""").fetchall()
    new_pairs = con.execute("""select u.customer_id, u.k, u.card_id from cm_union u anti join cm_r1 m using (customer_id, k)
                               order by 1, 2""").fetchall()
    new_cust = con.execute("select count(distinct customer_id) from cm_union where customer_id not in (select customer_id from cm_r1)").fetchone()[0]
    # closed cases in the new history: does the rule reproduce their card_id on every listed transaction?
    cc = con.execute("""with x as (select case_id, card_id, try_cast(unnest(string_split(txn_ids, '|')) as bigint) TransactionID
                                   from cc2 where txn_ids is not null and txn_ids <> '')
                        select x.case_id, x.TransactionID, x.card_id stated, m.card_id resolved
                        from x left join txu t using (TransactionID) left join cm_union m on m.customer_id = t.customer_id and m.k = coalesce(t.card6, '')""").fetchall()
    broken = sorted({r[0] for r in cc if r[3] is not None and r[2] != r[3]})
    missing = [r[1] for r in cc if r[3] is None]
    return {"source_round1": source, "pairs_round1": con.execute("select count(*) from cm_r1").fetchone()[0],
            "pairs_union": con.execute("select count(*) from cm_union").fetchone()[0],
            "shifted": len(shifted), "shifted_cards": [{"customer_id": a, "card6": b or None, "round1": c, "union": d} for a, b, c, d in shifted[:LIST_MAX]],
            "new_pairs": len(new_pairs), "new_pair_cards": [{"customer_id": a, "card6": b or None, "card_id": c} for a, b, c in new_pairs[:LIST_MAX]],
            "new_customers": new_cust,
            "closed_txns_checked": len(cc), "closed_txns_agree": sum(1 for r in cc if r[3] is not None and r[2] == r[3]),
            "closed_cases_broken": len(broken), "closed_cases_broken_ids": broken[:LIST_MAX],
            "closed_txns_missing": len(missing), "closed_txns_missing_ids": missing[:LIST_MAX]}


def _closed_diff(con, cols1, cols2, pack1: list[dict]) -> dict:
    common = [c for c in cols1 if c in cols2 and c != "case_id"]
    sig = "md5(concat_ws('|', " + ", ".join(f"coalesce(\"{c}\", '')" for c in common) + "))" if common else "''"
    ids1 = {r[0] for r in con.execute("select case_id from cc1").fetchall()}
    ids2 = {r[0] for r in con.execute("select case_id from cc2").fetchall()}
    added, removed = sorted(ids2 - ids1), sorted(ids1 - ids2)
    changed = [r[0] for r in con.execute(f"""select a.case_id from (select case_id, {sig} s from cc1) a
                                             join (select case_id, {sig} s from cc2) b using (case_id) where a.s <> b.s order by 1""").fetchall()]
    con.execute("create or replace temp table cc_added as select * from cc2 where case_id not in (select case_id from cc1)")
    mix = lambda col: dict(con.execute(f'select coalesce("{col}", \'\'), count(*) from cc_added group by 1 order by 2 desc').fetchall()) if col in cols2 else {}
    # added cases that are the Round 1 case-pack investigations: the flagged transaction is listed in the case, or (cleared
    # cases list no transactions) the same card opened at the same second
    links = []
    sql = ("select case_id, card_id, coalesce(txn_ids, ''), coalesce(first_fraud_txn_id, ''), coalesce(opened_at, '') "
           "from cc_added order by 1")
    for row in con.execute(sql).fetchall() if {"card_id", "txn_ids", "first_fraud_txn_id", "opened_at"} <= set(cols2) else []:
        for p in pack1:
            fid = str(p.get("flagged_txn_id") or "")
            if fid and (fid in row[2].split("|") or fid == row[3]) or (p.get("card_id") == row[1] and p.get("opened_at") == row[4]):
                links.append({"closed_case_id": row[0], "pack_case_id": p["case_id"], "card_id": row[1]})
                break
    by_month = lambda t: dict(con.execute(f"select substr(opened_at, 1, 7), count(*) from {t} group by 1 order by 1").fetchall()) \
        if "opened_at" in (cols1 if t == "cc1" else cols2) else {}
    return {"rows": [len(ids1), len(ids2)], "added": len(added), "added_ids": added[:LIST_MAX], "removed": len(removed),
            "removed_ids": removed[:LIST_MAX], "changed": len(changed), "changed_ids": changed[:LIST_MAX],
            "added_outcomes": mix("outcome"), "added_patterns": mix("pattern"),
            "added_opened": list(con.execute("select min(opened_at), max(opened_at) from cc_added").fetchone()) if "opened_at" in cols2 else [],
            "added_linked_to_round1_pack": links, "cases_by_month": {"round1": by_month("cc1"), "round2": by_month("cc2")}}


def _label_coverage(con) -> list[dict]:
    lab = lambda t: f"""(select distinct try_cast(unnest(string_split(txn_ids, '|')) as bigint) TransactionID,
                        outcome = 'confirmed_fraud' fraud from {t} where txn_ids is not null and txn_ids <> '')"""
    rows = con.execute(f"""
        with l1 as {lab('cc1')}, l2 as {lab('cc2')}
        select strftime(t.ts, '%Y-%m') m, count(*) n,
               count(*) filter (where l1.TransactionID is not null) lab1, count(*) filter (where l1.fraud) fraud1,
               count(*) filter (where l2.TransactionID is not null) lab2, count(*) filter (where l2.fraud) fraud2
        from txu t left join l1 using (TransactionID) left join l2 using (TransactionID)
        where t.ts is not null group by 1 order by 1""").fetchall()
    return [{"month": m, "txns": n, "labelled_round1": a, "fraud_round1": b, "labelled_round2": c, "fraud_round2": d,
             "labelled_share_round2": round(c / n, 4) if n else 0.0} for m, n, a, b, c, d in rows]


def _pack_diff(con, pack1: list[dict], pack2: list[dict]) -> dict:
    ids1, ids2 = {p["case_id"] for p in pack1}, {p["case_id"] for p in pack2}
    mix = lambda pk: {k: sum(1 for p in pk if p.get("trigger_type") == k) for k in sorted({p.get("trigger_type") for p in pk})}
    prefix = lambda ids: sorted({i.rsplit("-", 1)[0] for i in ids if "-" in i})
    out = {"rows": [len(pack1), len(pack2)], "ids_round1": sorted(ids1), "ids_round2": sorted(ids2),
           "added": sorted(ids2 - ids1), "removed": sorted(ids1 - ids2), "prefix": [prefix(ids1), prefix(ids2)],
           "trigger_mix": [mix(pack1), mix(pack2)], "opened": [], "flagged_in": {}, "card_mismatch": [], "flagged_missing": []}
    if pack2:
        out["opened"] = [min(p.get("opened_at") or "" for p in pack2), max(p.get("opened_at") or "" for p in pack2)]
    # where does each Round 2 flagged transaction live, and does the stated card match the transaction's own card?
    for p in pack2:
        tid = p.get("flagged_txn_id")
        r = con.execute("""select t.src, t.customer_id, t.card6, m.card_id from txu t
                           left join cm_union m on m.customer_id = t.customer_id and m.k = coalesce(t.card6, '')
                           where t.TransactionID = try_cast(? as bigint)""", [tid]).fetchone()
        if r is None:
            out["flagged_missing"].append({"case_id": p["case_id"], "flagged_txn_id": tid})
            continue
        out["flagged_in"][r[0]] = out["flagged_in"].get(r[0], 0) + 1
        if r[3] != p.get("card_id") or r[1] != p.get("customer_id"):
            out["card_mismatch"].append({"case_id": p["case_id"], "flagged_txn_id": tid, "stated_card": p.get("card_id"),
                                         "stated_customer": p.get("customer_id"), "resolved_card": r[3], "card6": r[2],
                                         "flag": "alert_card_mismatch"})
    try:
        intake = importlib.import_module("kavach.intake")
        hostile = []
        for p in pack2:
            it = intake.parse(dict(p))
            if getattr(it, "hostile", None):
                hostile.append({"case_id": p["case_id"], "markers": len(it.hostile)})
        out["hostile"] = {"checked": len(pack2), "rows_with_markers": len(hostile), "cases": hostile}
    except Exception as e:  # intake is another stream's module; the drift report must not depend on it
        out["hostile"] = {"skipped": f"kavach.intake not usable: {type(e).__name__}: {str(e)[:120]}"}
    return out


def _identity(con) -> dict:
    out = {}
    for rnd in (1, 2):
        n, cov, orph = con.execute(f"""select (select count(*) from tx{rnd}),
                                       (select count(*) from tx{rnd} where TransactionID in (select TransactionID from id{rnd})),
                                       (select count(*) from id{rnd} where TransactionID not in (select TransactionID from tx{rnd}))""").fetchone()
        out[f"round{rnd}"] = {"txns": n, "with_identity": cov, "share": round(cov / n, 4) if n else 0.0, "orphan_identity_rows": orph}
    out["round2_identity_on_round1_txns"] = con.execute("select count(*) from id2 where TransactionID in (select TransactionID from tx1)").fetchone()[0]
    return out


def _artifacts(union_rows: int) -> list[dict]:
    """Derived files built from the Round 1 inputs: are they stale for the union?"""
    out = []
    for name, sql in (("kavach.duckdb", "select count(*) from a.txn"), ("features.parquet", None)):
        path = DATA / name
        if not path.exists():
            continue
        try:
            if sql:
                c = duckdb.connect()
                c.execute(f"attach '{_p(path)}' as a (read_only)")
                n = c.execute(sql).fetchone()[0]
                c.close()
            else:
                n = duckdb.connect().execute(f"select count(*) from '{_p(path)}'").fetchone()[0]
            out.append({"artifact": f"data/{name}", "rows": n, "union_rows": union_rows, "stale": n != union_rows})
        except duckdb.Error as e:
            out.append({"artifact": f"data/{name}", "error": str(e)[:120]})
    return out


def detect(round1_dir=None, round2_dir=None, r1_index=R1_INDEX, sample_size: int = -1, artifacts: bool = True) -> dict:
    t0 = time.perf_counter()
    r1, r2 = Path(round1_dir or RAW), Path(round2_dir or RAW2)
    f1, f2 = _files(r1, exclude=r2), _files(r2)
    inv = {"round1_dir": r1.as_posix(), "round2_dir": r2.as_posix(),
           "round1": [{"file": f.relative_to(r1).as_posix(), "bytes": f.stat().st_size} for f in f1],
           "round2": [{"file": f.relative_to(r2).as_posix(), "bytes": f.stat().st_size} for f in f2], "roles": {}}
    paths = {}
    for role, pats in ROLES.items():
        a, b = _find(f1, r1, pats), _find(f2, r2, pats)
        paths[role] = (a, b)
        inv["roles"][role] = {"round1": a.relative_to(r1).as_posix() if a else None, "round2": b.relative_to(r2).as_posix() if b else None,
                              "renamed": bool(a and b and a.name != b.name), "moved": bool(a and b and a.relative_to(r1).parent.as_posix() != b.relative_to(r2).parent.as_posix())}
    missing = [r for r in CSV_ROLES if not all(paths[r])]
    if missing:
        raise FileNotFoundError(f"drift detect needs both rounds of {missing}; inventory: {inv['roles']}")

    con = duckdb.connect()
    schema = {}
    for role in CSV_ROLES:
        a, b = paths[role]
        s1, s2 = _schema(con, a, sample_size), _schema(con, b, sample_size)
        schema[role] = {"round1": s1, "round2": s2, "diff": _schema_diff(con, s1, s2, b)}
    cols = lambda role, i: [c for c, _ in schema[role]["round1" if i == 1 else "round2"]]

    _load_txn(con, "tx1", paths["transactions"][0], cols("transactions", 1))
    _load_txn(con, "tx2", paths["transactions"][1], cols("transactions", 2))
    con.execute("create or replace temp view txu as select *, 1 as src from tx1 union all select *, 2 as src from tx2")
    for i in (1, 2):
        con.execute(f"""create or replace temp table id{i} as select {_col(cols('identity', i), 'TransactionID', 'bigint')}
                        from read_csv('{_p(paths['identity'][i - 1])}', header=true, all_varchar=true)""")
        con.execute(f"create or replace temp table cc{i} as select * from read_csv('{_p(paths['closed_cases'][i - 1])}', header=true, all_varchar=true)")
    pk = [con.execute(f"select * from read_csv('{_p(paths['case_pack'][i])}', header=true, all_varchar=true)").df().to_dict("records") for i in (0, 1)]

    s1, s2 = _txn_stats(con, "tx1"), _txn_stats(con, "tx2")
    rows = {role: [con.execute(f"select count(*) from read_csv('{_p(p)}', header=true, all_varchar=true)").fetchone()[0] for p in paths[role]]
            for role in ("identity", "closed_cases", "case_pack")}
    rows["transactions"] = [s1["rows"], s2["rows"]]
    overlap = con.execute("select count(*) from tx2 where TransactionID in (select TransactionID from tx1)").fetchone()[0]
    new_cust = con.execute("select count(distinct customer_id) from tx2 where customer_id not in (select customer_id from tx1)").fetchone()[0]
    ranges = {"round1": s1, "round2": s2, "id_overlap": overlap,
              "dt_continues": bool(s1["TransactionDT"][1] is not None and s2["TransactionDT"][0] is not None and s2["TransactionDT"][0] > s1["TransactionDT"][1]),
              "round2_customers_new": new_cust}

    source = _card_maps(con, r1_index)
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": fingerprint(r2),
        "inventory": inv,
        "schema": schema,
        "rows": rows,
        "ranges": ranges,
        "identity": _identity(con),
        "closed": _closed_diff(con, cols("closed_cases", 1), cols("closed_cases", 2), pk[0]),
        "label_coverage": _label_coverage(con),
        "case_pack": _pack_diff(con, pk[0], pk[1]),
        "card_map": _card_map_report(con, source),
        "artifacts": _artifacts(s1["rows"] + s2["rows"]) if artifacts else [],
    }
    con.close()
    report["elapsed_s"] = round(time.perf_counter() - t0, 1)
    return report


# ---------------------------------------------------------------------------------------------------------------------
# explain and remap

def explain(report: dict) -> list[str]:
    """One line per change: what changed, what it breaks in this codebase, and the fix."""
    out = []
    inv, sch, rng, rows = report["inventory"]["roles"], report["schema"], report["ranges"], report["rows"]
    cl, pk, cm, lab = report["closed"], report["case_pack"], report["card_map"], report["label_coverage"]
    t = inv["transactions"]
    out.append(f"Transactions arrive as a second file ({t['round2']}, {rows['transactions'][1]:,} rows) beside {t['round1']} "
               f"({rows['transactions'][0]:,} rows), not as a replacement. Breaks: a loader that reads only the Round 1 file name "
               "ignores January silently. Fix: union both files by column name with src_file and round columns.")
    i = inv["identity"]
    out.append(f"Identity arrives as {i['round2']} ({rows['identity'][1]:,} rows) beside {i['round1']} ({rows['identity'][0]:,}). "
               "Breaks: device profiles (ident2.device_key) are missing for every January transaction unless both files are unioned. "
               "Fix: union by name, cast to the Round 1 types.")
    c = inv["closed_cases"]
    out.append(f"The closed-case history keeps its name ({c['round2']}) but is a replacement: {cl['rows'][0]:,} -> {cl['rows'][1]:,} cases, "
               f"{cl['added']} added, {cl['removed']} removed, {cl['changed']} changed in place. Breaks: appending would double count "
               f"{cl['rows'][0] - cl['removed']:,} cases and every likelihood ratio fitted on them. Fix: replace the closed table with the Round 2 file.")
    p = inv["case_pack"]
    out.append(f"The case pack moves to {p['round2']} (was {p['round1']}): ids {pk['prefix'][0]} -> {pk['prefix'][1]}, "
               f"{pk['rows'][0]} -> {pk['rows'][1]} cases. Breaks: kavach/validate.py expects files "
               f"{pk['ids_round1'][0] if pk['ids_round1'] else '?'}..{pk['ids_round1'][-1] if pk['ids_round1'] else '?'} and the CLI reads one pack. "
               "Fix: keep both packs in the cases table with a round column; validate against the ids of the round being checked.")
    for role in CSV_ROLES:
        d = sch[role]["diff"]
        if d["added"] or d["removed"] or not d["order_same"]:
            out.append(f"Column drift in {role}: added {d['added'][:8]}, removed {d['removed'][:8]}, moved {d['moved'][:8]}. "
                       "Breaks: any positional union and every query naming a removed column. Fix: union by name; map renamed columns explicitly.")
        for tc in d["type_changes"]:
            out.append(f"Type drift in {role}.{tc['column']}: DuckDB reads it as {tc['round2']} in the Round 2 file ({tc['round1']} in Round 1); "
                       f"{tc['cast_loss']} value(s) would be lost by a cast to {tc['round1']}. Breaks: a plain union widens the column to text, "
                       "so numeric comparisons, casts and keys built from it (for addr1: region_activity, holder_key) change meaning. "
                       f"Fix: read the Round 2 file with the Round 1 types{' (lossless)' if tc['cast_loss'] == 0 else '; values that do not fit become null and are counted'}.")
        if not (d["added"] or d["removed"] or d["type_changes"]) and d["order_same"]:
            out.append(f"Schema of {role}: unchanged ({d['n_cols'][1]} columns, same order, same inferred types).")
    out.append(f"TransactionID ranges: Round 1 {rng['round1']['TransactionID'][0]}..{rng['round1']['TransactionID'][1]}, Round 2 "
               f"{rng['round2']['TransactionID'][0]}..{rng['round2']['TransactionID'][1]}, overlap {rng['id_overlap']}; TransactionDT "
               f"{'continues after' if rng['dt_continues'] else 'does NOT continue after'} Round 1. "
               + ("TransactionID stays a key across the union." if rng["id_overlap"] == 0 else
                  "Breaks: TransactionID is no longer unique; joins to identity and closed cases would fan out. Fix: key on (src_file, TransactionID)."))
    out.append(f"Grader scope: the organizers run grade.py with {t['round2']} alone (ids {rng['round2']['TransactionID'][0]}..{rng['round2']['TransactionID'][1]}). "
               "Breaks: any affected_txn_ids or first_suspicious_txn_id from July to December fails its ID check even though it exists in our index. "
               "Fix: grader_scope table; episodes keep January ids only and cite older activity in evidence text.")
    fitted = [x for x in lab if x["month"] < "2016-11"]
    late = [x for x in lab if x["month"] >= "2016-11"]
    share = lambda xs: (sum(x["labelled_round2"] for x in xs) / max(1, sum(x["txns"] for x in xs)))
    late_txt = ", ".join(f"{x['month']}: {x['labelled_round2']}" for x in late)
    out.append(f"Label coverage: the closed cases label {share(fitted):.1%} of July to October transactions but {share(late):.2%} of "
               f"November to January ({late_txt}). Breaks: score calibration and the "
               "likelihood ratios (kavach/weights.py) were fitted on labelled months only; refitting over the union would read unlabelled "
               "fraud as legitimate. Fix: keep calibration on ts < 2016-11-01 and apply it only before 2017-01-01.")
    out.append(f"Risk model not retrained (brief): January risk_score mean {rng['round2']['risk_mean']} and p90 {rng['round2']['risk_p90']} vs Round 1 "
               f"{rng['round1']['risk_mean']} and {rng['round1']['risk_p90']}. Breaks: the calibrated score signal would treat a low January score as "
               "evidence of innocence. Fix: clamp the January score log-likelihood ratio; a high score alone cannot carry a verdict.")
    stale = [a for a in report.get("artifacts", []) if a.get("stale")]
    out.append("Derived artifacts built from Round 1 rows only: " + (", ".join(f"{a['artifact']} ({a['rows']:,} rows vs {a['union_rows']:,})" for a in stale)
               if stale else "per-card features and the Round 1 index") + ". Breaks: prior_n, amt_z, device_cards_total and every per-card "
               "baseline are wrong for January cards. Fix: rebuild the index over the union and the features parquet from it (features_r2.parquet).")
    out.append(f"TigerGraph lane: the Round 1 graph holds {rows['transactions'][0]:,} Transaction vertices and the Round 1 closed cases. Breaks: queries "
               f"on the graph lane never see January. Fix: load incrementally ({rows['transactions'][1]:,} transactions, {rows['identity'][1]:,} identity rows) "
               f"and replace the closed-case vertices with the {cl['rows'][1]:,}-case history.")
    if cm["shifted"] or cm["new_pairs"] or cm["closed_cases_broken"]:
        out.append(f"card_id mapping: {cm['shifted']} card ids shift and {cm['new_pairs']} new (customer, card6) pairs appear over the union; "
                   f"{cm['closed_cases_broken']} closed case(s) no longer match. Breaks: card histories, closed-case precedent and every answer "
                   "naming a shifted card. Fix: freeze the Round 1 card_map and append new pairs with the next free K.")
    else:
        out.append(f"card_id mapping: stable. The rule over the union reproduces all {cm['pairs_round1']:,} Round 1 card ids (0 shifted, "
                   f"0 new pairs, {cm['new_customers']} new customers) and {cm['closed_txns_agree']:,}/{cm['closed_txns_checked']:,} closed-case "
                   "transactions. No card re-map needed.")
    for m in pk["card_mismatch"]:
        out.append(f"Alert data error (alert_card_mismatch): {m['case_id']} states card {m['stated_card']} but flagged transaction "
                   f"{m['flagged_txn_id']} maps to {m['resolved_card']} (card6 {m['card6']}). The mapping is verified on every closed case, so "
                   "the pack row is wrong. Breaks: the Round 1 context code looks for the transaction on the stated card and fails. "
                   "Fix: resolve the card from the flagged transaction and record the discrepancy in the answer evidence.")
    for m in pk["flagged_missing"]:
        out.append(f"Alert data error: {m['case_id']} flags transaction {m['flagged_txn_id']}, which exists in neither file. Fix: fail closed.")
    if pk.get("flagged_in"):
        where = ", ".join(f"{'Round 1' if k == 1 else 'Round 2'} file: {v}" for k, v in sorted(pk["flagged_in"].items()))
        out.append(f"Round 2 flagged transactions live in: {where}.")
    h = pk.get("hostile", {})
    if "rows_with_markers" in h:
        out.append(f"Hostile text: {h['rows_with_markers']} of {h['checked']} pack rows carry instructions aimed at the agent "
                   f"({', '.join(x['case_id'] for x in h['cases'])}). Breaks: anything that passes trigger_text to a model. "
                   "Fix: kavach.intake parses the text as data; the LLM never sees it.")
    idn = report["identity"]
    out.append(f"Identity coverage: {idn['round2']['share']:.1%} of January transactions have an identity record "
               f"({idn['round1']['share']:.1%} in Round 1); {idn['round2']['orphan_identity_rows']} January identity rows have no transaction. "
               "Breaks nothing structurally, but device evidence is absent for most January transactions (see data-quality.md).")
    if cl["added_linked_to_round1_pack"]:
        out.append(f"{len(cl['added_linked_to_round1_pack'])} of the {cl['added']} added closed cases are the Round 1 investigations "
                   f"({', '.join(x['pack_case_id'] + '=' + x['closed_case_id'] for x in cl['added_linked_to_round1_pack'][:6])}...). "
                   "They are precedent now: recall and holder_fraud_history will find them.")
    return out


def remap(report: dict) -> dict:
    inv, cm, rng, pk, sch = report["inventory"]["roles"], report["card_map"], report["ranges"], report["case_pack"], report["schema"]
    stable = not (cm["shifted"] or cm["new_pairs"] or cm["closed_cases_broken"])
    casts = {f"{role}.{tc['column']}": {"from": tc["round2"], "to": tc["round1"], "cast_loss": tc["cast_loss"]}
             for role in CSV_ROLES for tc in sch[role]["diff"]["type_changes"]}
    return {
        "tables": {
            "txn": {"sources": [inv["transactions"]["round1"], "round2/" + inv["transactions"]["round2"]], "how": "union by name, Round 2 cast to Round 1 types",
                    "added_columns": ["src_file", "round"]},
            "ident": {"sources": [inv["identity"]["round1"], "round2/" + inv["identity"]["round2"]], "how": "union by name, Round 2 cast to Round 1 types"},
            "closed": {"sources": ["round2/" + inv["closed_cases"]["round2"]], "how": "replace (the Round 2 history supersedes Round 1)"},
            "cases": {"sources": [inv["case_pack"]["round1"], "round2/" + inv["case_pack"]["round2"]], "how": "union by name with a round column (1, 2)"},
        },
        "union_order": [inv["transactions"]["round1"], "round2/" + inv["transactions"]["round2"]],
        "type_casts": casts,
        "card_map": {"strategy": "recompute_over_union" if stable else "freeze_round1_append",
                     "rule": "customer_id || '-K' || rank of coalesce(card6, '') among the customer's distinct card6 values",
                     "shifted": cm["shifted"], "new_pairs": cm["new_pairs"],
                     "gate": "100% agreement on closed-case transactions and on every case-pack flagged transaction's card"},
        "alert_overrides": [{"case_id": m["case_id"], "stated_card": m["stated_card"], "use_card": m["resolved_card"], "flag": "alert_card_mismatch"}
                            for m in pk["card_mismatch"]],
        "grader_scope": {"file": inv["transactions"]["round2"], "TransactionID": rng["round2"]["TransactionID"], "rows": rng["round2"]["rows"],
                         "rule": "affected_txn_ids and first_suspicious_txn_id only from this file"},
        "calibration": {"fit_on": "ts < 2016-11-01", "apply_before": "2017-01-01", "january_score": "untrusted, LLR clamped"},
        "rebuild": ["DuckDB index over the union (data/kavach_r2.duckdb)", "features parquet over the union (data/features_r2.parquet)",
                    "weights refit (labelled months only)", "TigerGraph incremental load of January plus the replaced closed cases"],
        "validator": {"expected_ids": pk["ids_round2"]},
    }


# ---------------------------------------------------------------------------------------------------------------------
# report files, approval gate

def _md_table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def _render_md(payload: dict) -> str:
    r, ex, rm = payload["report"], payload["explain"], payload["remap"]
    inv, rows, rng, cl, pk, cm = r["inventory"], r["rows"], r["ranges"], r["closed"], r["case_pack"], r["card_map"]
    ap = payload.get("approval")
    L = ["# Schema drift report (challenge 12)", "",
         f"Generated by `python -m kavach.drift detect` at {r['generated_at']} in {r['elapsed_s']} s. Round 1 folder `{inv['round1_dir']}`, "
         f"Round 2 folder `{inv['round2_dir']}`. Fingerprint (sha256 over Round 2 file names and sizes): `{payload['fingerprint']}`.", "",
         "**Approval:** " + (f"approved, record `{ap.get('id', '?')}` by role `{ap.get('role', '?')}` at {ap.get('ts', '?')} "
                             f"(case `DRIFT-{payload['fingerprint'][:12]}`, hash `{str(ap.get('hash', ''))[:16]}`)." if ap else
                             "**not approved.** `kavach index` refuses Round 2 ingestion until `python -m kavach.drift approve --token <token>` "
                             "records an approval through `kavach.harness.approvals` (role derived from the token, append-only hash chain)."), "",
         "## What breaks and the fix", ""] + [f"{i + 1}. {x}" for i, x in enumerate(ex)] + ["",
         "## File inventory", "",
         _md_table(["role", "Round 1", "Round 2", "renamed", "moved"],
                   [[k, v["round1"], v["round2"], v["renamed"], v["moved"]] for k, v in inv["roles"].items()]), "",
         _md_table(["Round 2 file", "bytes"], [[f["file"], f"{f['bytes']:,}"] for f in inv["round2"]]), "",
         "## Schema", "",
         _md_table(["file", "columns R1", "columns R2", "added", "removed", "same order", "type changes (R1 -> R2, cast loss)"],
                   [[k, v["diff"]["n_cols"][0], v["diff"]["n_cols"][1], len(v["diff"]["added"]), len(v["diff"]["removed"]), v["diff"]["order_same"],
                     "; ".join(f"{t['column']} {t['round1']} -> {t['round2']} ({t['cast_loss']})" for t in v["diff"]["type_changes"]) or "none"]
                    for k, v in r["schema"].items()]), "",
         "## Rows and ranges", "",
         _md_table(["file", "rows R1", "rows R2"], [[k, f"{v[0]:,}", f"{v[1]:,}"] for k, v in rows.items()]), "",
         _md_table(["", "Round 1", "Round 2"], [
             ["ts", " to ".join(rng["round1"]["ts"]), " to ".join(rng["round2"]["ts"])],
             ["TransactionID", "..".join(map(str, rng["round1"]["TransactionID"])), "..".join(map(str, rng["round2"]["TransactionID"]))],
             ["TransactionDT", "..".join(map(str, rng["round1"]["TransactionDT"])), "..".join(map(str, rng["round2"]["TransactionDT"]))],
             ["customers", rng["round1"]["customers"], f"{rng['round2']['customers']} ({rng['round2_customers_new']} new)"],
             ["risk_score mean / p90", f"{rng['round1']['risk_mean']} / {rng['round1']['risk_p90']}", f"{rng['round2']['risk_mean']} / {rng['round2']['risk_p90']}"],
             ["id overlap", rng["id_overlap"], ""]]), "",
         "## Closed-case history diff", "",
         f"Rows {cl['rows'][0]:,} -> {cl['rows'][1]:,}; added {cl['added']}, removed {cl['removed']}, changed {cl['changed']}. "
         f"Added ids: {', '.join(cl['added_ids'])}. Added outcomes: {cl['added_outcomes']}. Added patterns: {cl['added_patterns']}. "
         f"Opened {' to '.join(map(str, cl['added_opened']))}.", "",
         _md_table(["closed case", "Round 1 pack case", "card"], [[x["closed_case_id"], x["pack_case_id"], x["card_id"]] for x in cl["added_linked_to_round1_pack"]]), "",
         "Label coverage by month (transactions listed in a closed case; R2 = the new history):", "",
         _md_table(["month", "txns", "labelled R1", "fraud R1", "labelled R2", "fraud R2", "labelled share R2"],
                   [[x["month"], f"{x['txns']:,}", x["labelled_round1"], x["fraud_round1"], x["labelled_round2"], x["fraud_round2"],
                     f"{x['labelled_share_round2']:.2%}"] for x in r["label_coverage"]]), "",
         "## Case pack diff", "",
         f"Round 1 {pk['rows'][0]} cases ({pk['prefix'][0]}), Round 2 {pk['rows'][1]} cases ({pk['prefix'][1]}). Trigger mix R1 {pk['trigger_mix'][0]}, "
         f"R2 {pk['trigger_mix'][1]}. Round 2 opened {' to '.join(pk['opened'])}. Flagged transactions by file: {pk['flagged_in']}. "
         f"Hostile markers: {pk.get('hostile')}.", "",
         _md_table(["case", "flagged txn", "stated card", "resolved card", "card6", "flag"],
                   [[m["case_id"], m["flagged_txn_id"], m["stated_card"], m["resolved_card"], m["card6"], m["flag"]] for m in pk["card_mismatch"]])
         if pk["card_mismatch"] else "No case-pack card mismatches.", "",
         "## Identity coverage", "",
         _md_table(["round", "txns", "with identity", "share", "orphan identity rows"],
                   [[k, f"{v['txns']:,}", f"{v['with_identity']:,}", f"{v['share']:.1%}", v["orphan_identity_rows"]]
                    for k, v in r["identity"].items() if isinstance(v, dict)]), "",
         "## card_id mapping stability", "",
         f"Round 1 map from {cm['source_round1']}: {cm['pairs_round1']:,} (customer, card6) pairs; union map {cm['pairs_union']:,}. "
         f"Shifted {cm['shifted']}, new pairs {cm['new_pairs']}, new customers {cm['new_customers']}. Closed-case transactions: "
         f"{cm['closed_txns_agree']:,}/{cm['closed_txns_checked']:,} agree, {cm['closed_cases_broken']} cases broken, {cm['closed_txns_missing']} txns missing.", "",
         (_md_table(["customer", "card6", "Round 1", "union"], [[s["customer_id"], s["card6"], s["round1"], s["union"]] for s in cm["shifted_cards"]])
          if cm["shifted_cards"] else ""), "",
         "## Derived artifacts", "",
         _md_table(["artifact", "rows", "union rows", "stale"], [[a["artifact"], a.get("rows"), a.get("union_rows"), a.get("stale")] for a in r["artifacts"]])
         if r["artifacts"] else "Not checked.", "",
         "## Proposed re-mapping", "", "```json", json.dumps(rm, indent=2), "```", ""]
    return "\n".join(L)


def write_report(report: dict, json_path=None, md_path=None) -> dict:
    jp, mp = Path(json_path or DRIFT_JSON), Path(md_path or REPORT_MD)
    payload = {"fingerprint": report["fingerprint"], "generated_at": report["generated_at"], "report": report,
               "explain": explain(report), "remap": remap(report), "approval": None}
    if jp.exists():  # keep an approval only while it covers the same delivery
        try:
            old = json.loads(jp.read_text(encoding="utf-8"))
            if old.get("approval") and old.get("approval_fingerprint") == report["fingerprint"]:
                payload["approval"], payload["approval_fingerprint"] = old["approval"], old["approval_fingerprint"]
            elif old.get("approval"):
                payload["stale_approval"] = {"fingerprint": old.get("approval_fingerprint"), "id": old["approval"].get("id")}
        except (ValueError, OSError):
            pass
    _save(payload, jp, mp)
    return payload


def _save(payload: dict, jp: Path, mp: Path) -> None:
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(_render_md(payload), encoding="utf-8")


def _load(json_path=None) -> dict | None:
    jp = Path(json_path or DRIFT_JSON)
    if not jp.exists():
        return None
    try:
        return json.loads(jp.read_text(encoding="utf-8"))
    except ValueError:
        return None


def approve(token: str, note: str = "", json_path=None, md_path=None, round2_dir=None) -> dict:
    """Record a human approval of the current drift report through the approvals chain (role from the token)."""
    jp, mp = Path(json_path or DRIFT_JSON), Path(md_path or REPORT_MD)
    payload = _load(jp)
    if payload is None:
        raise RuntimeError(f"no drift report at {jp}; run `python -m kavach.drift detect` first")
    fp = fingerprint(round2_dir)
    if payload["fingerprint"] != fp:
        raise RuntimeError("the Round 2 files changed since the drift report was written; run detect again before approving")
    approvals = importlib.import_module("kavach.harness.approvals")
    rec = approvals.approve(case_id=f"DRIFT-{fp[:12]}", action="CREATE_CASE", token=token, exposure=0.0,
                            note=note or "schema drift reviewed: approve the Round 2 re-mapping")
    rec = rec if isinstance(rec, dict) else getattr(rec, "__dict__", {"record": str(rec)})
    payload["approval"], payload["approval_fingerprint"] = rec, fp
    payload.pop("stale_approval", None)
    _save(payload, jp, mp)
    return rec


def status(json_path=None, round2_dir=None) -> dict:
    payload = _load(json_path)
    fp = fingerprint(round2_dir)
    st = {"fingerprint": fp, "approved": False, "reason": ""}
    if payload is None:
        st["reason"] = "no drift report; run `python -m kavach.drift detect`"
    elif payload.get("fingerprint") != fp:
        st["reason"] = "the Round 2 files changed since the drift report; run detect again"
    elif not payload.get("approval"):
        st["reason"] = "drift report not approved; run `python -m kavach.drift approve --token <token>`"
    elif payload.get("approval_fingerprint") != fp:
        st["reason"] = "the approval covers a different delivery; approve again"
    else:
        rec = payload["approval"]
        if rec.get("denied") or str(rec.get("status", "")).lower() == "denied":
            st["reason"] = "the recorded approval was denied"
        elif rec.get("case_id") not in (None, f"DRIFT-{fp[:12]}"):
            st["reason"] = f"approval record is for {rec.get('case_id')}, not DRIFT-{fp[:12]}"
        else:
            bad = _chain_problem(rec, f"DRIFT-{fp[:12]}")
            if bad:
                st["reason"] = bad
            else:
                st.update(approved=True, reason=f"approved by role {rec.get('role', '?')} at {rec.get('ts', '?')}", approval_id=rec.get("id"))
    return st


def _chain_problem(rec: dict, case_id: str) -> str:
    """The stored record must still be in the append-only approvals log and the chain must verify (fail closed)."""
    try:
        approvals = importlib.import_module("kavach.harness.approvals")
    except Exception as e:
        return f"cannot verify the approval: kavach.harness.approvals not importable ({type(e).__name__})"
    if hasattr(approvals, "verify"):
        ok = approvals.verify()
        if not (ok[0] if isinstance(ok, tuple) else ok):
            return "the approvals hash chain does not verify; the approval cannot be trusted"
    if hasattr(approvals, "records"):
        logged = [r for r in approvals.records(case_id) if r.get("hash") == rec.get("hash") and r.get("status", "approved") == "approved"]
        if not logged:
            return "the stored approval is not in the approvals log"
    return ""


def is_approved(json_path=None, round2_dir=None) -> bool:
    return status(json_path, round2_dir)["approved"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.drift", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("--round1", default=None)
    d.add_argument("--round2", default=None)
    d.add_argument("--sample-size", type=int, default=-1)
    a = sub.add_parser("approve")
    a.add_argument("--token", required=True)
    a.add_argument("--note", default="")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    if args.cmd == "detect":
        rep = detect(args.round1, args.round2, sample_size=args.sample_size)
        payload = write_report(rep)
        print(f"drift detect: {rep['elapsed_s']} s, fingerprint {rep['fingerprint'][:12]}, report {REPORT_MD} and {DRIFT_JSON}")
        for i, line in enumerate(payload["explain"], 1):
            print(f"{i:2d}. {line}")
        st = status()
        print(f"approval: {'yes' if st['approved'] else 'NO'} ({st['reason']})")
        return 0
    if args.cmd == "approve":
        try:
            rec = approve(args.token, args.note)
        except Exception as e:
            print(f"drift approval refused: {type(e).__name__}: {e}")
            return 2
        print(f"drift approved: record {rec.get('id')} role {rec.get('role')} case DRIFT-{fingerprint()[:12]}")
        return 0
    st = status()
    print(f"drift status: fingerprint {st['fingerprint'][:12]}, approved={st['approved']} ({st['reason']})")
    return 0 if st["approved"] else 1


if __name__ == "__main__":
    sys.exit(main())
