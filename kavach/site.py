"""Build the static case viewer's data files from what the pipeline wrote to disk.

    build(out=None, sources=None, verify=True, quiet=False) -> summary dict
    ring_frames(answer, sidecar=None, flagged_txn_id=None) -> dict | None

site/cases.js          window.KAVACH = {meta, rounds, cases, monitor, detectors}
site/data/<case>.js    window.KAVACH_DETAIL["<case>"] = {replay, attestation, verify, casefile, ring}

Sources: answer files (cases/*.json for Round 2, round1/cases/*.json for Round 1), traces (logs/traces), attestation
sidecars (attestations/), case files (data/casefiles via kavach.casefile), monitor findings (monitor/, round1/monitor/),
grown detectors (detectors/approved|proposed|rejected) and typology backtests (detectors/backtests). Transaction
details, the alert rows and the ring replay frames are read from the DuckDB index (read-only). Nothing is computed
that the page then presents as the agent's work: a missing trace, sidecar or case file is exported as
{"available": false, "reason": ...} and the page says so. Every file is a script assignment, so the page also works
from file:// without a server.

Ring replay frames are re-runnable: `python -m kavach.site ring <case>` runs the same query with the same parameters
and prints the frames; the exported frames carry the SQL, its parameters, the dataset version and the time.
CLI: python -m kavach.site [build] [--no-verify] | ring <case> [--json]
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import math
import re
import sys
from pathlib import Path

from kavach.config import CASES_DIR, LOGS, ROOT

SITE = ROOT / "site"
ROUND1 = ROOT / "round1"
CARD_RE = re.compile(r"^C\d{5}-K\d+$")
DETAIL_RE = re.compile(r"^[A-Za-z0-9_.-]+\.js$")
RING_MIN_CARDS = 3          # an evidence claim naming this many cards describes a ring (kavach.detectors.network)
RING_DAYS = 30.0            # default half-window around the flagged transaction (the device ring query's window)
RING_PAD_DAYS = 1.0         # the episode window is the ring's active span plus this much on each side
RING_MAX_ROWS = 2500        # above this, rows that carry no shared value and no cited id are omitted (and counted)
RING_CONCENTRATION = 0.2    # an element the agent did not name links the ring only when >= 20% of its cards are ring cards
SHARED_FIELDS = ("device_key", "R_emaildomain", "P_emaildomain", "addr1")
FIELD_LABEL = {"device_key": "device profile", "R_emaildomain": "recipient email domain",
               "P_emaildomain": "purchaser email domain", "addr1": "billing region"}
RING_SQL = ("select TransactionID, card_id, strftime(ts, '%Y-%m-%d %H:%M:%S') ts, TransactionAmt amt, ProductCD, "
            "device_key, R_emaildomain, P_emaildomain, cast(addr1 as integer) addr1 from txn "
            "where card_id in (select unnest(?)) and ts between ? and ? order by ts, TransactionID")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(p) -> str:
    p = Path(p)
    try:
        return str(p.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:            # outside the repo (an env override): never ship a local machine path
        return "/".join(p.parts[-2:])


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:300]}"


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return str(v).strip() in ("", "nan", "None", "NaT", "<NA>")


# ---------------------------------------------------------------------------------------------------------------
# DuckDB access (read-only); every caller survives its absence
class _Duck:
    def __init__(self):
        self.error = ""
        try:
            from kavach import duckq
            duckq.con()
            self.duckq = duckq
        except Exception as e:          # no index, or locked by a writer: export what the files hold
            self.duckq = None
            self.error = f"DuckDB index unavailable: {_err(e)}"

    @property
    def ok(self) -> bool:
        return self.duckq is not None

    def q(self, sql: str, *params):
        if not self.ok:
            raise RuntimeError(self.error)
        return self.duckq.q(sql, *params)

    def version(self) -> str:
        if not self.ok:
            return ""
        try:
            return str(self.duckq.dataset_version().get("version") or "")
        except Exception:
            return ""


def _pack_rows(db: _Duck) -> dict:
    if not db.ok:
        return {}
    try:
        df = db.q("select * from cases")
    except Exception:
        return {}
    out = {}
    for r in df.to_dict("records"):
        out[str(r["case_id"])] = {k: (None if _blank(v) else v) for k, v in r.items()}
    return out


def _txn_rows(db: _Duck, ids: list[str]) -> dict:
    ids = [x for x in ids if str(x).isdigit()]
    if not ids or not db.ok:
        return {}
    df = db.q("""select TransactionID, strftime(ts, '%Y-%m-%d %H:%M') ts, TransactionAmt amt, ProductCD, channel,
                        cast(addr1 as integer) addr1, card_id from txn where TransactionID in (select unnest(?))""",
              [int(x) for x in ids])
    return {str(r.TransactionID): {"ts": r.ts, "amt": round(float(r.amt), 2), "product": r.ProductCD, "channel": r.channel,
                                   "region": None if _blank(r.addr1) else int(r.addr1), "card": r.card_id}
            for r in df.itertuples()}


def _closed(db: _Duck, ids: list[str]) -> dict:
    if not ids or not db.ok:
        return {}
    df = db.q("select case_id, outcome, pattern, exposure_usd from closed where case_id in (select unnest(?))", list(ids))
    return {r.case_id: {"outcome": r.outcome, "pattern": r.pattern, "exposure": float(r.exposure_usd)} for r in df.itertuples()}


# ---------------------------------------------------------------------------------------------------------------
# sources
def discover_sources() -> list[dict]:
    """Round 1 answers live in round1/ (cases, logs/traces, attestations); Round 2 answers in cases/, logs/traces and
    attestations/. Round 1 comes first so a case present in both places (mid-move) is read from round1/."""
    from kavach.attest import attest_dir
    return [{"dir": ROUND1 / "cases", "traces": ROUND1 / "logs" / "traces", "attest": ROUND1 / "attestations", "round": 1},
            {"dir": CASES_DIR, "traces": LOGS / "traces", "attest": attest_dir(), "round": None}]


def _answers(sources: list[dict]) -> list[tuple[dict, dict, Path]]:
    seen, out = set(), []
    for src in sources:
        d = Path(src["dir"])
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            a = _read_json(f)
            if not isinstance(a, dict) or "case_id" not in a or not isinstance(a.get("case"), dict):
                continue
            if a["case_id"] in seen:
                continue
            seen.add(a["case_id"])
            out.append((a, src, f))
    return out


# ---------------------------------------------------------------------------------------------------------------
# per-case detail
def _intake(row: dict | None) -> dict:
    if not row or row.get("trigger_text") is None:
        return {}
    try:
        from kavach import intake
        it = intake.parse(dict(row))
        return {"hostile": [str(x) for x in it.hostile], "markers": list(it.markers), "instruction": bool(it.instruction),
                "notes": [str(x) for x in it.notes], "template": bool(it.template)}
    except Exception as e:
        return {"error": _err(e)}


def _replay(case_id: str, src: dict) -> dict:
    tp = Path(src["traces"]) / f"{case_id}.json"
    if not tp.exists():
        return {"available": False, "reason": f"no trace on disk ({_rel(tp)})"}
    try:
        from kavach import audit
        d = audit.replay_data(case_id, src["traces"], src["dir"], src["attest"])
    except Exception as e:
        return {"available": False, "reason": f"the replay could not be built from {_rel(tp)}: {_err(e)}"}
    trace = _read_json(tp) or {}
    h = trace.get("harness") or {}
    d.update({"available": True, "trace_path": _rel(tp), "harness_status": h.get("status"),
              "llm_status": h.get("llm_status") or trace.get("llm_status") or [],
              "bounds": h.get("bounds") or {}, "fault_injected": h.get("fault_injected")})
    return d


def _attestation(case_id: str, src: dict) -> tuple[dict, dict | None]:
    p = Path(src["attest"]) / f"{case_id}.json"
    if not p.exists():
        return {"available": False, "reason": f"no attestation sidecar on disk ({_rel(p)})"}, None
    side = _read_json(p)
    if not isinstance(side, dict):
        return {"available": False, "reason": f"{_rel(p)} is not valid JSON"}, None
    dv = side.get("dataset_version")
    version = dv.get("version") if isinstance(dv, dict) else (dv or "")
    claims = []
    for c in side.get("claims") or []:
        ev = {k: v for k, v in (c.get("evidence") or {}).items() if v}
        claims.append({"id": c.get("id", ""), "kind": c.get("kind", ""), "what": c.get("what", ""), "evidence": ev,
                       "how": c.get("how") or {}, "when": c.get("when") or {}, "source": c.get("source", ""),
                       "ref": c.get("ref", ""), "signal": c.get("signal", ""), "llr": c.get("llr"),
                       "supported_by": c.get("supported_by") or []})
    return ({"available": True, "path": _rel(p), "run_at": side.get("run_at", ""), "backend": side.get("backend", ""),
             "dataset_version": version, "counts": side.get("counts") or {}, "claims": claims,
             "data_quality": side.get("data_quality") or []}, side)


def _verify(case_id: str, src: dict, db: _Duck) -> dict:
    if not db.ok:
        return {"available": False, "reason": db.error}
    try:
        from kavach import attest
        rows = attest.verify(case_id, "duck", src["attest"])
    except Exception as e:
        return {"available": False, "reason": f"re-run failed: {_err(e)}"}
    counts = {s: sum(1 for r in rows if r.get("status") == s) for s in ("pass", "fail", "unverifiable")}
    return {"available": True, "at": _now(), "backend": "duck", "dataset_version": db.version(), "counts": counts,
            "rows": [{k: r.get(k) for k in ("id", "status", "reason", "tool", "n_ids", "missing")} for r in rows]}


def _casefile(case_id: str) -> dict:
    try:
        from kavach import casefile
        evs = casefile.events(case_id)
        where = _rel(casefile.path(case_id))
        if not evs:
            return {"available": False, "reason": f"no case file events on disk ({where})"}
        st = casefile.state(case_id)
    except Exception as e:
        return {"available": False, "reason": f"the case file could not be read: {_err(e)}"}
    keep = ("seq", "ts", "kind", "detail", "evidence_ids", "actor", "approval_id", "extra")
    return {"available": True, "path": where, "state": st,
            "events": [{**{k: e.get(k) for k in keep}, "hash": str(e.get("hash", ""))[:12]} for e in evs]}


# ---------------------------------------------------------------------------------------------------------------
# ring replay frames
def _ring_claims(case: dict, sidecar: dict | None) -> list[dict]:
    """Claims naming RING_MIN_CARDS or more cards: the sidecar's (with their recorded query) first, then answer
    evidence items the sidecar does not already carry."""
    out, whats = [], set()
    for c in (sidecar or {}).get("claims") or []:
        cards = list(dict.fromkeys(str(x) for x in (c.get("evidence") or {}).get("card_ids") or [] if CARD_RE.match(str(x))))
        if len(cards) >= RING_MIN_CARDS:
            out.append({"id": c.get("id", ""), "what": c.get("what", ""), "cards": cards, "how": c.get("how") or {},
                        "ref": c.get("ref", "")})
            whats.add(c.get("what", ""))
    for n, e in enumerate(case.get("evidence") or [], 1):
        cards = list(dict.fromkeys(str(x) for x in e.get("entity_ids") or [] if CARD_RE.match(str(x))))
        if len(cards) >= RING_MIN_CARDS and e.get("claim", "") not in whats:
            out.append({"id": f"evidence {n}", "what": e.get("claim", ""), "cards": cards, "how": {}, "ref": e.get("ref", "")})
    return out


def _ts(v):
    import pandas as pd
    t = pd.Timestamp(v)
    return t.tz_localize(None) if t.tzinfo is not None else t


def ring_frames(answer: dict, sidecar: dict | None = None, flagged_txn_id=None, db: _Duck | None = None) -> dict | None:
    """Ring replay frames for a case whose answer names connected cards or whose evidence names a ring (3+ cards):
    every transaction of the case card and the ring cards in the episode window, each with the values it carries
    for device profile, email domains and billing region, built from the DuckDB index. None when there is no ring."""
    from datetime import timedelta

    case = answer.get("case") or {}
    connected = list(dict.fromkeys(str(x) for x in case.get("connected_card_ids") or []))
    claims = _ring_claims(case, sidecar)
    if not connected and not claims:
        return None
    db = db or _Duck()
    if not db.ok:
        return {"available": False, "reason": db.error}

    anchor = flagged_txn_id or case.get("first_suspicious_txn_id") or (case.get("affected_txn_ids") or [None])[0]
    if _blank(anchor) or not str(anchor).isdigit():
        return {"available": False, "reason": "no flagged or affected transaction to anchor the window"}
    arow = db.q("select card_id, ts from txn where TransactionID = ?", int(anchor))
    if not len(arow):
        return {"available": False, "reason": f"transaction {anchor} is not in the index"}
    case_card, t_anchor = str(arow.card_id.iloc[0]), _ts(arow.ts.iloc[0])

    # query window: the recorded query of a ring claim when it has t0 and days, else +-30 days around the alert
    t0, days, wsrc = t_anchor, RING_DAYS, f"{RING_DAYS:g} days either side of flagged transaction {anchor}"
    for cl in claims:
        p = (cl.get("how") or {}).get("params") or {}
        if not _blank(p.get("t0")) and not _blank(p.get("days")):
            try:
                t0, days = _ts(p["t0"]), min(float(p["days"]), 90.0)
                wsrc = f"{days:g} days either side of t0 in the attested query {cl['how'].get('tool')} ({cl['id']})"
                break
            except (ValueError, TypeError):
                continue
    lo, hi = t0 - timedelta(days=days), t0 + timedelta(days=days)

    related = [c for cl in claims for c in cl["cards"] if c not in connected and c != case_card]
    members = list(dict.fromkeys([case_card] + connected + related))
    roles = {c: ("case card" if c == case_card else "connected" if c in connected else "related") for c in members}
    df = db.q(RING_SQL, members, lo.to_pydatetime(), hi.to_pydatetime())

    def val(field, v):
        if _blank(v):
            return None
        return str(int(float(v))) if field == "addr1" else str(v)

    rows = []
    for r in df.itertuples():
        rows.append({"id": str(int(r.TransactionID)), "card": r.card_id, "ts": r.ts, "amt": round(float(r.amt), 2),
                     "product": r.ProductCD, **{f: val(f, getattr(r, f)) for f in SHARED_FIELDS}})

    # values carried by two or more ring cards in the query window
    by_val: dict = {}
    for r in rows:
        for f in SHARED_FIELDS:
            if r[f] is not None:
                by_val.setdefault((f, r[f]), set()).add(r["card"])
    shared = {k: s for k, s in by_val.items() if len(s) >= 2}

    # which of them the agent itself named (connected device profiles, claim text, recorded query parameters)
    named_text = " ".join(cl["what"] for cl in claims) + " " + " ".join(str(x) for x in case.get("connected_device_profiles") or [])
    params = [(str(k), v) for cl in claims for k, v in ((cl.get("how") or {}).get("params") or {}).items()]
    named_params = {v for _, v in params if isinstance(v, str)} | {str(x) for x in case.get("connected_device_profiles") or []}
    regions = set(re.findall(r"(?:region|addr1)\s*=?\s*(\d+)", named_text))
    regions |= {str(int(float(v))) for k, v in params if ("addr" in k or "region" in k) and str(v).replace(".", "", 1).isdigit()}

    field_words = named_text + " " + " ".join(f"{k}={v}" for k, v in params)

    def is_named(f, v):
        """0: not named; 1: the value is named; 2: the value is named together with its field."""
        if f == "addr1":
            return 2 if v in regions else 0
        if not (v in named_params or (len(v) >= 4 and v in named_text)):
            return 0
        if f == "device_key":
            return 2
        return 2 if (f in field_words or re.search(rf"\b{f[0]} email {re.escape(v)}", named_text)) else 1

    # how widely each candidate is used by any card in the window (a domain like gmail.com links nothing)
    popular = {}
    top = sorted(shared.items(), key=lambda kv: -len(kv[1]))[:16]
    for (f, v), _ in top:
        if f == "addr1":
            n = db.q("select count(distinct card_id) n from txn where addr1 = ? and ts between ? and ?",
                     int(v), lo.to_pydatetime(), hi.to_pydatetime()).n.iloc[0]
        else:
            n = db.q(f"select count(distinct card_id) n from txn where {f} = ? and ts between ? and ?",
                     v, lo.to_pydatetime(), hi.to_pydatetime()).n.iloc[0]
        popular[(f, v)] = int(n)
    generic = set()
    try:
        from kavach.detectors.network import generic_device
        for (f, v) in popular:
            if f == "device_key":
                ever = int(db.q("select count(distinct card_id) n from txn where device_key = ?", v).n.iloc[0])
                if generic_device(v, ever):
                    generic.add((f, v))
    except Exception:
        pass

    elements = []
    for (f, v), cards in top:
        pop = popular.get((f, v), len(cards))
        score = is_named(f, v)
        elements.append({"field": f, "label": FIELD_LABEL[f], "value": v, "ring_cards": len(cards),
                         "window_cards": pop, "named": score > 0, "named_score": score, "generic": (f, v) in generic,
                         "concentrated": len(cards) / max(pop, 1) >= RING_CONCENTRATION, "primary": False})
    # the element that links the ring: not a placeholder device, mostly used by ring cards in the window (a domain
    # like hotmail.com on 600 cards links nothing), named by the agent when possible
    order = {f: i for i, f in enumerate(SHARED_FIELDS)}
    cand = [e for e in elements if not e["generic"] and e["concentrated"]
            and e["ring_cards"] >= (2 if e["named"] else RING_MIN_CARDS)]
    cand.sort(key=lambda e: (-e["named_score"], -e["ring_cards"], e["window_cards"], order[e["field"]]))
    primary = cand[0] if cand else None
    if primary:
        primary["primary"] = True

    affected = {str(x) for x in case.get("affected_txn_ids") or []}
    cited = set(affected)
    for e in case.get("evidence") or []:
        cited |= {str(x) for x in e.get("entity_ids") or [] if str(x).isdigit()}
    for c in (sidecar or {}).get("claims") or []:
        cited |= {str(x) for x in (c.get("evidence") or {}).get("txn_ids") or []}
    shared_keys = {(e["field"], e["value"]) for e in elements if not e["generic"] and e["concentrated"]}

    for r in rows:
        r["primary"] = bool(primary and r[primary["field"]] == primary["value"])
        r["shared"] = [f for f in SHARED_FIELDS if r[f] is not None and (f, r[f]) in shared_keys]
        r["affected"], r["flagged"], r["cited"] = r["id"] in affected, r["id"] == str(anchor), r["id"] in cited

    # episode window: the span of the ring's own activity (primary element, else cited transactions), padded
    marks = [r for r in rows if r["primary"]] or [r for r in rows if r["cited"] or r["flagged"]]
    if marks:
        e_lo = max(lo, _ts(marks[0]["ts"]) - timedelta(days=RING_PAD_DAYS))
        e_hi = min(hi, _ts(marks[-1]["ts"]) + timedelta(days=RING_PAD_DAYS))
    else:
        e_lo, e_hi = lo, hi
    rows = [r for r in rows if e_lo <= _ts(r["ts"]) <= e_hi]
    omitted = 0
    if len(rows) > RING_MAX_ROWS:
        keep = [r for r in rows if r["primary"] or r["shared"] or r["cited"] or r["flagged"] or r["card"] == case_card]
        omitted = len(rows) - len(keep)
        rows = keep

    mem = []
    for c in members:
        mine = [r for r in rows if r["card"] == c]
        via = [r for r in mine if r["primary"]] if primary else [r for r in mine if r["cited"] or r["flagged"]]
        first = (via or mine or [None])[0]
        mem.append({"card": c, "role": roles[c], "n_txns": len(mine), "n_primary": sum(1 for r in mine if r["primary"]),
                    "amt_primary": round(sum(r["amt"] for r in mine if r["primary"]), 2),
                    "joined_ts": first["ts"] if first else None, "joined_txn": first["id"] if first else None,
                    "joined_via": ("primary" if via and primary else "cited" if via else "first" if first else None),
                    "named_in": [cl["id"] for cl in claims if c in cl["cards"]]})
    mem.sort(key=lambda m: (m["joined_ts"] is None, m["joined_ts"] or "", m["role"] != "case card"))

    return {"available": True, "case_card": case_card, "anchor_txn": str(anchor), "members": mem,
            "elements": sorted(elements, key=lambda e: (not e["primary"], not e["named"], not e["concentrated"],
                                                        -e["ring_cards"]))[:8],
            "primary": primary, "claims": [{"id": cl["id"], "what": cl["what"], "how": cl.get("how") or {}, "ref": cl["ref"]}
                                           for cl in claims],
            "window": {"from": e_lo.strftime("%Y-%m-%d %H:%M:%S"), "to": e_hi.strftime("%Y-%m-%d %H:%M:%S"),
                       "query_from": lo.strftime("%Y-%m-%d %H:%M:%S"), "query_to": hi.strftime("%Y-%m-%d %H:%M:%S"),
                       "source": wsrc},
            "txns": rows, "omitted": omitted, "exposure_usd": case.get("exposure_usd"),
            "built": {"sql": RING_SQL, "params": {"cards": members, "from": lo.strftime("%Y-%m-%d %H:%M:%S"),
                                                  "to": hi.strftime("%Y-%m-%d %H:%M:%S")},
                      "dataset_version": db.version(), "at": _now(), "rerun": f"python -m kavach.site ring {answer['case_id']}"}}


# ---------------------------------------------------------------------------------------------------------------
# monitor findings and detectors
def _monitor() -> list[dict]:
    out, seen = [], set()
    for d in (ROUND1 / "monitor", ROOT / "monitor"):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.json")):
            a = _read_json(f)
            if not isinstance(a, dict) or "monitor" not in a:
                continue
            m, k = a["monitor"], a["case"]
            key = (a["case_id"], k.get("summary", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append({"case_id": a["case_id"], "sweep": m.get("sweep", ""), "card": m.get("card_id", ""),
                        "why": m.get("trigger_text", ""), "verdict": k.get("verdict"), "p": k.get("fraud_probability"),
                        "pattern": k.get("pattern"), "exposure": k.get("exposure_usd"), "sar": (a.get("sar") or {}).get("file"),
                        "summary": k.get("summary", ""), "path": _rel(f)})
    return out


def _gate_reasons() -> dict:
    """registry.load_approved's own verdict per approved file: passes, or the reason it is refused at load time."""
    try:
        from kavach.grow import registry
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = {s.get("name") for s in registry.load_approved(verbose=True)}
        refused = {}
        for line in buf.getvalue().splitlines():
            m = re.match(r"\s*grown detector (\S+) refused: (.*)$", line)
            if m:
                refused[m.group(1).removesuffix(".json")] = m.group(2)
        return {"ok": ok, "refused": refused}
    except Exception as e:
        return {"error": _err(e)}


