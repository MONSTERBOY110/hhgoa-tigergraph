"""Result digests for trace entries: what came back from a tool call, in a form that can be compared on a re-run.

result_digest(out) -> {"n_rows", "n_ids", "ids" (up to 20, sorted), "sha1" (of every collected id, sorted, one per line)}
Ids are collected from columns or keys named TransactionID, card_id, customer_id, case_id, device_key, and from
id-shaped strings inside tuples (e.g. (case_id, score) pairs from vector search).
"""
import hashlib
import re

import pandas as pd

ID_FIELDS = ("TransactionID", "card_id", "customer_id", "case_id", "device_key")
ID_RE = re.compile(r"^(?:C\d{5}-K\d+|C\d{5}|CC-\d{4,}|HH[GF]-\d{3}|\d{7})$")
MAX_IDS = 20


def _norm(v) -> str | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s or None


def _from_mapping(m: dict, acc: set) -> None:
    for k in ID_FIELDS:
        if k in m:
            v = m[k]
            for x in (v if isinstance(v, (list, tuple, set)) else [v]):
                s = _norm(x)
                if s:
                    acc.add(s)


def collect_ids(out) -> set:
    acc: set = set()
    if isinstance(out, pd.DataFrame):
        for k in ID_FIELDS:
            if k in out.columns:
                for x in out[k].dropna().unique().tolist():
                    s = _norm(x)
                    if s:
                        acc.add(s)
    elif isinstance(out, pd.Series):
        _from_mapping(out.to_dict(), acc)
    elif isinstance(out, dict):
        _from_mapping(out, acc)
    elif isinstance(out, (list, tuple)):
        for item in out:
            if isinstance(item, dict):
                _from_mapping(item, acc)
            elif isinstance(item, (list, tuple)):
                for x in item:
                    if isinstance(x, str) and ID_RE.match(x):
                        acc.add(x)
            elif isinstance(item, str) and ID_RE.match(item):
                acc.add(item)
    return acc


def n_rows(out) -> int:
    """Same rule the registry always used: rows for frames and lists, else 1 for a truthy result."""
    if isinstance(out, (pd.DataFrame, list)):
        return len(out)
    try:
        return 1 if out else 0
    except (TypeError, ValueError):
        return 1


def result_digest(out) -> dict:
    ids = sorted(collect_ids(out))
    return {"n_rows": n_rows(out), "n_ids": len(ids), "ids": ids[:MAX_IDS],
            "sha1": hashlib.sha1("\n".join(ids).encode()).hexdigest()}
