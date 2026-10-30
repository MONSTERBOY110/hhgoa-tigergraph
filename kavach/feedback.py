"""Analyst in the loop (brief challenge 8): accept or reject a finding, and behaviour that changes from those decisions
without retraining a model.

    record(case_id, finding, decision, token, reason, scope="context")   one analyst decision, append-only
    adjust(signals, ctx) -> signals                                       applied after detection (detectors.run_all)
    records() / verify_detail() / verify()                                the log and its hash chain
    python -m kavach.feedback record|list|verify|eval ...

A finding is a signal name the agent raised on a case (a detector such as "device_ring", or a scored fact such as
"risk_score_untrusted"). The decision is "accept" or "reject".

Who may decide. The role comes only from the bearer token, resolved by kavach.harness.approvals.role_for (the same
secret tokens and the same server-side check as the approvals). Scope "element" and "context" need role analyst or
above; scope "channel" changes behaviour across every product of a channel and needs L1 or above. An unknown token or a
role that is too low appends a "denied" record and raises Forbidden.

Where it is kept. Its own append-only, hash-chained log, data/feedback.jsonl (env KAVACH_FEEDBACK_FILE), with the same
canonical JSON, sha256 chain and file lock as data/approvals.jsonl. Feedback is not a policy action with a route, so it
does not go in the approvals log. There is no update and no delete: a change of mind is a new record (the latest
decision of each approver on a key wins). verify() re-walks the chain; adjust() refuses to run on a broken chain
(FeedbackIntegrityError: the investigation fails closed with an incident rather than using tampered feedback).

What changes, and how much. No model is retrained. A record carries a context key computed from the case it was made
on: the detector name, the alert trigger, and the flagged transaction's channel and product code (scope "context");
scope "element" adds the shared value the finding is about (the device profile, email domain, region or amount in the
signal's query parameters); scope "channel" drops the product code. adjust() looks at every signal of a new
investigation; when its key matches, the net decision of the distinct approvers decides:
    more rejects than accepts   llr x REJECT_FACTOR (0.25): the finding still counts, at a quarter of its weight
    more accepts than rejects   llr x min(1 + ACCEPT_STEP x net, ACCEPT_CAP) = x1.1, capped at x1.2
    a tie                       unchanged
The sign never flips and a zero-weight signal is never touched. Every adjusted signal says so in its claim and carries
`feedback = {"records": [ids], "factor", "llr_before", "llr_after", "key"}`, so the attestation shows which analyst
decisions changed the behaviour. Why the trigger is part of the key: the same finding means different things on a model
alert (the customer has said nothing) and on a customer dispute (R1 and R2 are different rules).
"""
import argparse
import dataclasses
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kavach.config import DATA
from kavach.harness import approvals
from kavach.harness.faults import Forbidden, HarnessError

PATH = DATA / "feedback.jsonl"
DECISIONS = ("accept", "reject")
SCOPES = ("element", "context", "channel")
SCOPE_ROLE = {"element": "analyst", "context": "analyst", "channel": "L1"}
REJECT_FACTOR = 0.25
ACCEPT_STEP = 0.1
ACCEPT_CAP = 1.2
# query parameters that name the shared element a finding is about (first present wins)
ELEMENT_PARAMS = ("device_key", "email", "r_email", "p_email", "value", "addr1", "region", "amt")


class FeedbackIntegrityError(HarnessError):
    kind = "feedback_integrity"


def path() -> Path:
    p = os.environ.get("KAVACH_FEEDBACK_FILE", "").strip()
    return Path(p) if p else Path(PATH)


def enabled() -> bool:
    return os.environ.get("KAVACH_FEEDBACK", "1").strip() != "0"


# ---------------------------------------------------------------- keys
def _s(v) -> str:
    if v is None:
        return ""
    try:
        import math
        if isinstance(v, float) and math.isnan(v):
            return ""
    except TypeError:
        pass
    return str(v)


def context_of(trigger, product, channel) -> dict:
    return {"trigger": _s(trigger), "product": _s(product), "channel": _s(channel)}


def ctx_context(ctx) -> dict:
    """The context of a running investigation (kavach.context.CaseContext)."""
    f = getattr(ctx, "f", {}) or {}
    return context_of(ctx.case.get("trigger_type"), f.get("ProductCD"), f.get("channel"))


def _get(sig, k, default=None):
    return sig.get(k, default) if isinstance(sig, dict) else getattr(sig, k, default)