def _spec(s: dict, path: Path, kind: str, gate: dict) -> dict:
    bt = dict(s.get("backtest") or {})
    dv = s.get("dataset_version")
    ap = s.get("approval") or None
    out = {"name": s.get("name", path.stem), "title": s.get("title", ""), "family": s.get("family", ""),
           "params": s.get("params") or {}, "rule": s.get("rule", ""), "description": s.get("description", ""),
           "description_source": s.get("description_source", ""), "status": s.get("status", kind), "kind": kind,
           "proposed_at": s.get("proposed_at", ""), "dataset_version": dv.get("version") if isinstance(dv, dict) else (dv or ""),
           "approval": ap, "backtest": bt, "path": _rel(path)}
    if kind == "approved":
        if "error" in gate:
            out["gate"] = {"passes": None, "reason": f"load gate not checked: {gate['error']}"}
        elif out["name"] in gate.get("ok", set()):
            out["gate"] = {"passes": True, "reason": "L2 approval on record and the spec still hashes to the approved sha"}
        else:
            out["gate"] = {"passes": False, "reason": gate.get("refused", {}).get(out["name"], "refused at load time")}
    return out


def _typology(b: dict, path: Path) -> dict:
    hits = b.get("hits") or {}
    fraud, cleared = hits.get("fraud") or [], hits.get("cleared") or []
    jan = b.get("january") or {}
    return {"name": b.get("name", path.stem), "run_at": b.get("run_at", ""), "dataset_version": b.get("dataset_version", ""),
            "n_closed": b.get("n_closed"), "n_fraud": b.get("n_fraud"), "n_cleared": b.get("n_cleared"),
            "population": b.get("population", ""), "n_population": b.get("n_population"), "chosen": b.get("chosen") or {},
            "n_grid": len(b.get("results") or []), "fraud_hits": len(fraud),
            "fraud_case_ids": [h.get("case_id") for h in fraud][:40],
            "cleared": [{"case_id": h.get("case_id"), "txn": h.get("txn"), "card": h.get("card")} for h in cleared],
            "population_hits": len(hits.get("population_sample") or []),
            "january": {"fired_txns": jan.get("fired_txns"), "fired_cards": jan.get("fired_cards"),
                        "cards": (jan.get("cards") or [])[:20]} if jan else {},
            "errors": b.get("errors") or {}, "seconds": b.get("seconds"), "tool_calls": b.get("tool_calls"), "path": _rel(path)}


