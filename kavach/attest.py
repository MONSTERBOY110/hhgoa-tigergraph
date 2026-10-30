"""Attestation sidecars: every claim the agent makes carries what, evidence ids, how (query plus parameters) and when.

    build(case_id, answer, signals, trace, backend, dataset_version, sar_sentences=None) -> sidecar dict
    write(case_id, sidecar) -> attestations/<case>.json
    verify(case_id, backend="duck") -> one row per claim: pass | fail | unverifiable, with a reason

A claim passes when its `how` re-runs through a fresh kavach.tools.ToolRegistry and every id in its `evidence`
appears in what comes back. Claims without a recorded query are reported as unverifiable, never silently passed.
CLI: python -m kavach.attest <case> | --all [--backend duck] [--build]
"""
import datetime as dt
import hashlib
import json
import math
import re
import sys
from pathlib import Path

from kavach.config import CASES_DIR, LOGS, RAW, RAW2, ROOT, env
from kavach.llm import ID_RE

KINDS = ("evidence", "sar_sentence", "detector_hit")
ID_KEYS = ("txn_ids", "card_ids", "customer_ids", "case_ids", "device_keys", "other")

_TXN = re.compile(r"^\d{7}$")
_CARD = re.compile(r"^C\d{5}-K\d+$")
_CUST = re.compile(r"^C\d{5}$")
_CASE = re.compile(r"^CC-\d{4}$")
_TOKEN_SPLIT = re.compile(r"[\s,;:'\"\[\]\(\)\{\}=]+")


def attest_dir() -> Path:
    return Path(env("KAVACH_ATTEST_DIR") or (ROOT / "attestations"))


# ---------------------------------------------------------------------------------------------------------------
# ids and parameters
def split_ids(ids) -> dict:
    """Group ids by shape: 7-digit transactions, Cxxxxx-Kn cards, Cxxxxx customers, CC-nnnn closed cases,
    device keys (contain ' | '), everything else under 'other'."""
    out = {k: [] for k in ID_KEYS}
    for x in ids or []:
        s = _norm_scalar(x)
        if not s:
            continue
        key = ("txn_ids" if _TXN.match(s) else "card_ids" if _CARD.match(s) else "customer_ids" if _CUST.match(s)
               else "case_ids" if _CASE.match(s) else "device_keys" if " | " in s else "other")
        if s not in out[key]:
            out[key].append(s)
    return out


def all_ids(evidence: dict) -> list[str]:
    return [x for k in ID_KEYS for x in (evidence or {}).get(k, [])]


def _norm_scalar(v) -> str:
    """String form used for id comparison: integral floats lose their '.0', numpy scalars become Python values."""
    if v is None:
        return ""
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        if v.is_integer():
            return str(int(v))
    return str(v).strip()