def element_of(sig) -> str:
    """The shared value a finding is about, read from its re-runnable query parameters (attestation `how`)."""
    params = (_get(sig, "how") or {}).get("params") or {}
    for k in ELEMENT_PARAMS:
        if params.get(k) not in (None, ""):
            return f"{k}={_s(params[k])}"
    return ""


def key_for(sig, context: dict, scope: str) -> dict:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    key = {"detector": _get(sig, "name"), "trigger": context["trigger"], "channel": context["channel"]}
    if scope in ("element", "context"):
        key["product"] = context["product"]
    if scope == "element":
        el = element_of(sig)
        if not el:
            raise ValueError(f"finding {key['detector']} names no shared element in its query parameters; use scope 'context'")
        key["element"] = el
    return key


def matches(rec: dict, sig, context: dict) -> bool:
    k = rec.get("key") or {}
    if k.get("detector") != _get(sig, "name"):
        return False
    if k.get("trigger") != context["trigger"] or k.get("channel") != context["channel"]:
        return False
    if "product" in k and k["product"] != context["product"]:
        return False
    if "element" in k and k["element"] != element_of(sig):
        return False
    return True


# ---------------------------------------------------------------- log
def _lines(p: Path) -> list[str]:
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _append(rec: dict) -> dict:
    p = path()
    with approvals._locked(p):
        lines = _lines(p)
        rec["prev_hash"] = json.loads(lines[-1])["hash"] if lines else approvals.GENESIS
        rec["hash"] = approvals.digest(rec)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return rec


def records() -> list[dict]:
    return [json.loads(ln) for ln in _lines(path())]


def verify_detail() -> dict:
    prev, n = approvals.GENESIS, 0
    for i, ln in enumerate(_lines(path()), 1):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            return {"ok": False, "records": n, "first_bad": i, "reason": "line is not valid JSON"}
        if rec.get("prev_hash") != prev:
            return {"ok": False, "records": n, "first_bad": i, "reason": "prev_hash does not match the previous record"}
        if approvals.digest(rec) != rec.get("hash"):
            return {"ok": False, "records": n, "first_bad": i, "reason": "record content does not match its hash"}
        prev, n = rec["hash"], n + 1
    return {"ok": True, "records": n, "first_bad": None, "reason": ""}


def verify() -> tuple[bool, int]:
    d = verify_detail()
    return d["ok"], d["records"]


_cache: dict = {}


def active_records() -> list[dict]:
    """Recorded (not denied) decisions, after verifying the chain. Cached on the file's size and mtime."""
    p = path()
    if not p.exists():
        return []
    st = p.stat()
    sig = (str(p), st.st_size, st.st_mtime_ns)
    if _cache.get("sig") == sig:
        return _cache["recs"]
    d = verify_detail()
    if not d["ok"]:
        raise FeedbackIntegrityError(f"feedback log {p.name} fails its hash chain at line {d['first_bad']} ({d['reason']}); "
                                     "analyst feedback cannot be trusted, so the investigation stops")
    recs = [r for r in records() if r.get("status") == "recorded" and r.get("decision") in DECISIONS]
    _cache.update(sig=sig, recs=recs)
    return recs


# ---------------------------------------------------------------- record
def _dataset_version() -> str:
    try:
        from kavach import duckq
        return duckq.dataset_version().get("version", "unknown")
    except Exception:  # noqa: BLE001  (a record is still useful with an unknown version; it says so)
        return "unknown"


def case_signals(case_id: str) -> tuple[list[dict], dict, dict]:
    """Re-run the case (no LLM prose, nothing written) to get the signals it raises now and its context.
    Returns (signals as dicts, context, case row)."""
    from kavach import duckq
    from kavach.harness.runner import run_case
    rows = duckq.q("select * from cases where case_id = ?", case_id)
    if not len(rows):
        raise ValueError(f"case {case_id} is not in the case packs of this index")
    row = {k: (None if _s(v) == "" else v) for k, v in rows.iloc[0].to_dict().items()}
    keep = {k: os.environ.get(k) for k in ("KAVACH_NO_LLM", "KAVACH_RECORD", "KAVACH_CASEFILE_EVENTS")}
    os.environ.update({"KAVACH_NO_LLM": "1", "KAVACH_RECORD": "0", "KAVACH_CASEFILE_EVENTS": "0"})
    try:
        out = run_case(row, quiet=True)
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    if out.answer is None:
        raise RuntimeError(f"{case_id}: the investigation failed ({(out.incident or {}).get('error')}); no feedback recorded")
    det = duckq.txn_detail(int(float(row["flagged_txn_id"])))
    ctx = context_of(row.get("trigger_type"), det.get("ProductCD"), det.get("channel"))
    return list(out.trace.get("signals") or []), ctx, row