def _detectors() -> dict:
    base = ROOT / "detectors"
    gate = _gate_reasons()
    out = {"approved": [], "proposed": [], "rejected": [], "typologies": []}
    if "error" in gate:
        out["gate_error"] = gate["error"]
    for kind in ("approved", "proposed", "rejected"):
        for p in sorted((base / kind).glob("*.json")) if (base / kind).exists() else []:
            s = _read_json(p)
            if isinstance(s, dict):
                out[kind].append(_spec(s, p, kind, gate))
    for p in sorted((base / "backtests").glob("*.json")) if (base / "backtests").exists() else []:
        b = _read_json(p)
        if isinstance(b, dict) and "chosen" in b:
            out["typologies"].append(_typology(b, p))
    return out


# ---------------------------------------------------------------------------------------------------------------
def _js(name: str, data) -> str:
    return f"window.{name} = " + json.dumps(data, ensure_ascii=False, default=str) + ";\n"


def build(out: Path | str | None = None, sources: list[dict] | None = None, verify: bool = True, quiet: bool = False) -> dict:
    """Write <out>/cases.js and <out>/data/<case>.js. Re-runnable: detail files of cases that no longer exist are
    removed. Returns a summary {cases, rounds, details, errors}."""
    out = Path(out) if out else SITE
    sources = sources if sources is not None else discover_sources()
    db = _Duck()
    pack = _pack_rows(db)
    errors = [db.error] if db.error else []
    cases, details = [], {}
    for a, src, f in _answers(sources):
        cid, c = a["case_id"], a["case"]
        row = pack.get(cid)
        rnd = int(row["round"]) if row and not _blank(row.get("round")) else (src.get("round") or 2)
        flagged = str(int(float(row["flagged_txn_id"]))) if row and not _blank(row.get("flagged_txn_id")) else ""
        a["_round"], a["_src"] = rnd, _rel(f)
        a["_pack"] = ({"trigger_type": row.get("trigger_type"), "trigger_text": row.get("trigger_text"),
                       "opened_at": str(row.get("opened_at") or ""), "card_id": row.get("card_id"),
                       "customer_id": row.get("customer_id"), "flagged_txn_id": flagged} if row else {})
        a["_intake"] = _intake(row)
        try:
            a["_txns"] = _txn_rows(db, sorted(set(c.get("affected_txn_ids") or []) | ({flagged} if flagged else set())))
            a["_closed"] = _closed(db, list(c.get("similar_prior_cases") or []))
        except Exception as e:
            a["_txns"], a["_closed"] = {}, {}
            errors.append(f"{cid}: transaction lookup failed: {_err(e)}")

        replay = _replay(cid, src)
        att, side = _attestation(cid, src)
        ver = _verify(cid, src, db) if (verify and att["available"]) else (
            {"available": False, "reason": "re-run skipped (--no-verify)"} if att["available"] else
            {"available": False, "reason": att["reason"]})
        cf = _casefile(cid)
        try:
            ring = ring_frames(a, side, flagged or None, db)
        except Exception as e:
            ring = {"available": False, "reason": f"ring frames could not be built: {_err(e)}"}
        details[cid] = {"case_id": cid, "replay": replay, "attestation": att, "verify": ver, "casefile": cf, "ring": ring}
        # which sidecar claim attests each evidence row of the answer, and how its re-run went
        by_what = {x["what"]: x["id"] for x in att.get("claims") or [] if x["kind"] == "evidence"}
        status = {r["id"]: r["status"] for r in ver.get("rows") or []}
        a["_evidence_claims"] = [({"id": by_what[e.get("claim")], "status": status.get(by_what[e.get("claim")])}
                                  if e.get("claim") in by_what else None) for e in c.get("evidence") or []]
        a["_avail"] = {
            "replay": replay["available"], "steps": replay.get("n_tool_calls") if replay["available"] else None,
            "attestation": att["available"], "claims": len(att.get("claims") or []) if att["available"] else None,
            "verify": ver.get("counts") if ver.get("available") else None,
            "casefile": cf["available"], "events": len(cf.get("events") or []) if cf["available"] else None,
            "pending": len((cf.get("state") or {}).get("pending_approvals") or []) if cf["available"] else None,
            "ring": None if ring is None else bool(ring.get("available")),
            "ring_cards": len(ring.get("members") or []) if ring and ring.get("available") else None}
        cases.append(a)

    rounds = {}
    for a in cases:
        r = rounds.setdefault(a["_round"], {"round": a["_round"], "prefixes": [], "n": 0, "verdicts": {}})
        r["n"] += 1
        p = a["case_id"].split("-")[0]
        if p not in r["prefixes"]:
            r["prefixes"].append(p)
        v = a["case"].get("verdict")
        r["verdicts"][v] = r["verdicts"].get(v, 0) + 1
    meta = {"built_at": _now(), "dataset_version": db.version(), "errors": errors,
            "sources": [{k: _rel(v) if isinstance(v, Path) else v for k, v in s.items()} for s in sources],
            "verify": verify}
    data = {"meta": meta, "rounds": sorted(rounds.values(), key=lambda r: -r["round"]),
            "cases": sorted(cases, key=lambda a: (-a["_round"], a["case_id"])), "monitor": _monitor(),
            "detectors": _detectors()}

    out.mkdir(parents=True, exist_ok=True)
    dd = out / "data"
    dd.mkdir(exist_ok=True)
    for old in dd.glob("*.js"):
        if DETAIL_RE.match(old.name) and old.stem not in details:
            old.unlink()
    for cid, d in details.items():
        body = "window.KAVACH_DETAIL = window.KAVACH_DETAIL || {};\n" + \
               f"window.KAVACH_DETAIL[{json.dumps(cid)}] = " + json.dumps(d, ensure_ascii=False, default=str) + ";\n"
        (dd / f"{cid}.js").write_text(body, encoding="utf-8")
    (out / "cases.js").write_text(_js("KAVACH", data), encoding="utf-8")

    summary = {"cases": len(cases), "rounds": {r["round"]: r["n"] for r in data["rounds"]}, "details": len(details),
               "replays": sum(1 for d in details.values() if d["replay"]["available"]),
               "attestations": sum(1 for d in details.values() if d["attestation"]["available"]),
               "casefiles": sum(1 for d in details.values() if d["casefile"]["available"]),
               "rings": sum(1 for d in details.values() if d["ring"] and d["ring"].get("available")),
               "monitor": len(data["monitor"]), "approved_detectors": len(data["detectors"]["approved"]),
               "errors": errors}
    if not quiet:
        print(f"{_rel(out / 'cases.js')}: {summary['cases']} cases "
              f"({', '.join(f'round {k}: {v}' for k, v in summary['rounds'].items()) or 'none'}), "
              f"{summary['monitor']} monitor findings, {summary['approved_detectors']} approved grown detector(s)")
        print(f"{_rel(dd)}/: {summary['details']} detail files; replay {summary['replays']}, attestation "
              f"{summary['attestations']}, case file {summary['casefiles']}, ring replay {summary['rings']}")
        for e in errors:
            print(f"  note: {e}")
    return summary


