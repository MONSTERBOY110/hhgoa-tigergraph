"""Someone else's schema (brief challenge 13): work out how a renamed and reshaped variant of the bank's files maps onto
the canonical schema kavach/data/duck.py reads, get a human approval, write canonical CSVs, and investigate on them.

    propose(variant_files) -> mapping       value profiling + name similarity + cross-file join keys; every choice carries
                                            the sample records that justify it and a confidence; unresolved columns are
                                            listed, never guessed silently
    approve(mapping_path, token)            the approval step (kavach.harness.approvals, role from the token, analyst or
                                            above, the same gate as the drift re-mapping); bound to the mapping's sha256
                                            and the variant files' fingerprint
    apply(mapping, out_dir) -> files        canonical CSVs (refuses an unapproved or changed mapping)
    build_index(canon_dir, out_path)        a DuckDB index over the canonical CSVs (never data/kavach_r2.duckdb)
    make_variant(seed, out_dir)             our own test variant from the real files (rename, reorder, reshape)
    score(mapping, truth) -> accuracy       per column, against the variant generator's truth file

CLI: python -m kavach.mapper variant|propose|approve|apply|index|score|compare ...  (docs/round2/schema-mapping.md)

How a column is recognised. Each canonical column has a reference profile computed from the bank's own canonical files
(type class, null rate, distinct ratio, quantiles, value shapes such as 9-9-9 9:9:9 or A9-A9, top values). Each variant
column, and each reshaped candidate built from variant columns, gets the same profile. Candidates:
    plain column; a fixed-count delimited field split into its parts (a combined "card" field); a date column plus a
    time column joined into one timestamp; an integer epoch in seconds or milliseconds turned into a timestamp; other
    timestamp formats (ISO with T, day-first or month-first with slashes) normalised; an integer amount scaled by 1/100
    (cents); a list with another delimiter normalised to "|"; enumerations recoded (case, T/F = Y/N = 1/0 = true/false,
    Yes/No); a long (melted) table of (id, attribute, value) pivoted to wide first.
Scores: profile similarity, name similarity (synonyms and family plus number, such as cnt_07 for C7), and, when the
variant's key column joins to the bank's own records (TransactionID, case_id), the share of joined rows on which the
candidate's value equals the canonical value exactly. Assignment is one-to-one, best score first. TransactionDT is
derived from the timestamp when no column carries it (the bank's TransactionDT is seconds since 2016-07-02 00:00:00,
one constant offset on all 650,938 rows); customer_id from a card id when missing (card ids are customer + "-K" + n).
"""
import argparse
import difflib
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from kavach.config import DATA, RAW, RAW2

VARIANT_DIR = DATA / "variant"
EPOCH0 = 1467417600          # 2016-07-02 00:00:00 UTC; canonical TransactionDT = epoch seconds - EPOCH0
TS_FMT = "%Y-%m-%d %H:%M:%S"
SAMPLE = 4000

# ------------------------------------------------------------------------------------------------ canonical schema
TXN_COLS = (["TransactionID", "TransactionDT", "TransactionAmt", "ProductCD"] + [f"card{i}" for i in range(1, 7)] +
            ["addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain"] + [f"C{i}" for i in range(1, 15)] +
            [f"D{i}" for i in range(1, 16)] + [f"M{i}" for i in range(1, 10)] + [f"V{i}" for i in range(1, 340)] +
            ["customer_id", "ts", "channel", "risk_score"])
IDENT_COLS = ["TransactionID"] + [f"id_{i:02d}" for i in range(1, 39)] + ["DeviceType", "DeviceInfo"]
CLOSED_COLS = ["case_id", "customer_id", "card_id", "opened_at", "closed_at", "outcome", "pattern", "first_fraud_txn_id",
               "txn_ids", "n_txns", "exposure_usd", "connected_card_ids", "actions_taken", "report_filed", "analyst_notes"]
PACK_COLS = ["case_id", "opened_at", "trigger_type", "trigger_text", "flagged_txn_id", "card_id", "customer_id", "risk_score"]
CANON = {"transactions": TXN_COLS, "identity": IDENT_COLS, "closed_cases": CLOSED_COLS, "case_pack": PACK_COLS}
KEY = {"transactions": "TransactionID", "identity": "TransactionID", "closed_cases": "case_id", "case_pack": "case_id"}
# the columns the agent reads (kavach/data/duck.py, kavach/duckq.py); a role is usable only when these resolve
NEEDED = {
    "transactions": ["TransactionID", "TransactionDT", "ts", "TransactionAmt", "ProductCD"] + [f"card{i}" for i in range(1, 7)] +
                    ["addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain", "C1", "C2", "C13", "C14", "D1", "D2",
                     "D3", "D4", "D10", "D15"] + [f"M{i}" for i in range(1, 10)] + ["customer_id", "channel", "risk_score"],
    "identity": ["TransactionID", "DeviceInfo", "DeviceType", "id_15", "id_23", "id_30", "id_31", "id_33"],
    "closed_cases": CLOSED_COLS,
    "case_pack": PACK_COLS,
}
REF_FILES = {
    "transactions": [RAW / "transactions.csv", RAW2 / "transactions_2017q1.csv"],
    "identity": [RAW / "identity.csv", RAW2 / "identity_2017q1.csv"],
    "closed_cases": [RAW2 / "closed_cases_history.csv"],
    "case_pack": [RAW / "case_pack.csv", RAW2 / "cases" / "case_pack_final.csv"],
}
SYN = {
    "TransactionID": ["transaction id", "txn id", "tx id", "txn ref", "transaction key", "trans id", "txn key"],
    "TransactionDT": ["transaction dt", "dt", "elapsed seconds", "seconds offset"],
    "TransactionAmt": ["transaction amount", "amount", "amt", "amount minor", "value", "cents"],
    "ProductCD": ["product code", "product", "product cd", "prod"],
    "addr1": ["billing region", "region", "address 1", "addr 1", "zip"], "addr2": ["billing country", "country", "address 2", "addr 2"],
    "dist1": ["distance 1", "dist 1"], "dist2": ["distance 2", "dist 2"],
    "P_emaildomain": ["purchaser email domain", "payer email", "buyer email", "p email"],
    "R_emaildomain": ["recipient email domain", "receiver email", "payee email", "r email"],
    "customer_id": ["customer id", "customer", "cust ref", "holder", "client id", "cardholder"],
    "ts": ["timestamp", "event time", "datetime", "txn time", "date time"], "channel": ["channel", "mode", "presence"],
    "risk_score": ["risk score", "model score", "score", "risk"], "DeviceType": ["device type", "device class"],
    "DeviceInfo": ["device info", "device", "device name"], "case_id": ["case id", "case ref", "case", "alert id"],
    "card_id": ["card id", "card ref", "card"], "opened_at": ["opened at", "opened", "raised at", "open time"],
    "closed_at": ["closed at", "closed", "close time"], "outcome": ["outcome", "result", "resolution", "disposition"],
    "pattern": ["pattern", "typology", "fraud type"], "first_fraud_txn_id": ["first fraud txn id", "first txn", "first fraud"],
    "txn_ids": ["txn ids", "transactions", "txn list", "transaction ids"], "n_txns": ["n txns", "txn count", "count"],
    "exposure_usd": ["exposure usd", "exposure", "loss", "loss amount"],
    "connected_card_ids": ["connected card ids", "connected cards", "linked cards"], "actions_taken": ["actions taken", "actions"],
    "report_filed": ["report filed", "sar filed", "report", "sar"], "analyst_notes": ["analyst notes", "notes", "comment"],
    "trigger_type": ["trigger type", "alert type", "trigger", "source"], "trigger_text": ["trigger text", "alert text", "message", "text"],
    "flagged_txn_id": ["flagged txn id", "flagged transaction", "txn ref", "flagged"],
}
FAMILY = {"C": ["c", "cnt", "count"], "D": ["d", "day", "days", "delta", "td"], "M": ["m", "match", "mf"],
          "V": ["v", "vf", "vesta", "feat", "f"], "id": ["id", "ident", "identity", "attr"]}