def record(case_id: str, finding: str, decision: str, token: str | None, reason: str, scope: str = "context",
           signals: list | None = None, context: dict | None = None, flagged_txn_id=None) -> dict:
    """Record one analyst decision on a finding of a case. The role comes from the token (server side). `signals` and
    `context` (trigger, product, channel) may be given by the caller that already holds the case; otherwise the case is
    re-run without prose or writes to get them. Returns the appended record; raises Forbidden after appending a denied
    record when the role is too low or the token unknown; raises ValueError for a bad decision, scope or finding."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if not case_id or len(case_id) > 64:
        raise ValueError("invalid case id")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required: the next analyst must see why the finding was accepted or rejected")
    role = approvals.role_for(token)
    need = SCOPE_ROLE[scope]
    base = {"id": "FB-" + uuid.uuid4().hex[:12], "case_id": case_id, "finding": finding, "decision": decision, "scope": scope,
            "role": role or "none", "approver": approvals.approver_id(token),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason[:500]}
    if role is None or approvals.RANK.get(role, 0) < approvals.RANK[need]:
        why = (f"unknown or unconfigured token; scope {scope} needs role {need} or above" if role is None else
               f"role {role} cannot record scope {scope} feedback; it needs role {need} or above")
        rec = _append({**base, "status": "denied", "denied_reason": why, "key": {}, "signal": {}})
        raise Forbidden(f"{case_id} {finding}: {why} (denied record {rec['id']})")
    if signals is None or context is None:
        got_sigs, got_ctx, row = case_signals(case_id)
        signals = got_sigs if signals is None else signals
        context = got_ctx if context is None else context
        flagged_txn_id = flagged_txn_id or row.get("flagged_txn_id")
    hits = [s for s in signals if _get(s, "name") == finding]
    if not hits:
        names = sorted({_get(s, "name") for s in signals})
        raise ValueError(f"finding {finding!r} was not raised on case {case_id}; its findings are {names}")
    sig = max(hits, key=lambda s: abs(float(_get(s, "llr") or 0.0)))
    key = key_for(sig, context, scope)
    snap = {"group": _get(sig, "group"), "llr": _get(sig, "llr"), "claim": str(_get(sig, "claim") or "")[:400],
            "entity_ids": [str(x) for x in (_get(sig, "entity_ids") or [])][:20], "how": _get(sig, "how") or {}}
    rec = {**base, "status": "recorded", "key": key, "signal": snap, "context": context,
           "flagged_txn_id": _s(flagged_txn_id), "dataset_version": _dataset_version()}
    return _append(rec)


# ---------------------------------------------------------------- adjust
def _net(hits: list[dict]) -> tuple[int, list[str]]:
    """Net decision over distinct approvers (each approver's latest decision on the key counts once)."""
    latest: dict = {}
    for r in sorted(hits, key=lambda r: r.get("ts", "")):
        latest[r.get("approver")] = r
    net = sum(1 if r["decision"] == "accept" else -1 for r in latest.values())
    return net, [r["id"] for r in latest.values()]


def factor_for(net: int) -> float:
    if net < 0:
        return REJECT_FACTOR
    if net > 0:
        return min(1.0 + ACCEPT_STEP * net, ACCEPT_CAP)
    return 1.0


def adjust(signals, ctx, context: dict | None = None, force: bool = False) -> list:
    """Apply the recorded analyst decisions to the signals of a new investigation. Returns a new list; the inputs are
    not mutated. KAVACH_FEEDBACK=0 turns it off (force=True ignores that, for the evaluation's probe). Raises
    FeedbackIntegrityError when the log's chain is broken."""
    sigs = list(signals)
    if not enabled() and not force:
        return sigs
    recs = active_records()
    if not recs:
        return sigs
    cx = context or ctx_context(ctx)
    out = []
    for s in sigs:
        llr = float(_get(s, "llr") or 0.0)
        hits = [r for r in recs if llr != 0.0 and matches(r, s, cx)]
        if not hits:
            out.append(s)
            continue
        net, ids = _net(hits)
        fac = factor_for(net)
        if fac == 1.0:
            out.append(s)
            continue
        new = round(llr * fac, 4)
        how = "down-weighted" if fac < 1 else "up-weighted"
        note = f" [analyst feedback {', '.join(ids)}: {how} x{fac:g}, weight {llr:+.2f} -> {new:+.2f}]"
        meta = {"records": ids, "factor": fac, "llr_before": llr, "llr_after": new, "net": net,
                "key": dict(hits[-1].get("key") or {})}
        if isinstance(s, dict):
            s2 = {**s, "llr": new, "claim": str(s.get("claim", "")) + note, "feedback": meta}
        else:
            s2 = dataclasses.replace(s, llr=new, claim=s.claim + note)
            s2.feedback = meta
        out.append(s2)
    return out


def changed(signals) -> list[dict]:
    """The feedback metadata of the adjusted signals (for traces and reports)."""
    return [{"signal": _get(s, "name"), **_get(s, "feedback")} for s in signals if _get(s, "feedback")]


# ---------------------------------------------------------------- before-and-after evaluation
HARM = ("BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT")      # scores zero on a legitimate case (brief)
ACTED = HARM + ("DECLINE_TRANSACTION",)                      # also refuses the customer's purchase


def _eval_path(label: str, shard) -> Path:
    from kavach.config import LOGS
    return LOGS / "feedback" / f"{label}_{shard}.jsonl"


def truth_round1() -> list[dict]:
    """Round 1 pack rows with the bank's real outcome: the closed record on the same card, customer and opened_at
    (the Round 2 history holds the Round 1 investigations as ordinary closed cases)."""
    from kavach import duckq
    df = duckq.q("""select c.*, h.case_id truth_case, h.outcome truth_outcome, h.pattern truth_pattern, h.report_filed truth_report
                    from cases c join closed h on h.card_id = c.card_id and h.customer_id = c.customer_id
                         and cast(h.opened_at as timestamp) = cast(c.opened_at as timestamp)
                    where c.round = 1 order by c.case_id""")
    out = []
    for r in df.to_dict("records"):
        t = {"case": r.pop("truth_case"), "outcome": r.pop("truth_outcome"), "pattern": r.pop("truth_pattern"),
             "report": r.pop("truth_report")}
        row = {k: (None if _s(v) == "" else v) for k, v in r.items()}
        out.append({"set": "round1", "row": row, "truth": t})
    return out


def replay_rows(since: str = "2016-09-01", until: str = "2016-11-01", n_fraud: int = 260, seed: str = "kavach") -> list[dict]:
    """Closed cases replayed as alerts, with their real outcome. Cleared records were model alerts the cardholder
    confirmed (every one carries its flagged transaction): replayed as risk_score alerts on that transaction. Confirmed
    frauds were customer reports: replayed as customer_report alerts on their latest episode transaction. All cleared
    records of the window and a seeded sample of n_fraud frauds. The detectors only read closed cases opened before the
    alert, so a replayed case never sees its own record."""
    from kavach import duckq
    df = duckq.q("""
      with c as (select * from closed where cast(opened_at as timestamp) >= cast(? as timestamp)
                   and cast(opened_at as timestamp) < cast(? as timestamp) and txn_ids is not null and txn_ids <> ''),
      ids as (select c.case_id, cast(unnest(string_split(c.txn_ids, '|')) as bigint) tid from c),
      last as (select ids.case_id, arg_max(ids.tid, t.ts) tid from ids join txn t on t.TransactionID = ids.tid group by 1),
      pick as (select * from c where outcome = 'cleared' union all
               (select * from c where outcome = 'confirmed_fraud' order by hash(case_id || ?) limit ?))
      select p.case_id, p.customer_id, p.card_id, p.opened_at, p.outcome, p.pattern, p.report_filed, l.tid, t.risk_score,
             t.TransactionAmt amt, t.addr1, t.channel
      from pick p join last l using (case_id) join txn t on t.TransactionID = l.tid order by p.case_id""",
                 since, until, seed, int(n_fraud))
    out = []
    for r in df.itertuples():
        if r.outcome == "cleared":
            where = "online" if r.channel == "online" else f"in billing region {r.addr1}"
            row = {"trigger_type": "risk_score", "risk_score": f"{r.risk_score:.2f}",
                   "trigger_text": f"Real-time model scored transaction {r.tid} (${r.amt:.2f}, {where}) at {r.risk_score:.2f}. Review and decide."}
        else:
            row = {"trigger_type": "customer_report", "risk_score": None,
                   "trigger_text": f"Customer {r.customer_id} message: 'I do not recognise this ${r.amt:.2f} purchase.' Refers to {r.tid}."}
        row.update({"case_id": "RP-" + r.case_id.split("-", 1)[1], "opened_at": str(r.opened_at), "flagged_txn_id": str(r.tid),
                    "card_id": r.card_id, "customer_id": r.customer_id})
        out.append({"set": "replay", "row": row,
                    "truth": {"case": r.case_id, "outcome": r.outcome, "pattern": r.pattern, "report": r.report_filed}})
    return out


def hhf_rows() -> list[dict]:
    from kavach import duckq
    return [{"set": "round2", "row": {k: (None if _s(v) == "" else v) for k, v in r.items()}, "truth": None}
            for r in duckq.case_pack(2).to_dict("records")]


_probe: dict = {}


def _install_probe():
    """Wrap kavach.agent.run_all: always compute what the feedback WOULD change (the probe), and apply it when
    KAVACH_FEEDBACK is on and detectors.run_all does not already apply it (before the lead's integration)."""
    import inspect

    from kavach import agent, detectors
    if getattr(agent.run_all, "_kavach_feedback_probe", False):
        return
    orig = agent.run_all
    integrated = "feedback.adjust" in inspect.getsource(detectors.run_all)

    def wrapped(ctx):
        sigs, fnd = orig(ctx)
        raw = [dataclasses.replace(s) for s in sigs]
        for s, r in zip(sigs, raw):
            if hasattr(s, "feedback"):     # already adjusted inside run_all: probe from the pre-feedback weight
                r.llr, r.claim = s.feedback["llr_before"], s.claim.split(" [analyst feedback")[0]
        _probe["changes"] = changed(adjust(raw, ctx, force=True))
        if enabled() and not integrated:
            sigs = adjust(sigs, ctx)
        return sigs, fnd

    wrapped._kavach_feedback_probe = True
    agent.run_all = wrapped


def _summ(out) -> dict:
    if out.answer is None:
        return {"incident": (out.incident or {}).get("error", "")[:300]}
    a = out.answer
    c = a["case"]
    fb = [s for s in (out.trace.get("signals") or []) if s.get("feedback")]
    return {"verdict": c["verdict"], "p": c["fraud_probability"], "pattern": c["pattern"],
            "final": [x["action"] for x in a["next_best_actions"]["final"]], "sar": a["sar"]["file"],
            "affected": list(c["affected_txn_ids"]), "connected": list(c["connected_card_ids"]),
            "feedback": [{"signal": s["name"], **s["feedback"]} for s in fb]}


def evaluate(source_case: str, label: str = "eval", shard: int = 0, shards: int = 1, n_fraud: int = 260,
             replays: bool = True, hhf: bool = False, resume: bool = False) -> int:
    """One shard of the before-and-after run. Every case runs once without feedback (KAVACH_FEEDBACK=0); a case on
    which the recorded decisions would change at least one signal (the probe) runs again with feedback. A case with no
    matching signal gets identical signals in both arms by construction, so its "after" is its "before"."""
    os.environ.update({"KAVACH_NO_LLM": "1", "KAVACH_RECORD": "0", "KAVACH_CASEFILE_EVENTS": "0"})
    from kavach.config import LOGS
    os.environ.setdefault("KAVACH_INCIDENTS_DIR", str(LOGS / "feedback" / "incidents"))
    from kavach.harness.runner import run_case
    _install_probe()
    active_records()          # verify the chain up front: a broken log stops the evaluation
    items = truth_round1() + (replay_rows(n_fraud=n_fraud) if replays else []) + (hhf_rows() if hhf else [])
    p = _eval_path(label, shard)
    p.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if resume and p.exists():
        done = {json.loads(ln)["case_id"] for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}
    n = 0
    with open(p, "a" if resume else "w", encoding="utf-8") as fh:
        for i, it in enumerate(items):
            if i % shards != shard or it["row"]["case_id"] in done:
                continue
            row = it["row"]
            os.environ["KAVACH_FEEDBACK"] = "0"
            _probe.clear()
            before = _summ(run_case(dict(row), quiet=True))
            probe = list(_probe.get("changes") or [])
            after = before
            if probe:
                os.environ["KAVACH_FEEDBACK"] = "1"
                after = _summ(run_case(dict(row), quiet=True))
            os.environ["KAVACH_FEEDBACK"] = "1"
            fh.write(json.dumps({"set": it["set"], "case_id": row["case_id"], "source": row["case_id"] == source_case,
                                 "truth": it["truth"], "trigger": row.get("trigger_type"), "probe": probe,
                                 "before": before, "after": after}, default=str) + "\n")
            fh.flush()
            n += 1
    return n


def _metrics(rows: list[dict], arm: str) -> dict:
    cl = [r for r in rows if r["truth"] and r["truth"]["outcome"] == "cleared" and "verdict" in r[arm]]
    fr = [r for r in rows if r["truth"] and r["truth"]["outcome"] == "confirmed_fraud" and "verdict" in r[arm]]
    harmed = [r["case_id"] for r in cl if set(r[arm]["final"]) & set(HARM)]
    acted = [r["case_id"] for r in cl if set(r[arm]["final"]) & set(ACTED)]
    caught = [r["case_id"] for r in fr if r[arm]["verdict"] == "fraud"]
    blocked = [r["case_id"] for r in fr if set(r[arm]["final"]) & {"BLOCK_CARD", "BLOCK_ALL_CARDS"}]
    return {"cleared": len(cl), "fraud": len(fr), "harmed": harmed, "acted_against": acted, "fraud_caught": caught,
            "fraud_blocked": blocked, "cleared_uncertain": [r["case_id"] for r in cl if r[arm]["verdict"] == "uncertain"]}


def merge(label: str, source_case: str) -> dict:
    from glob import glob

    from kavach.config import LOGS
    rows = []
    for f in sorted(glob(str(LOGS / "feedback" / f"{label}_*.jsonl"))):
        rows += [json.loads(ln) for ln in Path(f).read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows.sort(key=lambda r: (r["set"], r["case_id"]))
    others = [r for r in rows if not r["source"]]
    rep = {"label": label, "source_case": source_case, "feedback": [{k: x.get(k) for k in ("id", "case_id", "finding", "decision",
           "scope", "role", "key", "ts", "reason")} for x in active_records()], "n_cases": len(rows),
           "incidents": [r["case_id"] for r in rows if "incident" in r["before"] or "incident" in r["after"]],
           "sets": {}, "changed": [], "source": [r for r in rows if r["source"]]}
    for s in sorted({r["set"] for r in others}):
        sub = [r for r in others if r["set"] == s]
        rep["sets"][s] = {"n": len(sub), "probed": sum(1 for r in sub if r["probe"]),
                          "before": _metrics(sub, "before"), "after": _metrics(sub, "after")}
    for r in others:
        b, a = r["before"], r["after"]
        if b.get("verdict") != a.get("verdict") or b.get("final") != a.get("final") or b.get("p") != a.get("p"):
            rep["changed"].append({"set": r["set"], "case_id": r["case_id"], "truth": (r["truth"] or {}).get("outcome"),
                                   "truth_case": (r["truth"] or {}).get("case"), "before": {k: b.get(k) for k in ("verdict", "p", "final")},
                                   "after": {k: a.get(k) for k in ("verdict", "p", "final")},
                                   "records": sorted({i for f in a.get("feedback", []) for i in f["records"]})})
    from kavach.config import LOGS as _L
    (_L / "feedback" / f"{label}.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    return rep


def report_text(rep: dict) -> str:
    lines = [f"feedback before-and-after: {rep['label']} ({rep['n_cases']} runs, source case {rep['source_case']}, "
             f"incidents {len(rep['incidents'])})"]
    for f in rep["feedback"]:
        lines.append(f"  record {f['id']} {f['decision']} {f['finding']} on {f['case_id']} by {f['role']} scope {f['scope']} key {json.dumps(f['key'], sort_keys=True)}")
    for s, m in rep["sets"].items():
        b, a = m["before"], m["after"]
        lines.append(f"  {s}: {m['n']} cases ({m['cleared'] if 'cleared' in m else b['cleared']} cleared, {b['fraud']} fraud), "
                     f"{m['probed']} touched by feedback")
        lines.append(f"    cleared customers blocked or reported  {len(b['harmed']):3d} -> {len(a['harmed']):3d}")
        lines.append(f"    cleared customers acted against (incl. decline) {len(b['acted_against']):3d} -> {len(a['acted_against']):3d}")
        lines.append(f"    cleared left uncertain                 {len(b['cleared_uncertain']):3d} -> {len(a['cleared_uncertain']):3d}")
        lines.append(f"    frauds called fraud                    {len(b['fraud_caught']):3d} -> {len(a['fraud_caught']):3d}")
        lines.append(f"    frauds blocked                         {len(b['fraud_blocked']):3d} -> {len(a['fraud_blocked']):3d}")
    for c in rep["changed"]:
        lines.append(f"  changed {c['set']:7s} {c['case_id']} ({c['truth_case'] or 'no truth'}, {c['truth'] or '?'}): "
                     f"{c['before']['verdict']} {c['before']['p']} {','.join(c['before']['final'] or [])} -> "
                     f"{c['after']['verdict']} {c['after']['p']} {','.join(c['after']['final'] or [])} by {','.join(c['records'])}")
    for r in rep["source"]:
        lines.append(f"  source case {r['case_id']} (truth {(r['truth'] or {}).get('outcome')}): {r['before'].get('verdict')} "
                     f"{r['before'].get('p')} -> {r['after'].get('verdict')} {r['after'].get('p')} {','.join(r['after'].get('final') or [])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def _token(args) -> str | None:
    if args.token:
        return args.token
    env = {"analyst": "KAVACH_TOKEN_ANALYST", "L1": "KAVACH_TOKEN_L1", "L2": "KAVACH_TOKEN_L2"}.get(args.as_role or "")
    return os.environ.get(env, "") if env else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.feedback", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="accept or reject a finding on a case")
    r.add_argument("case_id")
    r.add_argument("finding")
    r.add_argument("decision", choices=DECISIONS)
    r.add_argument("--reason", required=True)
    r.add_argument("--scope", default="context", choices=SCOPES)
    r.add_argument("--token", default=None, help="bearer token (the role is resolved from it)")
    r.add_argument("--as-role", default=None, choices=("analyst", "L1", "L2"),
                   help="read the token of this role from the environment (.env) instead of --token")
    sub.add_parser("list")
    sub.add_parser("verify")
    e = sub.add_parser("eval", help="before-and-after run on the same cases (one shard; see docs/round2/analyst-loop.md)")
    e.add_argument("--source-case", required=True, help="the case the feedback was recorded on (reported apart)")
    e.add_argument("--label", default="eval")
    e.add_argument("--shard", type=int, default=0)
    e.add_argument("--shards", type=int, default=1)
    e.add_argument("--n-fraud", type=int, default=260)
    e.add_argument("--no-replays", action="store_true", help="only the Round 1 pack (and --hhf)")
    e.add_argument("--hhf", action="store_true", help="also the Round 2 pack (no truth: changes only)")
    e.add_argument("--resume", action="store_true", help="skip the cases already in this shard's file")
    m = sub.add_parser("merge", help="merge eval shards into the report")
    m.add_argument("--label", default="eval")
    m.add_argument("--source-case", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "record":
        try:
            rec = record(args.case_id, args.finding, args.decision, _token(args), args.reason, scope=args.scope)
        except Forbidden as ex:
            print(f"DENIED: {ex}")
            return 3
        print(json.dumps({k: rec[k] for k in ("id", "case_id", "finding", "decision", "scope", "role", "key", "ts", "hash")}, indent=1))
        return 0
    if args.cmd == "list":
        for x in records():
            print(f"{x['id']} {x['ts']} {x['status']:8s} {x.get('role', ''):8s} {x['case_id']} {x['finding']} {x['decision']} "
                  f"{json.dumps(x.get('key', {}), sort_keys=True)} | {x.get('reason', '')[:80]}")
        return 0
    if args.cmd == "verify":
        d = verify_detail()
        print(json.dumps(d))
        return 0 if d["ok"] else 1
    if args.cmd == "eval":
        n = evaluate(args.source_case, args.label, args.shard, args.shards, n_fraud=args.n_fraud,
                     replays=not args.no_replays, hhf=args.hhf, resume=args.resume)
        print(f"shard {args.shard}/{args.shards}: {n} cases -> {_eval_path(args.label, args.shard)}")
        return 0
    if args.cmd == "merge":
        rep = merge(args.label, args.source_case)
        print(report_text(rep))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