# ---------------------------------------------------------------------------------------------------------------
def _ring_cli(case_id: str, as_json: bool) -> int:
    from kavach.attest import attest_dir
    src = None
    for s in discover_sources():
        if (Path(s["dir"]) / f"{case_id}.json").exists():
            src = s
            break
    if src is None:
        print(f"no answer file for {case_id} in round1/cases or cases/")
        return 1
    a = _read_json(Path(src["dir"]) / f"{case_id}.json")
    side = _read_json(Path(src["attest"] or attest_dir()) / f"{case_id}.json")
    db = _Duck()
    pack = _pack_rows(db).get(case_id) or {}
    flagged = str(int(float(pack["flagged_txn_id"]))) if not _blank(pack.get("flagged_txn_id")) else None
    r = ring_frames(a, side, flagged, db)
    if r is None:
        print(f"{case_id}: no ring (no connected cards and no evidence claim naming {RING_MIN_CARDS}+ cards)")
        return 0
    if as_json:
        print(json.dumps(r, indent=2, default=str))
        return 0 if r.get("available") else 1
    if not r.get("available"):
        print(f"{case_id}: ring frames unavailable: {r.get('reason')}")
        return 1
    p = r["primary"]
    print(f"{case_id}: {len(r['members'])} cards, {len(r['txns'])} transactions {r['window']['from']} to {r['window']['to']} "
          f"(window {r['window']['source']}); dataset {r['built']['dataset_version']}")
    print(f"shared element: {p['label']} {p['value']} on {p['ring_cards']} ring cards ({p['window_cards']} cards in the window)"
          if p else "shared element: none of device profile, email domains or billing region links the ring cards")
    for m in r["members"]:
        print(f"  {m['joined_ts'] or '-':19s} {m['card']:11s} {m['role']:9s} txns {m['n_txns']:>4d} via shared {m['n_primary']:>3d} "
              f"${m['amt_primary']:>10,.2f}  first {m['joined_txn'] or '-'}")
    print(f"query: {r['built']['sql']}")
    print(f"params: {json.dumps(r['built']['params'])}")
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m kavach.site", description="Static viewer data export.")
    ap.add_argument("cmd", nargs="?", default="build", choices=("build", "ring"))
    ap.add_argument("case", nargs="?", default="")
    ap.add_argument("--no-verify", action="store_true", help="skip re-running the attested claims at export time")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if a.cmd == "ring":
        if not a.case:
            ap.error("ring needs a case id")
        return _ring_cli(a.case, a.json)
    build(verify=not a.no_verify)
    return 0


if __name__ == "__main__":
    sys.exit(main())
