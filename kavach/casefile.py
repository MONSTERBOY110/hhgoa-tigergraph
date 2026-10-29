"""Case lifecycle: an append-only event log per case, the current state, and a handover an analyst can pick up cold.

    append(case_id, kind, detail="", evidence_ids=(), actor="agent", approval_id=None, extra=None)
    events(case_id) -> list of events, oldest first
    state(case_id) -> status, last decision, what changed, pending approvals, who decided what
    handover(case_id) -> text
    record_answer(answer, trace=None) -> the decision events for a finished investigation

Events live in data/casefiles/<case>.jsonl (override with env KAVACH_CASEFILE_DIR). Each line carries a UTC ts and
a hash chained to the previous line, so an edited or deleted line is detectable (verify_chain). There is no update
or delete path. CLI: python -m kavach.casefile <case> [--json]
"""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from kavach.config import DATA, env

KINDS = ("opened", "intake", "hostile_text_detected", "gathered", "detected", "assessed", "actions_initial",
         "evidence_requested", "evidence_received", "actions_final", "awaiting_approval", "approved", "rejected",
         "escalated", "closed", "failed", "rerun", "written_to_graph")
DECISION_KINDS = ("actions_initial", "actions_final", "approved", "rejected", "escalated", "closed")
APPROVAL_ROUTES = ("L1", "L2")


def casefile_dir() -> Path:
    return Path(env("KAVACH_CASEFILE_DIR") or (DATA / "casefiles"))


def path(case_id: str) -> Path:
    return casefile_dir() / f"{case_id}.jsonl"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _redact(obj):
    try:
        from kavach.harness.redact import redact
        return redact(obj)
    except Exception:
        return obj


def _digest(prev: str, ev: dict) -> str:
    body = {k: v for k, v in ev.items() if k != "hash"}
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
def append(case_id: str, kind: str, detail: str = "", evidence_ids=(), actor: str = "agent", approval_id=None,
           extra: dict | None = None) -> dict:
    """Append one event. `kind` must be one of KINDS; `actor` is "agent" or the approving role (never taken from a
    request body by callers); `evidence_ids` are the dataset ids that caused the change."""
    if kind not in KINDS:
        raise ValueError(f"unknown casefile event kind {kind!r}; expected one of {', '.join(KINDS)}")
    p = path(case_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = events(case_id)
    ev = {"seq": len(prev) + 1, "ts": _now(), "case_id": case_id, "kind": kind, "detail": str(detail or ""),
          "evidence_ids": [str(x) for x in evidence_ids or []], "actor": actor or "agent", "approval_id": approval_id,
          "extra": dict(extra or {}), "prev_hash": prev[-1]["hash"] if prev else ""}
    ev = _redact(ev)
    ev["hash"] = _digest(ev["prev_hash"], ev)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, default=str, ensure_ascii=False) + "\n")
    return ev


def events(case_id: str) -> list[dict]:
    p = path(case_id)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def verify_chain(case_id: str) -> bool:
    prev = ""
    for ev in events(case_id):
        if ev.get("prev_hash", "") != prev or _digest(prev, ev) != ev.get("hash"):
            return False
        prev = ev["hash"]
    return True


# ---------------------------------------------------------------------------------------------------------------
def _actions(lst) -> list[dict]:
    return [{"action": x["action"], "route": x.get("route", "auto"), "reason": x.get("reason", "")} for x in lst or []]


def _fmt_actions(lst) -> str:
    return ", ".join(f"{a['action']} ({a['route']})" for a in lst) or "none"