def _jsonable(v):
    """Parameters as plain JSON so a judge can re-run them: timestamps become ISO strings, numpy scalars Python values."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------------------------
# dataset version
def dataset_version() -> dict:
    """kavach.duckq.dataset_version() when the index records it, else file sizes over data/raw and data/raw/round2."""
    try:
        from kavach import duckq
        if hasattr(duckq, "dataset_version"):
            v = duckq.dataset_version()
            if v and v.get("version") not in (None, "", "unknown"):
                return v
    except Exception:
        pass
    return files_version()


def files_version() -> dict:
    files = {}
    for base, pattern in ((RAW, "*"), (RAW2, "**/*")):
        if base.exists():
            for p in sorted(base.glob(pattern)):
                if p.is_file():
                    files[str(p.relative_to(RAW)).replace("\\", "/")] = p.stat().st_size
    ver = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    return {"version": f"files-{ver}", "built_at": "", "files": files}


def _version_id(v) -> str:
    if isinstance(v, dict):
        return str(v.get("version") or hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:12])
    return str(v or "")


# ---------------------------------------------------------------------------------------------------------------
# SAR sentences with their citations
def sar_sentences_template(facts: dict) -> list[tuple[str, list[str]]]:
    """kavach.narrative.sar_template, sentence by sentence, with the ids each sentence rests on.
    " ".join(sentence for sentence, _ in result) equals narrative.sar_template(facts)."""
    from kavach.narrative import PATTERN_WORDS
    ep_ids = [str(t["id"]) for t in facts.get("episode_txns", []) if t.get("id")]
    ep_devs = list(dict.fromkeys(str(t["device"]) for t in facts.get("episode_txns", []) if t.get("device")))
    cust, card = str(facts["customer_id"]), str(facts["card_id"])
    first = str(facts["first_txn"])
    s = [
        (f"This report concerns customer {facts['customer_id']}, card {facts['card_id']}, issued as a {facts['card_desc']}.",
         [cust, card]),
        ((f"On {facts['date_from']}, " if facts['date_from'] == facts['date_to'] else f"Between {facts['date_from']} and {facts['date_to']}, ")
         + f"{facts['n_txns']} transaction(s) totalling ${facts['exposure']:,.2f} "
         f"were identified as part of one fraud episode, beginning with transaction {facts['first_txn']}.",
         list(dict.fromkeys([first] + ep_ids))),
        (f"The activity took place {facts['where']}.",
         list(dict.fromkeys(ep_ids + [d for d in ep_devs if d in str(facts['where'])]))),
        (f"The transactions were {facts['how']}.", ep_ids),
        (f"The activity is assessed as {PATTERN_WORDS[facts['pattern']]}: {facts.get('top_fraud_plain') or facts['top_fraud']}.",
         list(dict.fromkeys(ID_RE.findall(str(facts.get('top_fraud_plain') or facts['top_fraud'])) or ep_ids))),
    ]
    if facts["n_connected"]:
        s.append((f"The shared element ({facts['shared']}) links this activity to {facts['n_connected']} other card(s), including "
                  f"{', '.join(facts['connected'][:4])}, which indicates a common actor across cardholders.",
                  [card] + [str(c) for c in facts["connected"]]))
    else:
        s.append(("No other cardholder was found to share a device profile, region cluster or email with this activity.", [card]))
    if facts.get("prior_cases"):
        s.append((f"Earlier confirmed cases {', '.join(facts['prior_cases'][:3])} show the same pattern.",
                  [str(c) for c in facts["prior_cases"][:3]]))
    s.append((f"{facts['customer_statement']}", [cust]))
    s.append((f"The bank has recommended {facts['containment'] or 'containment of the card'} and opened an internal case; "
              f"the report is filed under {facts['rule']}.", [card]))
    return [(t.strip(), ids) for t, ids in s if t.strip()]


def template_ids(template_sentences) -> set:
    return {str(i) for _, ids in template_sentences for i in ids}


def check_llm_sar(text: str, template_sentences) -> bool:
    """Accept an LLM rewrite of the SAR only when every id it uses is cited by some template sentence."""
    if not text or not text.strip():
        return False
    return set(ID_RE.findall(text)) <= template_ids(template_sentences)


def _split_sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", (text or "").strip()) if x.strip()]


def _words(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9$.,-]+", t.lower()) if len(w) > 2}


def cite_sentences(text: str, template_sentences=None, extra_ids=()) -> list[tuple[str, list[str]]]:
    """Sentence-level citations for a narrative that did not come from the template (an accepted LLM rewrite, or an
    old answer file): ids written in the sentence, device keys from the template or extra_ids found in it, and for a
    sentence that names no id, the ids of the closest template sentence by word overlap."""
    tmpl = list(template_sentences or [])
    known = [str(i) for i in list(template_ids(tmpl)) + list(extra_ids) if " | " in str(i)]
    out = []
    for sent in _split_sentences(text):
        ids = list(dict.fromkeys(ID_RE.findall(sent) + [d for d in known if d in sent]))
        if not ids and tmpl:
            w = _words(sent)
            best = max(tmpl, key=lambda ts: len(w & _words(ts[0])) / (len(w | _words(ts[0])) or 1))
            ids = list(best[1])
        out.append((sent, ids))
    return out


# ---------------------------------------------------------------------------------------------------------------
# build and write
def _sig_dict(s) -> dict:
    d = dict(s.__dict__) if hasattr(s, "__dict__") else dict(s)
    d["how"] = d.get("how") or {}
    return d


def _how_from_ref(ref: str, case_id: str) -> dict:
    """Only refs that name their source exactly become a query; formatted query refs are never parsed (their
    parameters are abbreviated), so those need Signal.how."""
    ref = ref or ""
    if ref.startswith("document:README#"):
        return {"tool": "document", "params": {"doc_id": ref.split("#", 1)[-1]}}
    if ref.startswith("case_pack:"):
        return {"tool": "case_pack_row", "params": {"case_id": case_id}}
    return {}


def _match_signal(item: dict, sigs: list[dict], used: set):
    for i, s in enumerate(sigs):
        if i not in used and s.get("claim") == item.get("claim"):
            return i
    cands = [i for i, s in enumerate(sigs) if i not in used and s.get("ref") and s.get("ref") == item.get("ref")]
    if cands:
        ids = set(item.get("entity_ids") or [])
        return max(cands, key=lambda i: len(ids & {str(x) for x in sigs[i].get("entity_ids") or []}))
    return None


def build(case_id: str, answer: dict, signals, trace: dict | None, backend: str, dataset_version: dict | None,
          sar_sentences=None) -> dict:
    """The attestation sidecar for one answer (TRD2 section 8). `signals` are kavach.assess.Signal objects or their
    dicts (as stored in the trace); `sar_sentences` is a list of (sentence, ids), normally from
    sar_sentences_template(facts) or cite_sentences(llm_text, template)."""
    dv = dataset_version if dataset_version is not None else globals()["dataset_version"]()
    run_at = _now()
    when = {"ts": run_at, "dataset_version": _version_id(dv)}
    sigs = [_sig_dict(s) for s in (signals if signals is not None else (trace or {}).get("signals", []))]
    used: set = set()
    claims = []

    for n, item in enumerate((answer.get("case") or {}).get("evidence") or [], 1):
        i = _match_signal(item, sigs, used)
        sig = sigs[i] if i is not None else {}
        if i is not None:
            used.add(i)
        how = _jsonable(sig.get("how") or {}) or _how_from_ref(item.get("ref", ""), case_id)
        claims.append({"id": f"E{n}", "kind": "evidence", "what": item.get("claim", ""),
                       "evidence": split_ids(item.get("entity_ids")), "how": how, "when": dict(when),
                       "source": item.get("source", ""), "ref": item.get("ref", ""),
                       "signal": sig.get("name", ""), "llr": sig.get("llr")})

    # detector hits the assessment used that did not make the answer's evidence list
    n = 0
    for i, s in enumerate(sigs):
        if i in used or not s.get("llr") or not (s.get("entity_ids") or s.get("how")):
            continue
        n += 1
        claims.append({"id": f"D{n}", "kind": "detector_hit", "what": s.get("claim", ""),
                       "evidence": split_ids(s.get("entity_ids")),
                       "how": _jsonable(s.get("how") or {}) or _how_from_ref(s.get("ref", ""), case_id),
                       "when": dict(when), "source": s.get("source", ""), "ref": s.get("ref", ""),
                       "signal": s.get("name", ""), "llr": s.get("llr")})

    sar = answer.get("sar") or {}
    if sar_sentences is None and sar.get("file") and sar.get("narrative"):
        sar_sentences = cite_sentences(sar["narrative"], None, sar.get("subjects") or [])
    ev_claims = list(claims)
    for n, (sent, ids) in enumerate(sar_sentences or [], 1):
        ev = split_ids(ids)
        idset = set(all_ids(ev))
        support = [c["id"] for c in ev_claims if idset & set(all_ids(c["evidence"]))]
        params = {k: v for k, v in ev.items() if v and k != "other"}
        claims.append({"id": f"S{n}", "kind": "sar_sentence", "what": sent, "evidence": ev,
                       "how": {"tool": "lookup_ids", "params": params} if params else {},
                       "when": dict(when), "supported_by": support})

    with_how = sum(1 for c in claims if c["how"])
    return {"case_id": case_id, "dataset_version": dv, "backend": backend, "run_at": run_at, "claims": claims,
            "counts": {"claims": len(claims), "with_how": with_how,
                       "by_kind": {k: sum(1 for c in claims if c["kind"] == k) for k in KINDS}}}


def write(case_id: str, sidecar: dict, out_dir: Path | str | None = None) -> Path:
    d = Path(out_dir) if out_dir else attest_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{case_id}.json"
    p.write_text(json.dumps(sidecar, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    return p


def load(case_id: str, attest_dir_: Path | str | None = None) -> dict:
    p = (Path(attest_dir_) if attest_dir_ else attest_dir()) / f"{case_id}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def build_from_files(case_id: str, cases_dir: Path | str | None = None, traces_dir: Path | str | None = None,
                     backend: str = "duck") -> dict:
    """Sidecar for an answer already on disk (cases/<id>.json plus logs/traces/<id>.json); SAR citations come from
    the narrative itself."""
    a = json.loads((Path(cases_dir or CASES_DIR) / f"{case_id}.json").read_text(encoding="utf-8"))
    tp = Path(traces_dir or (LOGS / "traces")) / f"{case_id}.json"
    trace = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {}
    return build(case_id, a, trace.get("signals", []), trace, backend, None)


# ---------------------------------------------------------------------------------------------------------------
# verify
def _lookup_ids(txn_ids=(), card_ids=(), customer_ids=(), case_ids=(), device_keys=()):
    """Which of the given ids exist in the index (one query per id kind). The `how` of a SAR sentence."""
    from kavach.duckq import q
    found = []
    if txn_ids:
        found += [str(x) for x in q("select TransactionID from txn where TransactionID in (select unnest(?))",
                                    [int(t) for t in txn_ids]).TransactionID]
    if card_ids:
        found += list(q("select distinct card_id from txn where card_id in (select unnest(?))", list(card_ids)).card_id)
    if customer_ids:
        found += list(q("select distinct customer_id from txn where customer_id in (select unnest(?))",
                        list(customer_ids)).customer_id)
    if case_ids:
        found += list(q("select case_id from closed where case_id in (select unnest(?))", list(case_ids)).case_id)
    if device_keys:
        # ids are stored trimmed (see _id), but device keys can end in spaces ("SM-J700T1 |  |  | "): compare trimmed forms
        found += list(q("select distinct trim(device_key) k from txn where trim(device_key) in (select unnest(?))",
                        [str(d).strip() for d in device_keys]).k)
    return found


def _case_pack_row(case_id: str) -> dict:
    from kavach.duckq import q
    df = q("select * from cases where case_id = ?", case_id)
    return df.iloc[0].to_dict() if len(df) else {}


def _document(doc_id: str) -> dict:
    from kavach import vectors
    ix = vectors.index()
    for i, sec, txt in zip(ix["doc_ids"], ix["doc_sections"], ix["doc_texts"]):
        if str(i) == doc_id:
            return {"doc_id": str(i), "section": str(sec), "text": str(txt)}
    return {}


LOCAL_TOOLS = {"lookup_ids": _lookup_ids, "case_pack_row": _case_pack_row, "document": _document}


def _tokens(out) -> set:
    """Every value in a tool result as a comparable string (DataFrame: any column; dict: any value; list of tuples:
    first element; scalar: itself). Text cells also contribute their tokens so ids inside notes or lists match."""
    import pandas as pd
    vals = []
    if isinstance(out, pd.DataFrame):
        for col in out.columns:
            vals += list(out[col].values)
    elif isinstance(out, dict):
        stack = list(out.values())
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                stack += list(v.values())
            elif isinstance(v, (list, tuple, set)):
                stack += list(v)
            else:
                vals.append(v)
    elif isinstance(out, (list, tuple)):
        for x in out:
            if isinstance(x, tuple):
                vals.append(x[0] if x else None)
            elif isinstance(x, dict):
                vals += list(_tokens(x))
            else:
                vals.append(x)
    else:
        vals.append(out)
    toks = set()
    for v in vals:
        if isinstance(v, (list, tuple, set)):
            toks |= _tokens(list(v))
            continue
        s = _norm_scalar(v)
        if not s:
            continue
        toks.add(s)
        if isinstance(v, str) and len(s) > 7:
            toks |= {t.rstrip(".") for t in _TOKEN_SPLIT.split(s) if t}
    return toks


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", str(t).replace("**", "").replace("`", "")).strip()


def _check_document(claim: dict, out: dict) -> tuple[str, str]:
    if not out:
        return "fail", f"document {claim['how']['params'].get('doc_id')} not found"
    text = _norm_text(out["text"])
    pieces = [p.strip().removesuffix("...").strip().rstrip(".") for p in _norm_text(claim["what"]).split(": ")]
    quoted = [p for p in pieces if len(p) >= 20]
    if quoted and any(p in text for p in quoted):
        return "pass", f"document {out['doc_id']} contains the quoted text"
    return "fail", f"document {out['doc_id']} does not contain the quoted text"


def verify_claim(claim: dict, tools, current_version: str = "") -> dict:
    how = claim.get("how") or {}
    ids = all_ids(claim.get("evidence"))
    row = {"id": claim.get("id", ""), "kind": claim.get("kind", ""), "tool": how.get("tool", ""), "n_ids": len(ids),
           "status": "unverifiable", "reason": "", "missing": []}
    if not how.get("tool"):
        ref = claim.get("ref") or ""
        row["reason"] = ("unverifiable: assumed customer response, not a data query" if ref.startswith("evidence_request")
                         else "unverifiable: the sentence cites no ids" if claim.get("kind") == "sar_sentence" and not ids
                         else "unverifiable: no query recorded")
        return row
    if not ids and how["tool"] != "document":
        row["reason"] = "unverifiable: the claim cites no ids"
        return row
    try:
        fn = LOCAL_TOOLS.get(how["tool"])
        params = how.get("params") or {}
        out = fn(**params) if fn else tools.call(how["tool"], **params)
    except Exception as e:
        row["status"], row["reason"] = "fail", f"re-run failed: {type(e).__name__}: {str(e)[:120]}"
        return row
    if how["tool"] == "document":
        row["status"], row["reason"] = _check_document(claim, out)
        return row
    toks = _tokens(out)
    missing = [i for i in ids if i not in toks]
    n = len(out) if isinstance(out, (list, tuple)) or hasattr(out, "columns") else (1 if out else 0)
    row["missing"] = missing
    if missing:
        row["status"] = "fail"
        row["reason"] = f"{len(missing)} of {len(ids)} ids not in the re-run result ({n} rows): {', '.join(missing[:4])}"
    else:
        row["status"] = "pass"
        row["reason"] = f"all {len(ids)} ids found in {n} rows"
    claimed = (claim.get("when") or {}).get("dataset_version")
    if current_version and claimed and claimed != current_version:
        row["reason"] += f"; dataset changed since the run ({claimed} -> {current_version})"
    return row


def verify(case_id: str, backend: str = "duck", attest_dir_: Path | str | None = None) -> list[dict]:
    """Re-run every claim's query through a fresh ToolRegistry and check its ids against the result."""
    from kavach.tools import ToolRegistry
    side = load(case_id, attest_dir_)
    if backend in (None, "", "auto"):          # re-run each case on the lane that produced it (recorded in the sidecar)
        backend = (load(case_id, attest_dir_) or {}).get("backend") or "duck"
    tools = ToolRegistry(backend)
    cur = _version_id(dataset_version())
    return [verify_claim(c, tools, cur) for c in side.get("claims", [])]