BOOL_SETS = [("T", "F"), ("Y", "N"), ("1", "0"), ("true", "false"), ("True", "False"), ("TRUE", "FALSE"), ("Yes", "No"),
             ("yes", "no"), ("YES", "NO")]
TS_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S",
              "%m/%d/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y%m%d%H%M%S"]
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y%m%d"]
TIME_FORMATS = ["%H:%M:%S", "%H%M%S"]
SPLIT_DELIMS = ["|", ";", "#", "~", "^", "/"]
LIST_DELIMS = [";", ",", " ", "/", "#"]


class MappingNotApproved(RuntimeError):
    pass


def _p(path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


# ------------------------------------------------------------------------------------------------ value helpers
def shape(v: str) -> str:
    """Character-class shape with runs collapsed: 'C01234-K2' -> 'A9-A9', '2017-01-02 03:04:05' -> '9-9-9 9:9:9'."""
    out, last = [], None
    for ch in v[:40]:
        c = "9" if ch.isdigit() else "A" if ch.isupper() else "a" if ch.islower() else ch
        if c != last:
            out.append(c)
        last = c
    return "".join(out)


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def norm_value(v) -> str | None:
    """Comparable form of a raw value: numbers to a fixed float form, everything else the stripped string."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return None
    try:
        f = float(s)
        if math.isfinite(f) and not re.fullmatch(r"[A-Za-z]+", s):
            return repr(round(f, 6))
    except ValueError:
        pass
    return s


def _norm_series(s: pd.Series) -> pd.Series:
    return s.map(norm_value)


def profile(s: pd.Series) -> dict:
    """Profile of one column of raw strings."""
    s = s.astype("object").where(s.notna(), None)
    vals = s.dropna().astype(str).str.strip()
    vals = vals[vals != ""]
    n = len(s)
    p = {"n": n, "null": round(1 - len(vals) / n, 4) if n else 1.0, "distinct": int(vals.nunique())}
    p["distinct_ratio"] = round(p["distinct"] / max(len(vals), 1), 4)
    if not len(vals):
        p["kind"] = "empty"
        return p
    num = _num(vals)
    num_share = float(num.notna().mean())
    shapes = vals.head(600).map(shape).value_counts(normalize=True).head(8)
    p["shapes"] = {k: round(float(v), 4) for k, v in shapes.items()}
    p["top"] = {str(k): round(float(v), 4) for k, v in vals.value_counts(normalize=True).head(30).items()}
    p["len"] = [int(vals.str.len().min()), float(round(vals.str.len().mean(), 2)), int(vals.str.len().max())]
    if num_share >= 0.98 and p["distinct"] > 2:
        x = num.dropna().astype(float)
        p["kind"] = "num"
        p["int_share"] = round(float((np.abs(x - np.round(x)) < 1e-9).mean()), 4)
        p["q"] = [float(x.quantile(q)) for q in (0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99)]
        p["min"], p["max"] = float(x.min()), float(x.max())
        p["neg_share"] = round(float((x < 0).mean()), 4)
    elif p["distinct"] <= 40 and p["distinct_ratio"] < 0.2:
        p["kind"] = "enum"
    else:
        p["kind"] = "str"
    return p


def _qsim(a: list, b: list) -> float:
    """Similarity of two quantile vectors on a signed log scale."""
    f = lambda x: math.copysign(math.log1p(abs(x)), x)
    d = [abs(f(x) - f(y)) for x, y in zip(a, b)]
    return max(0.0, 1.0 - (sum(d) / len(d)) / 3.0)


def profile_sim(ps: dict, pt: dict) -> float:
    """How alike a candidate's profile is to a canonical column's reference profile (0 to 1)."""
    ks, kt = ps.get("kind"), pt.get("kind")
    if ks == "empty" or kt == "empty":
        return 0.3 if ks == kt else 0.05
    null_sim = 1 - abs(ps["null"] - pt["null"])
    if kt == "num" and ks == "num":
        s = (0.45 * _qsim(ps["q"], pt["q"]) + 0.15 * (1 - abs(ps["int_share"] - pt["int_share"])) +
             0.1 * (1 - abs(ps["neg_share"] - pt["neg_share"])) + 0.15 * null_sim +
             0.15 * max(0.0, 1 - abs(math.log10(ps["distinct"] + 1) - math.log10(pt["distinct"] + 1)) / 2))
        return round(s, 4)
    if (kt == "num") != (ks == "num"):
        # a 2-valued numeric (0/1) can still be an enum; anything else is a different type
        return 0.05
    top = sum(min(v, ps.get("top", {}).get(k, 0.0)) for k, v in pt.get("top", {}).items())
    shp = sum(min(v, ps.get("shapes", {}).get(k, 0.0)) for k, v in pt.get("shapes", {}).items())
    ln = 1 - min(1.0, abs(ps["len"][1] - pt["len"][1]) / max(pt["len"][1], 1))
    card = max(0.0, 1 - abs(math.log10(ps["distinct"] + 1) - math.log10(pt["distinct"] + 1)) / 2)
    return round(0.35 * top + 0.3 * shp + 0.1 * ln + 0.1 * null_sim + 0.15 * card, 4)


def _tokens(name: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    return " ".join(t for t in re.split(r"[^A-Za-z0-9]+", s.lower()) if t)


def _family(target: str):
    m = re.fullmatch(r"(C|D|M|V)(\d+)", target) or re.fullmatch(r"(id)_(\d+)", target)
    return (m.group(1), int(m.group(2))) if m else None


@lru_cache(maxsize=None)
def name_sim(src: str, target: str) -> float:
    a = _tokens(src)
    fam = _family(target)
    if fam:
        nums = re.findall(r"\d+", a)
        words = re.findall(r"[a-z]+", a)
        if nums and int(nums[-1]) == fam[1] and any(w in FAMILY[fam[0]] for w in words):
            return 0.95
        if nums and int(nums[-1]) == fam[1]:
            return 0.5
        return 0.0
    cands = [_tokens(target)] + SYN.get(target, [])
    best = max(difflib.SequenceMatcher(None, a, c).ratio() for c in cands)
    if any(c == a for c in cands):
        best = 1.0
    return round(best, 4)


# ------------------------------------------------------------------------------------------------ reading
def _read_sample(path: Path, n: int = SAMPLE, ids: list | None = None, key: str | None = None, seed: int = 7) -> pd.DataFrame:
    con = duckdb.connect()
    src = f"read_csv('{_p(path)}', header=true, all_varchar=true, sample_size=-1)"
    if ids is not None and key:
        con.execute("create temp table k (v varchar)")
        con.executemany("insert into k values (?)", [[str(i)] for i in ids])
        df = con.execute(f'select * from {src} where "{key}" in (select v from k)').df()
    else:
        df = con.execute(f"select * from {src} using sample reservoir({int(n)} rows) repeatable ({int(seed)})").df()
    con.close()
    return df


def _columns(path: Path) -> list[str]:
    con = duckdb.connect()
    cols = [r[0] for r in con.execute(f"describe select * from read_csv('{_p(path)}', header=true, all_varchar=true)").fetchall()]
    con.close()
    return cols


def fingerprint(files) -> str:
    h = hashlib.sha256()
    for f in sorted(Path(x) for x in files):
        h.update(f"{f.name}:{f.stat().st_size}\n".encode())
    return h.hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ reference
_REF: dict = {}


def reference(role: str, n: int = SAMPLE) -> dict:
    """Reference profiles of the canonical columns of a role, from the bank's own canonical files."""
    if role in _REF:
        return _REF[role]
    files = [f for f in REF_FILES[role] if f.exists()]
    if not files:
        raise FileNotFoundError(f"no canonical {role} file under {RAW} to profile; run `python -m kavach download` first")
    parts = [_read_sample(f, max(n // len(files), 200)) for f in files]
    df = pd.concat(parts, ignore_index=True)
    prof = {c: profile(df[c]) for c in CANON[role] if c in df.columns}
    _REF[role] = prof
    return prof


def reference_rows(role: str, ids: list) -> pd.DataFrame:
    """The bank's own canonical rows for the given key values (the cross-file join used to confirm a mapping)."""
    frames = [_read_sample(f, ids=ids, key=KEY[role]) for f in REF_FILES[role] if f.exists()]
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=CANON[role])
    return pd.concat(frames, ignore_index=True).drop_duplicates(KEY[role])


# ------------------------------------------------------------------------------------------------ candidates
def _parse_ts(vals: pd.Series, fmts: list[str]) -> tuple[str | None, pd.Series | None]:
    v = vals.dropna().astype(str).str.strip()
    v = v[v != ""].head(500)
    if not len(v):
        return None, None
    best = None
    for fmt in fmts:
        ok = pd.to_datetime(v, format=fmt, errors="coerce").notna().mean()
        if ok >= 0.98 and (best is None or ok > best[1]):
            best = (fmt, ok)
    if best is None:
        return None, None
    out = pd.to_datetime(vals.astype("object").where(vals.notna(), None), format=best[0], errors="coerce")
    return best[0], out.dt.strftime(TS_FMT).where(out.notna(), None)


def _split_info(vals: pd.Series):
    v = vals.dropna().astype(str)
    v = v[v != ""]
    if len(v) < 20:
        return None
    for d in SPLIT_DELIMS:
        cnt = v.str.count(re.escape(d))
        if (cnt == 0).mean() > 0.01:
            continue
        k = int(cnt.mode().iloc[0]) + 1
        if k >= 2 and (cnt == k - 1).mean() >= 0.995:
            return d, k
    return None


def _list_info(vals: pd.Series):
    """A list column with a delimiter other than '|' (variable item count, items look like ids or action names)."""
    v = vals.dropna().astype(str)
    v = v[v != ""]
    if len(v) < 3:
        return None
    for d in LIST_DELIMS:
        cnt = v.str.count(re.escape(d))
        if cnt.max() == 0:
            continue
        items = v.str.split(re.escape(d), regex=True).explode().str.strip()
        sh = items.map(shape).value_counts(normalize=True)
        if sh.iloc[0] >= 0.9 and " " not in sh.index[0]:
            return d
    return None


def candidates(df: pd.DataFrame) -> list[dict]:
    """Every plain or reshaped candidate column a variant table offers, with its transformed values."""
    out = []
    cols = list(df.columns)
    for c in cols:
        s = df[c].astype("object").where(df[c].notna(), None)
        out.append({"id": c, "sources": [c], "transform": {"kind": "plain"}, "values": s, "atoms": {c}})
        v = s.dropna().astype(str).str.strip()
        v = v[v != ""]
        if not len(v):
            continue
        num = _num(v)
        if num.notna().mean() >= 0.99:
            x = num.dropna()
            if x.between(1.0e9, 2.2e9).mean() >= 0.99:
                ts = pd.to_datetime(_num(s), unit="s", errors="coerce")
                out.append({"id": f"{c}@epoch_s", "sources": [c], "transform": {"kind": "epoch", "unit": "s"},
                            "values": ts.dt.strftime(TS_FMT).where(ts.notna(), None), "atoms": {c}})
            elif x.between(1.0e12, 2.2e12).mean() >= 0.99:
                ts = pd.to_datetime(_num(s), unit="ms", errors="coerce")
                out.append({"id": f"{c}@epoch_ms", "sources": [c], "transform": {"kind": "epoch", "unit": "ms"},
                            "values": ts.dt.strftime(TS_FMT).where(ts.notna(), None), "atoms": {c}})
            elif (np.abs(x - np.round(x)) < 1e-9).mean() >= 0.999 and x.min() >= 0:
                out.append({"id": f"{c}@div100", "sources": [c], "transform": {"kind": "scale", "factor": 0.01},
                            "values": (_num(s) / 100).round(2).map(lambda f: None if pd.isna(f) else repr(float(f))),
                            "atoms": {c}})
            continue
        sh = v.head(200).map(shape).value_counts().index[0]
        if re.search(r"9[-/]9[-/]9", sh) and ":" in sh or re.fullmatch(r"9A9:9:9A?", sh):
            fmt, ts = _parse_ts(s, TS_FORMATS)
            if fmt and fmt != TS_FMT:
                out.append({"id": f"{c}@ts", "sources": [c], "transform": {"kind": "ts_format", "format": fmt}, "values": ts,
                            "atoms": {c}})
        sp = None if any(x["id"] == f"{c}@ts" for x in out) else _split_info(s)
        if sp:
            d, k = sp
            parts = s.astype("object").map(lambda x: None if x is None else str(x).split(d))
            for i in range(k):
                pv = parts.map(lambda p, i=i: None if p is None or len(p) <= i or p[i] == "" else p[i])
                out.append({"id": f"{c}[{i}]", "sources": [c], "transform": {"kind": "split", "delim": d, "index": i, "parts": k},
                            "values": pv, "atoms": {f"{c}[{i}]"}, "base": c})
        ld = _list_info(s)
        if ld:
            out.append({"id": f"{c}@list", "sources": [c], "transform": {"kind": "list", "delim": ld},
                        "values": s.map(lambda x: None if x is None else "|".join(t.strip() for t in str(x).split(ld))),
                        "atoms": {c}})
    # a date column plus a time column
    dates, times = [], []
    for c in cols:
        v = df[c].dropna().astype(str).str.strip()
        v = v[v != ""].head(300)
        if not len(v):
            continue
        sh = v.map(shape).value_counts().index[0]
        if sh in ("9-9-9", "9/9/9", "9"):
            fmt, _ = _parse_ts(df[c], DATE_FORMATS)
            if fmt:
                dates.append((c, fmt))
        if sh in ("9:9:9", "9:9"):
            times.append((c, "%H:%M:%S" if sh == "9:9:9" else "%H:%M"))
    for dc, dfmt in dates:
        for tc, tfmt in times:
            joined = df[dc].astype(str).str.strip() + " " + df[tc].astype(str).str.strip()
            ts = pd.to_datetime(joined.where(df[dc].notna() & df[tc].notna(), None), format=f"{dfmt} {tfmt}", errors="coerce")
            out.append({"id": f"{dc}+{tc}", "sources": [dc, tc], "transform": {"kind": "date_time", "date_format": dfmt,
                        "time_format": tfmt}, "values": ts.dt.strftime(TS_FMT).where(ts.notna(), None), "atoms": {dc, tc}})
    return out


def _recode_for(cand_vals: pd.Series, ref_prof: dict, aligned: pd.Series | None = None) -> dict | None:
    """A value map from the candidate's enumeration to the canonical one (case, boolean synonyms, or learned from
    joined rows), or None when no recode is needed or none fits."""
    v = cand_vals.dropna().astype(str).str.strip()
    v = v[v != ""]
    have = set(v.unique())
    want = set(ref_prof.get("top", {}))
    if not have or len(have) > 40 or not want or have <= want:
        return None
    if aligned is not None:
        pairs = pd.DataFrame({"a": cand_vals.astype("object"), "b": aligned.astype("object")}).dropna()
        if len(pairs) >= 10:
            m = pairs.groupby("a")["b"].agg(lambda x: x.value_counts().index[0]).to_dict()
            agree = (pairs.a.map(m) == pairs.b).mean()
            if agree >= 0.98 and len(set(m.values())) == len(m):
                return {str(k): str(val) for k, val in m.items() if str(k) != str(val)} or None
    low = {w.lower(): w for w in want}
    if all(h.lower() in low for h in have):
        return {h: low[h.lower()] for h in have if h != low[h.lower()]}
    for tset in BOOL_SETS:
        if set(tset) >= want or (want and want <= set(tset)):
            for sset in BOOL_SETS:
                if have <= set(sset) and sset != tset:
                    return {sset[0]: tset[0], sset[1]: tset[1]}
    return None


# ------------------------------------------------------------------------------------------------ long format
def detect_long(df: pd.DataFrame, stats: dict | None = None) -> dict | None:
    """A melted table: three columns, an id column that repeats (each id has several rows), a low-cardinality
    attribute-name column, one value column, and (id, attribute) unique. `stats` holds whole-file distinct counts
    ({col: distinct, "_rows": n}); without it the sample decides."""
    if df.shape[1] != 3 or len(df) < 50:
        return None
    n = (stats or {}).get("_rows", len(df))
    dist = lambda c: (stats or {}).get(c, df[c].nunique())
    best = None
    for idc in df.columns:
        if dist(idc) / max(n, 1) > 0.5:
            continue
        for kc in df.columns:
            if kc == idc:
                continue
            keys = df[kc].dropna().astype(str)
            if not (3 <= dist(kc) <= 400) or not keys.map(lambda x: bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_ .-]*", x))).all():
                continue
            rest = [c for c in df.columns if c not in (idc, kc)]
            if df.duplicated([idc, kc]).mean() > 0.001:
                continue
            if best is None or dist(kc) > best[0]:
                best = (dist(kc), {"id": idc, "key": kc, "value": rest[0]})
    return best[1] if best else None


def pivot_long(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    wide = df.pivot(index=spec["id"], columns=spec["key"], values=spec["value"]).reset_index()
    wide.columns.name = None
    return wide


def _read_variant(path: Path, n: int, seed: int = 7) -> tuple[pd.DataFrame, dict | None]:
    head = _read_sample(path, 400, seed=seed)
    stats = None
    if head.shape[1] == 3:
        con = duckdb.connect()
        src = f"read_csv('{_p(path)}', header=true, all_varchar=true, sample_size=-1)"
        row = con.execute("select count(*), " + ", ".join(f'count(distinct "{c}")' for c in head.columns) + f" from {src}").fetchone()
        con.close()
        stats = {"_rows": row[0], **{c: row[i + 1] for i, c in enumerate(head.columns)}}
    spec = detect_long(head, stats)
    if spec is None:
        return _read_sample(path, n, seed=seed), None
    con = duckdb.connect()
    src = f"read_csv('{_p(path)}', header=true, all_varchar=true, sample_size=-1)"
    df = con.execute(f"""select * from {src} where "{spec['id']}" in
                         (select "{spec['id']}" from (select distinct "{spec['id']}" from {src}) using sample reservoir({int(n)} rows) repeatable ({int(seed)}))""").df()
    con.close()
    return pivot_long(df, spec), spec


# ------------------------------------------------------------------------------------------------ propose
def _role_scores(df: pd.DataFrame, cands: list[dict]) -> dict:
    """How well a variant table fits each canonical role: mean best profile similarity over the role's needed
    columns, with a small bonus for a column count close to the canonical one."""
    out = {}
    for c in cands:
        c.setdefault("profile", profile(c["values"]))
    cprof = [(c, c["profile"]) for c in cands]
    for role in CANON:
        try:
            ref = reference(role)
        except FileNotFoundError:
            continue
        needed = [t for t in NEEDED[role] if t in ref]
        best = []
        for t in needed:
            best.append(max((profile_sim(p, ref[t]) for _, p in cprof), default=0.0))
        size = 1 - min(1.0, abs(math.log((df.shape[1] + 1) / (len(CANON[role]) + 1))) / 3)
        out[role] = round(0.85 * float(np.mean(best)) + 0.15 * size, 4)
    return out


def _key_candidate(cands: list[dict], ref_prof: dict) -> dict | None:
    best = None
    for c in cands:
        if c["transform"]["kind"] != "plain":
            continue
        p = profile(c["values"])
        if p.get("distinct_ratio", 0) < 0.95 or p.get("null", 1) > 0.01:
            continue
        s = profile_sim(p, ref_prof)
        if best is None or s > best[0]:
            best = (s, c)
    return best[1] if best else None


def _samples(df_key: pd.Series, cand: dict, vals: pd.Series, aligned: pd.Series | None, k: int = 3) -> list[dict]:
    idx = [i for i in vals.index if vals[i] is not None and not (isinstance(vals[i], float) and math.isnan(vals[i]))][:k]
    out = []
    for i in idx:
        rec = {"key": str(df_key[i]) if df_key is not None else None,
               "source": {s: (None if cand["raw"][s][i] is None else str(cand["raw"][s][i])) for s in cand["sources"]},
               "canonical_value": str(vals[i])}
        if aligned is not None:
            rec["bank_record_value"] = None if aligned[i] is None or (isinstance(aligned[i], float) and math.isnan(aligned[i])) else str(aligned[i])
        out.append(rec)
    return out


def _match_table(path: Path, role: str, df: pd.DataFrame, cands: list[dict], long_spec: dict | None, verbose=False) -> dict:
    ref = reference(role)
    targets = [t for t in CANON[role] if t in ref]
    for c in cands:
        c["raw"] = {s: df[s] for s in c["sources"]}
        c.setdefault("profile", profile(c["values"]))
        c["norm"] = _norm_series(c["values"])
    # cross-file join: the variant key column against the bank's own records
    key_cand = _key_candidate(cands, ref[KEY[role]])
    aligned_df, join_rate = None, 0.0
    if key_cand is not None:
        ids = [x for x in key_cand["values"].dropna().astype(str).unique()][:SAMPLE]
        bank = reference_rows(role, ids)
        if len(bank):
            bank = bank.set_index(KEY[role])
            keys = key_cand["values"].astype(str)
            join_rate = float(keys.isin(bank.index).mean())
            if join_rate >= 0.5:
                aligned_df = bank.reindex(keys.values)
                aligned_df.index = df.index
                aligned_df[KEY[role]] = keys.where(keys.isin(bank.index), None).values
    scores = {}
    recodes = {}
    for t in targets:
        pt = ref[t]
        ref_aligned = _norm_series(aligned_df[t]) if aligned_df is not None and t in aligned_df.columns else None
        raw_aligned = aligned_df[t] if aligned_df is not None and t in aligned_df.columns else None
        pre = []
        for c in cands:
            vals, rc = c["values"], None
            if pt.get("kind") == "enum" and c["profile"].get("kind") in ("enum", "num") and c["profile"].get("distinct", 99) <= 40:
                rc = _recode_for(vals, pt, raw_aligned)
                if rc:
                    vals = vals.map(lambda x, rc=rc: rc.get(str(x).strip(), x) if x is not None else None)
            ps = profile_sim(profile(vals) if rc else c["profile"], pt)
            ns = name_sim(c["id"].split("@")[0].split("[")[0].split("+")[0], t)
            if c["transform"]["kind"] == "split":
                ns *= 0.5
            pre.append((c, vals, rc, ps, ns))
        # joined-row agreement only for the plausible candidates (profile or name), never for all pairs
        ranked = sorted(pre, key=lambda x: -(0.6 * x[3] + 0.4 * x[4]))
        check = {id(x[0]) for x in ranked[:25]} | {id(x[0]) for x in pre if x[4] >= 0.8}
        for c, vals, rc, ps, ns in pre:
            agree = None
            if ref_aligned is not None and id(c) in check:
                a = _norm_series(vals) if rc else c["norm"]
                both = a.notna() | ref_aligned.notna()
                agree = float((a[both] == ref_aligned[both]).mean()) if both.any() else None
            elif ref_aligned is not None:
                continue
            if agree is not None:
                score = 0.7 * agree + 0.2 * ps + 0.1 * ns
            else:
                score = 0.6 * ps + 0.4 * ns
            scores[(t, c["id"])] = (round(score, 4), round(ps, 4), round(ns, 4), agree)
            if rc:
                recodes[(t, c["id"])] = rc
    # one-to-one assignment, best score first; a whole column and its split parts are mutually exclusive
    by_id = {c["id"]: c for c in cands}
    order = sorted(scores.items(), key=lambda kv: (-kv[1][0], -(kv[1][2]), kv[0]))
    assigned, used_atoms, used_whole_bases, used_split_bases = {}, set(), set(), set()
    floor = 0.5
    for (t, cid), sc in order:
        if t in assigned or sc[0] < floor:
            continue
        if sc[3] is not None and sc[3] < 0.9 and aligned_df is not None:
            continue          # joined rows disagree: a profile look-alike is not the column
        c = by_id[cid]
        base = c.get("base")
        if c["atoms"] & used_atoms:
            continue
        if base and base in used_whole_bases:
            continue
        if not base and any(src in used_split_bases for src in c["sources"]):
            continue
        assigned[t] = cid
        used_atoms |= c["atoms"]
        if base:
            used_split_bases.add(base)
        else:
            used_whole_bases.update(c["sources"])
    cols = []
    key_series = key_cand["values"] if key_cand is not None else None
    for t in targets:
        if t not in assigned:
            continue
        cid = assigned[t]
        c = by_id[cid]
        sc = scores[(t, cid)]
        alts = sorted([(v[0], k[1]) for k, v in scores.items() if k[0] == t and k[1] != cid], reverse=True)[:2]
        margin = sc[0] - (alts[0][0] if alts else 0.0)
        conf = sc[0] if sc[3] is not None else sc[0] * (0.7 + 0.3 * min(1.0, margin / 0.2))
        tr = dict(c["transform"])
        rc = recodes.get((t, cid))
        if rc:
            tr["recode"] = rc
        vals = c["values"].map(lambda x, rc=rc: rc.get(str(x).strip(), x) if (rc and x is not None) else x)
        aligned = aligned_df[t] if aligned_df is not None and t in aligned_df.columns else None
        reasons = [f"profile similarity {sc[1]:.2f}", f"name similarity {sc[2]:.2f}"]
        if sc[3] is not None:
            reasons.insert(0, f"equals the bank's own value on {sc[3]:.1%} of {len(df)} joined rows")
        if rc:
            reasons.append(f"values recoded {rc}")
        cols.append({"target": t, "source": c["sources"], "candidate": cid, "transform": tr, "confidence": round(min(conf, 1.0), 3),
                     "score": sc[0], "agreement": sc[3], "reasons": reasons,
                     "alternatives": [{"candidate": a[1], "score": a[0]} for a in alts],
                     "samples": _samples(key_series, c, vals, aligned)})
    # derived columns, stated with their rule
    derived = []
    have = {x["target"] for x in cols}
    if role == "transactions" and "TransactionDT" not in have and "ts" in have:
        derived.append({"target": "TransactionDT", "rule": f"epoch seconds of ts minus {EPOCH0} (2016-07-02 00:00:00)",
                        "from": "ts", "confidence": 0.99,
                        "reason": "the bank's TransactionDT is seconds since 2016-07-02 00:00:00 (one constant offset on every row)"})
    if role == "transactions" and "ts" not in have and "TransactionDT" in have:
        derived.append({"target": "ts", "rule": f"timestamp of TransactionDT plus {EPOCH0} seconds", "from": "TransactionDT",
                        "confidence": 0.99, "reason": "inverse of the TransactionDT rule"})
    if role in ("closed_cases", "case_pack") and "customer_id" not in have and "card_id" in have:
        derived.append({"target": "customer_id", "rule": "card_id before '-K'", "from": "card_id", "confidence": 0.95,
                        "reason": "card ids are the customer id plus -K and a number"})
    have |= {d["target"] for d in derived}
    used_sources = {s for x in cols for s in x["source"]}
    unresolved = []
    for t in CANON[role]:
        if t in have:
            continue
        best = sorted([(v[0], k[1]) for k, v in scores.items() if k[0] == t], reverse=True)[:3]
        unresolved.append({"target": t, "needed": t in NEEDED[role], "best_candidates": [{"candidate": b[1], "score": b[0]} for b in best]})
    return {"file": path.name, "path": str(path), "role": role, "long": long_spec,
            "key": {"candidate": key_cand["id"] if key_cand else None, "join_rate": round(join_rate, 4)},
            "rows_sampled": int(len(df)), "columns": cols, "derived": derived, "unresolved": unresolved,
            "unused_source_columns": [c for c in df.columns if c not in used_sources and (long_spec is None or c not in long_spec.values())]}


def propose(variant_files, n: int = SAMPLE, verbose: bool = False) -> dict:
    """Work out the mapping of a set of variant files onto the canonical schema. Nothing is written or used until
    approve() records a human approval of this exact mapping."""
    files = [Path(f) for f in variant_files]
    t0 = time.time()
    tables = []
    for f in files:
        df, long_spec = _read_variant(f, n)
        cands = candidates(df)
        rs = _role_scores(df, cands)
        role = max(rs, key=rs.get)
        if verbose:
            print(f"  {f.name}: role {role} {rs}{' (long format, pivoted)' if long_spec else ''}", flush=True)
        tab = _match_table(f, role, df, cands, long_spec, verbose)
        tab["role_scores"] = rs
        tables.append(tab)
    mapping = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "variant_fingerprint": fingerprint(files), "files": tables, "seconds": round(time.time() - t0, 1)}
    roles = {t["role"] for t in tables}
    mapping["missing_roles"] = [r for r in CANON if r not in roles]
    mapping["unresolved_needed"] = [f"{t['file']}:{u['target']}" for t in tables for u in t["unresolved"] if u["needed"]]
    return mapping


def mapping_sha(mapping: dict) -> str:
    body = {k: v for k, v in mapping.items() if k not in ("approval", "approval_sha")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def save(mapping: dict, path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, indent=1, default=str), encoding="utf-8")
    return p


# ------------------------------------------------------------------------------------------------ approval
def approve(mapping_path, token: str, note: str = "") -> dict:
    """Record a human approval of this exact mapping through the approvals chain (role from the token: analyst or
    above, the same gate as the drift re-mapping). Bound to the mapping's sha256."""
    from kavach.harness import approvals
    p = Path(mapping_path)
    mapping = json.loads(p.read_text(encoding="utf-8"))
    sha = mapping_sha(mapping)
    rec = approvals.approve(case_id=f"MAP-{sha[:12]}", action="CREATE_CASE", token=token, exposure=0.0,
                            note=note or f"schema mapping reviewed: approve mapping {sha[:12]} for use")
    mapping["approval"], mapping["approval_sha"] = rec, sha
    save(mapping, p)
    return rec


def approval_status(mapping: dict, files=None) -> str:
    """'' when the mapping carries a valid approval, else the reason it may not be used."""
    from kavach.harness import approvals
    rec = mapping.get("approval")
    if not rec:
        return "the mapping has no approval; run `python -m kavach.mapper approve <mapping.json> --as-role analyst`"
    if mapping.get("approval_sha") != mapping_sha(mapping):
        return "the mapping changed after it was approved; approve it again"
    ok = approvals.verify()
    if not (ok[0] if isinstance(ok, tuple) else ok):
        return "the approvals hash chain does not verify; the approval cannot be trusted"
    if not [r for r in approvals.records(rec.get("case_id")) if r.get("hash") == rec.get("hash") and r.get("status") == "approved"]:
        return "the stored approval is not in the approvals log"
    if files is not None and fingerprint(files) != mapping.get("variant_fingerprint"):
        return "the variant files changed since the mapping was proposed"
    return ""


# ------------------------------------------------------------------------------------------------ apply
def _sql_value(col: dict) -> str:
    """DuckDB expression for one mapped column, from the source table `v` (all varchar)."""
    tr = col["transform"]
    src = col["source"]
    q = lambda c: '"' + c.replace('"', '""') + '"'
    k = tr["kind"]
    if k == "plain":
        e = f"nullif(trim({q(src[0])}), '')"
    elif k == "scale":
        e = f"cast(round(try_cast({q(src[0])} as double) * {tr['factor']}, 2) as varchar)"
    elif k == "epoch":
        mult = 1000000 if tr["unit"] == "s" else 1000
        e = f"strftime(make_timestamp(cast(try_cast({q(src[0])} as double) * {mult} as bigint)), '{TS_FMT}')"
    elif k == "ts_format":
        e = f"strftime(try_strptime(trim({q(src[0])}), '{tr['format']}'), '{TS_FMT}')"
    elif k == "date_time":
        e = (f"strftime(try_strptime(trim({q(src[0])}) || ' ' || trim({q(src[1])}), '{tr['date_format']} {tr['time_format']}'), "
             f"'{TS_FMT}')")
    elif k == "split":
        d = tr["delim"].replace("'", "''")
        e = f"nullif(split_part({q(src[0])}, '{d}', {tr['index'] + 1}), '')"
    elif k == "list":
        d = tr["delim"].replace("'", "''")
        e = f"nullif(array_to_string(list_transform(string_split({q(src[0])}, '{d}'), x -> trim(x)), '|'), '')"
    else:
        raise ValueError(f"unknown transform {k}")
    rc = tr.get("recode")
    if rc:
        whens = " ".join(f"when '{str(a).replace(chr(39), chr(39) * 2)}' then '{str(b).replace(chr(39), chr(39) * 2)}'" for a, b in rc.items())
        e = f"(case trim(coalesce({e}, '')) {whens} else {e} end)"
    return e


def apply(mapping, out_dir=None, files_check: bool = True) -> dict:
    """Write canonical CSVs for an approved mapping. Returns a manifest {role: [{path, round, rows}]}."""
    if isinstance(mapping, (str, Path)):
        mapping = json.loads(Path(mapping).read_text(encoding="utf-8"))
    paths = [Path(t["path"]) for t in mapping["files"]]
    why = approval_status(mapping, paths if files_check else None)
    if why:
        raise MappingNotApproved(why)
    out = Path(out_dir or (VARIANT_DIR / "canonical"))
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    manifest = {"mapping_sha": mapping_sha(mapping), "approval": mapping["approval"].get("id"), "files": []}
    counters: dict = {}
    for tab in mapping["files"]:
        role, path = tab["role"], Path(tab["path"])
        src = f"read_csv('{_p(path)}', header=true, all_varchar=true, sample_size=-1)"
        if tab.get("long"):
            sp = tab["long"]
            con.execute(f'create or replace temp table v as pivot (select "{sp["id"]}", "{sp["key"]}", "{sp["value"]}" from {src}) '
                        f'on "{sp["key"]}" using first("{sp["value"]}") group by "{sp["id"]}"')
        else:
            con.execute(f"create or replace temp table v as select * from {src}")
        exprs = {c["target"]: _sql_value(c) for c in tab["columns"]}
        for d in tab.get("derived", []):
            if d["target"] == "TransactionDT":
                exprs["TransactionDT"] = f"cast(epoch(try_strptime({exprs['ts']}, '{TS_FMT}')) - {EPOCH0} as bigint)"
            elif d["target"] == "ts":
                exprs["ts"] = f"strftime(make_timestamp((try_cast({exprs['TransactionDT']} as bigint) + {EPOCH0}) * 1000000), '{TS_FMT}')"
            elif d["target"] == "customer_id":
                exprs["customer_id"] = f"split_part({exprs['card_id']}, '-K', 1)"
        sel = ", ".join(f'{exprs.get(c, "cast(null as varchar)")} as "{c}"' for c in CANON[role])
        counters[role] = counters.get(role, 0) + 1
        target = out / f"{role}_{counters[role]}.csv"
        order = f' order by try_cast("{KEY[role]}" as bigint) nulls last, "{KEY[role]}"' if role in ("transactions", "identity") else ""
        con.execute(f"copy (select {sel} from v{order}) to '{_p(target)}' (header, delimiter ',')")
        rows = con.execute(f"select count(*) from read_csv('{_p(target)}', header=true, all_varchar=true)").fetchone()[0]
        rnd = 1
        tcol = {"transactions": "ts", "case_pack": "opened_at"}.get(role)
        if tcol:
            mn = con.execute(f"select min(try_cast(\"{tcol}\" as timestamp)) from read_csv('{_p(target)}', header=true, all_varchar=true)").fetchone()[0]
            rnd = 2 if mn is not None and str(mn) >= "2017-01-01" else 1
        manifest["files"].append({"role": role, "path": str(target), "round": rnd, "rows": int(rows), "from": path.name})
    # identity rows of both rounds live in one table; give each identity file the round of the transactions it joins
    con.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


@contextmanager
def canonical_inputs(manifest: dict):
    """Point kavach.data.duck's input paths at the canonical CSVs for the duration of a build."""
    from kavach.data import duck
    keep = (duck.TXN_FILES, duck.IDENT_FILES, duck.CLOSED_FILE, duck.CASE_PACKS)
    by = lambda role: [(Path(f["path"]), f["round"]) for f in manifest["files"] if f["role"] == role]
    txn = sorted(by("transactions"), key=lambda x: x[1])
    ident = [(p, i + 1) for i, (p, _) in enumerate(by("identity"))]
    closed = by("closed_cases")
    try:
        duck.TXN_FILES, duck.IDENT_FILES = txn, ident
        duck.CLOSED_FILE = closed[0][0] if closed else duck.CLOSED_FILE
        duck.CASE_PACKS = sorted(by("case_pack"), key=lambda x: x[1])
        yield
    finally:
        duck.TXN_FILES, duck.IDENT_FILES, duck.CLOSED_FILE, duck.CASE_PACKS = keep


def build_index(manifest, out_path) -> Path:
    """A DuckDB index over the canonical CSVs at out_path (refuses the production index file)."""
    from kavach.data import duck
    if isinstance(manifest, (str, Path)):
        manifest = json.loads(Path(manifest).read_text(encoding="utf-8"))
    out = Path(out_path).resolve()
    if out == Path(duck.DUCK_PATH).resolve() or out.name in ("kavach_r2.duckdb", "kavach.duckdb"):
        raise ValueError(f"refusing to overwrite the production index {out}")
    if out.exists():
        out.unlink()
    with canonical_inputs(manifest):
        duck.build(out_path=out)
    return out


# ------------------------------------------------------------------------------------------------ variant generator
VARIANT_NAMES = {
    "TransactionID": ["txn_ref", "transaction_key", "TxnId"], "ProductCD": ["product_code", "prod_cd", "ProductLine"],
    "addr1": ["billing_region", "bill_addr_1", "RegionCode"], "addr2": ["billing_country", "bill_addr_2", "CountryCode"],
    "dist1": ["distance_1", "dist_a"], "dist2": ["distance_2", "dist_b"],
    "P_emaildomain": ["purchaser_email_domain", "payer_mail"], "R_emaildomain": ["recipient_email_domain", "payee_mail"],
    "customer_id": ["cust_ref", "holder_id", "ClientID"], "risk_score": ["model_score", "risk", "fraud_score"],
    "channel": ["presence", "sales_channel"],
    "case_id": ["case_ref", "CaseNumber"], "card_id": ["card_ref", "card_key"], "opened_at": ["opened_ts", "raised_at"],
    "closed_at": ["closed_ts", "resolved_at"], "outcome": ["result", "disposition"], "pattern": ["typology", "fraud_type"],
    "first_fraud_txn_id": ["first_bad_txn", "first_txn_ref"], "txn_ids": ["txn_list", "transactions"],
    "n_txns": ["txn_count", "num_txns"], "exposure_usd": ["loss_amount", "exposure"],
    "connected_card_ids": ["linked_cards", "related_cards"], "actions_taken": ["actions", "steps_taken"],
    "report_filed": ["sar_filed", "reported"], "analyst_notes": ["notes", "analyst_comment"],
    "trigger_type": ["alert_type", "source"], "trigger_text": ["alert_text", "message"], "flagged_txn_id": ["flagged_txn", "txn_ref"],
}


def _vname(rng, canon: str) -> str:
    fam = _family(canon)
    if fam:
        f, n = fam
        return {"C": f"cnt_{n:02d}", "D": f"days_{n:02d}", "M": f"match_{n}", "V": f"vf_{n:03d}", "id": f"ident_{n:02d}"}[f]
    return rng.choice(VARIANT_NAMES.get(canon, [canon.lower() + "_x"]))


def make_variant(seed: int = 7, out_dir=None, limit: int | None = None, src: dict | None = None) -> dict:
    """Write a renamed, reordered and reshaped variant of the canonical files to out_dir (default data/variant/) plus
    truth.json (which variant column and reshape each canonical column became; propose() never reads it).
    Reshapes: card1..card6 merged into one delimited field; the amount in cents; ts split into a date and a time column;
    TransactionDT replaced by a Unix epoch in milliseconds; channel upper-cased; M flags T/F written Y/N; identity
    melted to (id, attribute, value); closed-case lists with ';'; report_filed 1/0; ISO 'T' timestamps on closed cases;
    day-first timestamps on the case pack; every file renamed and its columns shuffled. limit: rows per file (tests)."""
    rng = random.Random(seed)
    out = Path(out_dir or VARIANT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.csv"):
        old.unlink()
    src = src or REF_FILES
    con = duckdb.connect()
    lim = f" limit {int(limit)}" if limit else ""
    truth = {"seed": seed, "files": {}, "columns": {}}
    q = lambda c: '"' + c + '"'
    delim = rng.choice(["|", ";", "#"])
    txn_names = {c: _vname(rng, c) for c in TXN_COLS if not c.startswith("card") and c not in ("TransactionAmt", "ts", "TransactionDT")}
    card_name, amt_name, date_name, time_name, epoch_name = (rng.choice(["card_profile", "card_details", "CardInfo"]),
                                                              rng.choice(["amount_minor", "amt_cents"]), "txn_date", "txn_time",
                                                              rng.choice(["event_ms", "epoch_millis"]))
    tfiles = [f for f in src["transactions"] if Path(f).exists()]
    for i, f in enumerate(tfiles):
        name = ["ledger_hist.csv", "ledger_new.csv", "ledger_more.csv"][i]
        sel = {}
        for c in TXN_COLS:
            if c in txn_names:
                if c in ("M1", "M2", "M3", "M5", "M6", "M7", "M8", "M9"):
                    sel[txn_names[c]] = f"case {q(c)} when 'T' then 'Y' when 'F' then 'N' else {q(c)} end"
                elif c == "channel":
                    sel[txn_names[c]] = f"upper({q(c)})"
                else:
                    sel[txn_names[c]] = q(c)
        sel[card_name] = f" || '{delim}' || ".join(f"coalesce({q(f'card{k}')}, '')" for k in range(1, 7))
        sel[amt_name] = "cast(round(cast(\"TransactionAmt\" as double) * 100) as bigint)"
        sel[date_name] = "strftime(cast(ts as timestamp), '%Y-%m-%d')"
        sel[time_name] = "strftime(cast(ts as timestamp), '%H:%M:%S')"
        sel[epoch_name] = f"(cast(\"TransactionDT\" as bigint) + {EPOCH0}) * 1000"
        names = list(sel)
        rng.shuffle(names)
        body = ", ".join(f"{sel[n]} as {q(n)}" for n in names)
        con.execute(f"copy (select {body} from read_csv('{_p(f)}', header=true, all_varchar=true, sample_size=-1){lim}) "
                    f"to '{_p(out / name)}' (header, delimiter ',')")
        truth["files"][name] = "transactions"
    for c in TXN_COLS:
        if c in txn_names:
            kind = "recode" if c in ("M1", "M2", "M3", "M5", "M6", "M7", "M8", "M9", "channel") else "plain"
            truth["columns"][f"transactions.{c}"] = {"source": [txn_names[c]], "kind": kind}
    for k in range(1, 7):
        truth["columns"][f"transactions.card{k}"] = {"source": [card_name], "kind": "split", "index": k - 1}
    truth["columns"]["transactions.TransactionAmt"] = {"source": [amt_name], "kind": "scale"}
    truth["columns"]["transactions.ts"] = {"source": [date_name, time_name], "kind": "date_time", "alt": [[epoch_name]]}
    truth["columns"]["transactions.TransactionDT"] = {"source": [epoch_name], "kind": "derived_or_epoch"}
    # identity: long format
    id_names = {c: ("device_info" if c == "DeviceInfo" else "device_class" if c == "DeviceType" else _vname(rng, c)) for c in IDENT_COLS[1:]}
    idk = rng.choice(["txn_ref", "transaction_key"])
    for i, f in enumerate([f for f in src["identity"] if Path(f).exists()]):
        name = ["device_attrs_hist.csv", "device_attrs_new.csv"][i]
        unions = " union all ".join(f"select \"TransactionID\" as {q(idk)}, '{id_names[c]}' as attribute, {q(c)} as attr_value from base where {q(c)} is not null"
                                    for c in IDENT_COLS[1:])
        con.execute(f"create or replace temp table base as select * from read_csv('{_p(f)}', header=true, all_varchar=true, sample_size=-1){lim}")
        con.execute(f"copy ({unions}) to '{_p(out / name)}' (header, delimiter ',')")
        truth["files"][name] = "identity"
    truth["columns"]["identity.TransactionID"] = {"source": [idk], "kind": "long_id"}
    for c in IDENT_COLS[1:]:
        truth["columns"][f"identity.{c}"] = {"source": [id_names[c]], "kind": "long_attribute"}
    # closed cases
    cf = [f for f in src["closed_cases"] if Path(f).exists()]
    if cf:
        cn = {c: _vname(rng, c) for c in CLOSED_COLS}
        cn["customer_id"] = rng.choice(["cust_ref", "holder_id"])
        sel = {}
        for c in CLOSED_COLS:
            if c in ("txn_ids", "connected_card_ids", "actions_taken"):
                sel[cn[c]] = f"replace({q(c)}, '|', ';')"
            elif c == "report_filed":
                sel[cn[c]] = f"case {q(c)} when 'Yes' then '1' when 'No' then '0' else {q(c)} end"
            elif c in ("opened_at", "closed_at"):
                sel[cn[c]] = f"strftime(try_cast({q(c)} as timestamp), '%Y-%m-%dT%H:%M:%S')"
            else:
                sel[cn[c]] = q(c)
        names = list(sel)
        rng.shuffle(names)
        body = ", ".join(f"{sel[n]} as {q(n)}" for n in names)
        con.execute(f"copy (select {body} from read_csv('{_p(cf[0])}', header=true, all_varchar=true, sample_size=-1){lim}) "
                    f"to '{_p(out / 'case_outcomes.csv')}' (header, delimiter ',')")
        truth["files"]["case_outcomes.csv"] = "closed_cases"
        for c in CLOSED_COLS:
            kind = ("list" if c in ("txn_ids", "connected_card_ids", "actions_taken") else "recode" if c == "report_filed"
                    else "ts_format" if c in ("opened_at", "closed_at") else "plain")
            truth["columns"][f"closed_cases.{c}"] = {"source": [cn[c]], "kind": kind}
    # case packs
    pn = {c: _vname(rng, c) for c in PACK_COLS}
    pn["customer_id"] = rng.choice(["cust_ref", "holder_id"])
    pn["flagged_txn_id"] = "flagged_txn"
    for i, f in enumerate([f for f in src["case_pack"] if Path(f).exists()]):
        name = ["alerts_q4.csv", "alerts_jan.csv"][i]
        sel = {pn[c]: (f"strftime(try_cast({q(c)} as timestamp), '%d/%m/%Y %H:%M:%S')" if c == "opened_at" else q(c)) for c in PACK_COLS}
        names = list(sel)
        rng.shuffle(names)
        body = ", ".join(f"{sel[n]} as {q(n)}" for n in names)
        con.execute(f"copy (select {body} from read_csv('{_p(f)}', header=true, all_varchar=true, sample_size=-1)) "
                    f"to '{_p(out / name)}' (header, delimiter ',')")
        truth["files"][name] = "case_pack"
    for c in PACK_COLS:
        truth["columns"][f"case_pack.{c}"] = {"source": [pn[c]], "kind": "ts_format" if c == "opened_at" else "plain"}
    con.close()
    (out / "truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    return truth


def variant_files(d=None) -> list[Path]:
    return sorted(Path(d or VARIANT_DIR).glob("*.csv"))


# ------------------------------------------------------------------------------------------------ scoring
def score(mapping: dict, truth: dict) -> dict:
    """Accuracy of a proposed mapping against the generator's truth: per canonical column, is the source right (and,
    for reshapes, the part)? Roles per file are checked too."""
    got = {}
    role_ok = []
    for tab in mapping["files"]:
        role_ok.append({"file": tab["file"], "role": tab["role"], "truth": truth["files"].get(tab["file"]),
                        "ok": tab["role"] == truth["files"].get(tab["file"])})
        for c in tab["columns"]:
            got.setdefault(f"{tab['role']}.{c['target']}", c)
        for d in tab.get("derived", []):
            got.setdefault(f"{tab['role']}.{d['target']}", {"source": ["(derived) " + d["rule"]], "transform": {"kind": "derived"}})
    rows = []
    for key, t in truth["columns"].items():
        g = got.get(key)
        if g is None:
            rows.append({"column": key, "ok": False, "got": None, "truth": t["source"]})
            continue
        ok = sorted(g["source"]) == sorted(t["source"]) or any(sorted(g["source"]) == sorted(a) for a in t.get("alt", []))
        if t["kind"] == "split":
            ok = ok and g["transform"].get("kind") == "split" and g["transform"].get("index") == t["index"]
        if t["kind"] == "derived_or_epoch":
            ok = ok or g["transform"].get("kind") == "derived"
        rows.append({"column": key, "ok": bool(ok), "got": g["source"], "transform": g["transform"].get("kind"), "truth": t["source"],
                     "confidence": g.get("confidence")})
    by_role = {}
    for r in rows:
        role = r["column"].split(".")[0]
        a = by_role.setdefault(role, [0, 0])
        a[0] += r["ok"]
        a[1] += 1
    needed = [r for r in rows if r["column"].split(".", 1)[1] in NEEDED[r["column"].split(".")[0]]]
    return {"columns": len(rows), "correct": sum(r["ok"] for r in rows), "accuracy": round(sum(r["ok"] for r in rows) / max(len(rows), 1), 4),
            "needed_correct": sum(r["ok"] for r in needed), "needed": len(needed), "by_role": {k: f"{a}/{b}" for k, (a, b) in by_role.items()},
            "roles": role_ok, "wrong": [r for r in rows if not r["ok"]]}


# ------------------------------------------------------------------------------------------------ compare decisions
def investigate(case_ids: list[str], duck_file: str | None = None) -> dict:
    """Run cases (no prose, nothing written) in a fresh process on the index KAVACH_DUCK_FILE selects and return
    their decision fields (kavach.harness.runner.decisions)."""
    import subprocess
    env = dict(os.environ, KAVACH_NO_LLM="1", KAVACH_RECORD="0", KAVACH_CASEFILE_EVENTS="0", PYTHONIOENCODING="utf-8")
    if duck_file:
        env["KAVACH_DUCK_FILE"] = str(duck_file)
    code = ("import json,sys\nfrom kavach import duckq\nfrom kavach.harness.runner import run_case, decisions\n"
            "rows={r['case_id']:r for r in duckq.q('select * from cases').to_dict('records')}\nout={}\n"
            "for c in sys.argv[1:]:\n    o=run_case({k:(None if str(v) in ('nan','None','') else v) for k,v in rows[c].items()},quiet=True)\n"
            "    out[c]=decisions(o.answer) if o.answer else {'incident':(o.incident or {}).get('error')}\n"
            "print('@@'+json.dumps(out,default=str))\n")
    from kavach.config import ROOT
    r = subprocess.run([sys.executable, "-c", code, *case_ids], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=1800)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("@@")]
    if not line:
        raise RuntimeError(f"investigation run failed: {r.stderr[-1500:]}")
    return json.loads(line[-1][2:])


# ------------------------------------------------------------------------------------------------ CLI
def _token(args) -> str | None:
    if getattr(args, "token", None):
        return args.token
    env = {"analyst": "KAVACH_TOKEN_ANALYST", "L1": "KAVACH_TOKEN_L1", "L2": "KAVACH_TOKEN_L2"}.get(getattr(args, "as_role", None) or "")
    return os.environ.get(env, "") if env else None


def summary(mapping: dict) -> str:
    lines = [f"mapping over {len(mapping['files'])} files ({mapping['seconds']} s), missing roles {mapping['missing_roles'] or 'none'}, "
             f"needed columns unresolved {len(mapping['unresolved_needed'])}"]
    for t in mapping["files"]:
        lines.append(f"  {t['file']}: {t['role']}{' (long, pivoted)' if t['long'] else ''}, key {t['key']['candidate']} joins "
                     f"{t['key']['join_rate']:.0%} of sampled rows to the bank's records; {len(t['columns'])} mapped, "
                     f"{len(t['derived'])} derived, {len(t['unresolved'])} unresolved, unused {t['unused_source_columns'][:6]}")
        for c in t["columns"]:
            if c["transform"]["kind"] != "plain" or c["confidence"] < 0.8:
                lines.append(f"    {c['target']:20s} <- {c['candidate']:24s} {c['transform']['kind']:10s} conf {c['confidence']:.2f}  {c['reasons'][0]}")
        for d in t["derived"]:
            lines.append(f"    {d['target']:20s} <- derived: {d['rule']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.mapper", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("variant")
    v.add_argument("--seed", type=int, default=7)
    v.add_argument("--out", default=None)
    v.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("propose")
    p.add_argument("files", nargs="*")
    p.add_argument("--dir", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--sample", type=int, default=SAMPLE)
    a = sub.add_parser("approve")
    a.add_argument("mapping")
    a.add_argument("--token", default=None)
    a.add_argument("--as-role", default=None, choices=("analyst", "L1", "L2"))
    a.add_argument("--note", default="")
    y = sub.add_parser("apply")
    y.add_argument("mapping")
    y.add_argument("--out", default=None)
    i = sub.add_parser("index")
    i.add_argument("--manifest", default=None)
    i.add_argument("--out", default=None)
    s = sub.add_parser("score")
    s.add_argument("mapping")
    s.add_argument("--truth", default=None)
    c = sub.add_parser("compare")
    c.add_argument("cases", nargs="+")
    c.add_argument("--index", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "variant":
        t = make_variant(args.seed, args.out, args.limit)
        print(f"variant written to {args.out or VARIANT_DIR}: {', '.join(t['files'])} ({len(t['columns'])} canonical columns in truth.json)")
        return 0
    if args.cmd == "propose":
        files = [Path(f) for f in args.files] or variant_files(args.dir)
        m = propose(files, n=args.sample, verbose=True)
        out = save(m, args.out or (Path(args.dir or VARIANT_DIR) / "mapping.json"))
        print(summary(m))
        print(f"mapping written to {out} (sha {mapping_sha(m)[:12]}); approve it before use")
        return 0
    if args.cmd == "approve":
        from kavach.harness.faults import Forbidden
        try:
            rec = approve(args.mapping, _token(args), args.note)
        except Forbidden as ex:
            print(f"DENIED: {ex}")
            return 3
        print(f"approved by role {rec['role']} (record {rec['id']}, route {rec['route']})")
        return 0
    if args.cmd == "apply":
        try:
            man = apply(args.mapping, args.out)
        except MappingNotApproved as ex:
            print(f"NOT APPLIED: {ex}")
            return 3
        for f in man["files"]:
            print(f"  {f['role']:13s} round {f['round']} {f['rows']:>9,} rows -> {f['path']}")
        return 0
    if args.cmd == "index":
        man = Path(args.manifest or (VARIANT_DIR / "canonical" / "manifest.json"))
        out = build_index(man, args.out or (VARIANT_DIR / "kavach_variant.duckdb"))
        print(f"index built at {out}")
        return 0
    if args.cmd == "score":
        m = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
        t = json.loads(Path(args.truth or (Path(args.mapping).parent / "truth.json")).read_text(encoding="utf-8"))
        r = score(m, t)
        print(f"columns {r['correct']}/{r['columns']} ({r['accuracy']:.1%}); needed by the agent {r['needed_correct']}/{r['needed']}; "
              f"by role {r['by_role']}; roles {sum(x['ok'] for x in r['roles'])}/{len(r['roles'])} files")
        for w in r["wrong"][:40]:
            print(f"  WRONG {w['column']}: got {w['got']} truth {w['truth']}")
        return 0
    if args.cmd == "compare":
        idx = Path(args.index or (VARIANT_DIR / "kavach_variant.duckdb")).resolve()
        base = investigate(args.cases)
        var = investigate(args.cases, str(idx))
        same = 0
        for cid in args.cases:
            b, vv = base.get(cid, {}), var.get(cid, {})
            diff = sorted(k for k in set(b) | set(vv) if b.get(k) != vv.get(k))
            same += not diff
            print(f"{cid}: normal index {b.get('verdict')} p={b.get('p')} {b.get('pattern')} | variant index {vv.get('verdict')} "
                  f"p={vv.get('p')} {vv.get('pattern')} | {'identical' if not diff else 'differs in ' + ','.join(diff)}")
        print(f"identical decisions {same}/{len(args.cases)}")
        return 0 if same == len(args.cases) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