def record_answer(answer: dict, trace: dict | None = None, actor: str = "agent") -> list[dict]:
    """The decision events of a finished investigation, in order: assessment(s), initial actions, evidence asked for
    and received, final actions, one awaiting_approval per L1/L2 action, escalation and closure."""
    cid = answer["case_id"]
    k = answer.get("case", {})
    nba = answer.get("next_best_actions", {})
    trace = trace or {}
    out = []
    ep_ids = list(k.get("affected_txn_ids") or []) + list(k.get("connected_card_ids") or [])

    def snap(a):
        return {"p": a.get("p"), "verdict": a.get("verdict"), "fraud_groups": a.get("fraud_groups", []),
                "legit_groups": a.get("legit_groups", []), "by_group": a.get("by_group", {})}

    a0 = trace.get("assess0")
    if a0:
        out.append(append(cid, "assessed", f"before any evidence request: p={a0.get('p')} verdict {a0.get('verdict')}",
                          actor=actor, extra={"stage": "assess0", **snap(a0)}))
    initial = _actions(nba.get("initial"))
    out.append(append(cid, "actions_initial", _fmt_actions(initial), actor=actor, extra={"actions": initial}))
    for i, r in enumerate(answer.get("evidence_requests") or [], 1):
        out.append(append(cid, "evidence_requested", f"{r.get('type')} after step {r.get('asked_after_step')}",
                          actor=actor, extra={"request": i, "type": r.get("type")}))
        out.append(append(cid, "evidence_received", r.get("assumed_response", ""), actor=actor,
                          extra={"request": i, "assumed": True}))
    a1 = trace.get("assess1")
    if a1 and answer.get("evidence_requests"):
        out.append(append(cid, "assessed", f"after the reply: p={a1.get('p')} verdict {a1.get('verdict')}",
                          actor=actor, extra={"stage": "assess1", **snap(a1)}))
    elif not a0:
        out.append(append(cid, "assessed", f"p={k.get('fraud_probability')} verdict {k.get('verdict')}", actor=actor,
                          extra={"stage": "final", "p": k.get("fraud_probability"), "verdict": k.get("verdict")}))
    final = _actions(nba.get("final"))
    out.append(append(cid, "actions_final", _fmt_actions(final), evidence_ids=ep_ids, actor=actor,
                      extra={"actions": final, "what_changed": nba.get("what_changed", ""), "status": k.get("status"),
                             "verdict": k.get("verdict"), "p": k.get("fraud_probability"),
                             "exposure_usd": k.get("exposure_usd")}))
    for a in final:
        if a["route"] in APPROVAL_ROUTES:
            out.append(append(cid, "awaiting_approval", f"{a['action']} needs {a['route']} approval: {a['reason']}",
                              evidence_ids=ep_ids, actor=actor, extra={"action": a["action"], "route": a["route"]}))
    esc = next((a for a in final if a["action"] == "ESCALATE_TO_ANALYST"), None)
    if esc or k.get("status") == "escalated":
        out.append(append(cid, "escalated", esc["reason"] if esc else "status escalated", actor=actor,
                          extra={"status": k.get("status")}))
    if str(k.get("status", "")).startswith("closed"):
        out.append(append(cid, "closed", f"{k.get('status')}: verdict {k.get('verdict')}", evidence_ids=ep_ids,
                          actor=actor, extra={"status": k.get("status")}))
    return out


# ---------------------------------------------------------------------------------------------------------------
def _harness_pending(case_id: str, actions: list[dict]):
    """kavach.harness.approvals.pending(case_id, actions): the L1/L2 actions with no approved record in the
    hash-chained approvals log. None when the harness is not importable or fails (then the casefile alone decides)."""
    try:
        from kavach.harness import approvals
        got = approvals.pending(case_id, actions) or []
    except Exception:
        return None
    return [g if isinstance(g, dict) else dict(getattr(g, "__dict__", {})) for g in got]


def _pending_from_events(evs: list[dict]) -> list[dict]:
    """Approvals the latest decision needs and nobody has given or refused yet. Required approvals come from the
    awaiting_approval events after the last actions_final; an approved or rejected record for the same action
    anywhere in the file resolves it (decisions are deterministic, so a re-run asks for the same approval)."""
    last_final = max((e["seq"] for e in evs if e["kind"] == "actions_final"), default=0)
    resolved = {e.get("extra", {}).get("action") for e in evs if e["kind"] in ("approved", "rejected")}
    out, seen = [], set()
    for e in evs:
        if e["kind"] != "awaiting_approval" or e["seq"] < last_final:
            continue
        act = e.get("extra", {}).get("action")
        if act in resolved or act in seen:
            continue
        seen.add(act)
        out.append({"action": act, "route": e.get("extra", {}).get("route"), "since": e["ts"], "detail": e["detail"],
                    "source": "casefile"})
    return out