# ---------------------------------------------------------------------------------------------------------------
# CLI
def render(case_id: str, rows: list[dict]) -> str:
    lines = [f"attest {case_id}", f"{'claim':6s} {'kind':13s} {'status':13s} {'ids':>4s} {'tool':22s} reason"]
    for r in rows:
        lines.append(f"{r['id']:6s} {r['kind']:13s} {r['status']:13s} {r['n_ids']:>4d} {r['tool'][:22]:22s} {r['reason']}")
    c = {s: sum(1 for r in rows if r["status"] == s) for s in ("pass", "fail", "unverifiable")}
    lines.append(f"{case_id}: {len(rows)} claims, pass {c['pass']}, fail {c['fail']}, unverifiable {c['unverifiable']}")
    return "\n".join(lines)


def _utf8_stdout() -> None:
    """Windows consoles and pipes default to cp1252; policy text carries the section sign."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m kavach.attest", description="Re-run and verify attested claims.")
    ap.add_argument("case", nargs="?", help="case id, e.g. HHF-003")
    ap.add_argument("--all", action="store_true", help="every sidecar in attestations/")
    ap.add_argument("--backend", default="auto", help="auto: the lane recorded in each sidecar (duck or tg)")
    ap.add_argument("--build", action="store_true",
                    help="first (re)build the sidecar from cases/<id>.json and logs/traces/<id>.json")
    ap.add_argument("--dir", default=None, help="attestations directory (default attestations/)")
    a = ap.parse_args(argv)
    _utf8_stdout()
    d = Path(a.dir) if a.dir else attest_dir()
    if a.all:
        ids = sorted(p.stem for p in d.glob("*.json"))
        if a.build:
            ids = sorted(set(ids) | {p.stem for p in CASES_DIR.glob("*.json")})
    elif a.case:
        ids = [a.case]
    else:
        ap.error("give a case id or --all")
    if not ids:
        print(f"no attestation sidecars in {d}")
        return 1
    failed = 0
    for cid in ids:
        if a.build:
            write(cid, build_from_files(cid, backend=a.backend), d)
        if not (d / f"{cid}.json").exists():
            print(f"{cid}: no attestation sidecar at {d / (cid + '.json')}; a claim nobody can check counts as wrong")
            failed += 1
            continue
        rows = verify(cid, a.backend, d)
        print(render(cid, rows))
        failed += sum(1 for r in rows if r["status"] == "fail")
    if len(ids) > 1:
        print(f"attest: {len(ids)} cases, {failed} failing claim(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