def state(case_id: str) -> dict:
    evs = events(case_id)
    status = "unknown"
    verdict = p = None
    last_decision = None
    what_changed = []
    for e in evs:
        kind, x = e["kind"], e.get("extra", {})
        if kind in ("opened", "rerun"):
            status = "open"
        elif kind == "escalated":
            status = "escalated"
        elif kind == "closed":
            status = x.get("status") or "closed"
        elif kind == "failed":
            status = "failed"
        elif kind == "actions_final" and x.get("status"):
            status = x["status"]
        if kind == "assessed" or (kind == "actions_final" and x.get("verdict")):
            verdict, p = x.get("verdict", verdict), x.get("p", p)
        if kind in DECISION_KINDS:
            last_decision = {"kind": kind, "ts": e["ts"], "actor": e["actor"], "detail": e["detail"],
                             "approval_id": e.get("approval_id")}
        if kind == "evidence_received" or (kind == "actions_final" and x.get("what_changed") not in (None, "", "nothing")):
            what_changed.append({"ts": e["ts"], "kind": kind,
                                 "detail": x.get("what_changed") if kind == "actions_final" else e["detail"]})
    pending = _pending_from_events(evs)
    final = next((e.get("extra", {}).get("actions") for e in reversed(evs) if e["kind"] == "actions_final"), None)
    if final is not None:
        # an approval recorded in either log resolves the action; the harness log is the server-side authority
        hp = _harness_pending(case_id, final)
        if hp is not None:
            still = {h.get("action"): h for h in hp}
            pending = [{**x, "needs_role": still[x["action"]].get("needs_role")} for x in pending if x["action"] in still]
    decisions = [{"ts": e["ts"], "kind": e["kind"], "actor": e["actor"], "detail": e["detail"],
                  "approval_id": e.get("approval_id")} for e in evs if e["kind"] in DECISION_KINDS]
    return {"case_id": case_id, "status": status, "verdict": verdict, "p": p, "n_events": len(evs),
            "sittings": sum(1 for e in evs if e["kind"] in ("opened", "rerun")) or (1 if evs else 0),
            "opened_at": evs[0]["ts"] if evs else "", "last_activity": evs[-1]["ts"] if evs else "",
            "last_decision": last_decision, "what_changed": what_changed, "pending_approvals": pending,
            "decisions": decisions, "chain_ok": verify_chain(case_id)}


def handover(case_id: str) -> str:
    s = state(case_id)
    evs = events(case_id)
    if not evs:
        return f"Case {case_id}: no casefile events recorded (looked in {path(case_id)})."
    wait = s["pending_approvals"]
    L = [f"Case {case_id}: {s['status']}" + (f", waiting on {len(wait)} approval(s)" if wait else ""),
         f"Opened {s['opened_at']}; {s['n_events']} events over {s['sittings']} sitting(s); last activity {s['last_activity']}.",
         f"Current assessment: verdict {s['verdict']}, fraud probability {s['p']}." if s["verdict"] else "No assessment yet.",
         f"Audit chain: {'intact' if s['chain_ok'] else 'BROKEN (an event was edited or removed)'}.", ""]
    ld = s["last_decision"]
    L.append("Where it stands")
    L.append(f"  last decision: {ld['kind']} by {ld['actor']} at {ld['ts']}: {ld['detail']}" if ld else "  no decision yet")
    last = evs[-1]
    L.append(f"  last event: {last['kind']} at {last['ts']}" + (f": {last['detail']}" if last["detail"] else ""))
    L += ["", "What it is waiting on"]
    L += [f"  - {w.get('action')} needs {w.get('route')} approval (pending since {w.get('since', '?')})" for w in wait] or \
         ["  - nothing: no L1 or L2 action is waiting for approval"]
    if s["status"] == "failed":
        L.append("  - the last run failed; see the failed event and incidents/ before re-running")
    L += ["", "What changed"]
    L += [f"  - {w['ts']} {w['kind']}: {w['detail']}" for w in s["what_changed"]] or ["  - nothing since the first assessment"]
    L += ["", "Who decided what"]
    L += [f"  - {d['ts']} {d['actor']}: {d['kind']}"
          + (f" [approval {d['approval_id']}]" if d.get("approval_id") else "") + f": {d['detail']}"
          for d in s["decisions"]] or ["  - no decisions recorded"]
    L += ["", "Timeline"]
    for e in evs:
        ids = e.get("evidence_ids") or []
        L.append(f"  {e['seq']:>3d} {e['ts']} {e['kind']:21s} {e['actor']:8s} {e['detail'][:110]}"
                 + (f" [ids: {', '.join(ids[:5])}{', ...' if len(ids) > 5 else ''}]" if ids else ""))
    return "\n".join(L)


def _utf8_stdout() -> None:
    """Windows consoles and pipes default to cp1252; policy text carries the section sign."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m kavach.casefile", description="Case handover view.")
    ap.add_argument("case")
    ap.add_argument("--json", action="store_true", help="print the state as JSON")
    a = ap.parse_args(argv)
    _utf8_stdout()
    if a.json:
        print(json.dumps(state(a.case), indent=2, default=str))
    else:
        print(handover(a.case))
    return 0 if events(a.case) else 1


if __name__ == "__main__":
    sys.exit(main())
