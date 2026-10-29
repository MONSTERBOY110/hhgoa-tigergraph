"""Regulator's view: replay an investigation step by step from its trace.

    replay_data(case_id) -> dict   (steps, assessment snapshots, signals, decision; for the viewer and the server)
    replay(case_id) -> str         (the same as a text table)

Each tool call shows its parameters, how many rows came back, a sample of the ids in the result (result_digest,
when the harness recorded it) and the attested claims that rest on it. Assessment snapshots sit where they were
taken (assess0 after detection, assess1 after the customer's reply, plus any per-step snapshots in the trace),
with the signals that appeared and how each evidence group moved p.
CLI: python -m kavach.audit <case> [--json]
"""
import json
import sys
from pathlib import Path

from kavach.config import CASES_DIR, LOGS

PHASES = {0: "INTAKE", 1: "GATHER", 2: "GATHER", 3: "GATHER", 4: "DETECT", 5: "ASSESS0", 6: "ACT0", 7: "REQUEST",
          8: "ASSESS1", 9: "ACT1", 10: "RECALL", 11: "NARRATE", 12: "PERSIST"}
DETECT_END, REPLY_END = 4, 8


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _fmt(v) -> str:
    from kavach.tools import _fmt as tfmt
    return tfmt(v)


def _params_str(params: dict, width: int = 60) -> str:
    s = ", ".join(f"{k}={v}" for k, v in (params or {}).items())
    return s if len(s) <= width else s[: width - 3] + "..."


def _links(entry: dict, claims: list[dict]) -> list[str]:
    """Attested claims whose query is this tool call (same tool, every parameter formatted the same way)."""
    out = []
    for c in claims:
        how = c.get("how") or {}
        if how.get("tool") != entry.get("tool"):
            continue
        tp = entry.get("params") or {}
        if all(str(tp.get(k)) == _fmt(v) for k, v in (how.get("params") or {}).items()):
            out.append(c["id"])
    return out


def _signal_row(s: dict) -> dict:
    return {"name": s.get("name", ""), "group": s.get("group", ""), "llr": s.get("llr", 0.0), "claim": s.get("claim", ""),
            "ids": [str(x) for x in s.get("entity_ids") or []], "ref": s.get("ref", ""),
            "how": s.get("how") or {}}


def _snapshot(label: str, a: dict, sigs: list[dict], before: dict | None) -> dict:
    by = a.get("by_group") or {}
    moved = []
    if before is not None:
        b = before.get("by_group") or {}
        for g in sorted(set(b) | set(by)):
            if round(b.get(g, 0.0), 2) != round(by.get(g, 0.0), 2):
                moved.append({"group": g, "before": b.get(g, 0.0), "after": by.get(g, 0.0)})
    return {"label": label, "p": a.get("p"), "verdict": a.get("verdict"), "by_group": by,
            "fraud_groups": a.get("fraud_groups", []), "legit_groups": a.get("legit_groups", []),
            "p_before": before.get("p") if before else None, "moved": moved, "signals": sigs}


def replay_data(case_id: str, traces_dir: Path | str | None = None, cases_dir: Path | str | None = None,
                attest_dir: Path | str | None = None) -> dict:
    trace = _load(Path(traces_dir or (LOGS / "traces")) / f"{case_id}.json")
    if trace is None:
        raise FileNotFoundError(f"no trace for {case_id} in {traces_dir or (LOGS / 'traces')}")
    answer = _load(Path(cases_dir or CASES_DIR) / f"{case_id}.json") or {}
    if attest_dir is None:
        from kavach.attest import attest_dir as _ad
        attest_dir = _ad()
    side = _load(Path(attest_dir) / f"{case_id}.json") or {}
    claims = side.get("claims", [])

    sigs = [_signal_row(s) for s in trace.get("signals", [])]
    reply_sigs = [s for s in sigs if s["ref"].startswith("evidence_request")]
    det_sigs = [s for s in sigs if not s["ref"].startswith("evidence_request")]
    a0, a1 = trace.get("assess0") or {}, trace.get("assess1") or {}
    asked = bool(answer.get("evidence_requests")) or bool(reply_sigs) or (a1 and a0 and a1 != a0)

    # snapshots keyed by the step after which they were taken
    snaps = []
    if a0:
        snaps.append((DETECT_END, _snapshot("ASSESS0 after detection", a0, det_sigs, None)))
    for s in trace.get("assessments") or []:              # optional per-step snapshots recorded by the agent
        snaps.append((int(s.get("step", 0)), _snapshot(f"assessment after step {s.get('step')}", s, [], None)))
    if a1 and asked:
        snap1 = _snapshot("ASSESS1 after the reply", a1, reply_sigs, a0)
        snap1["requests"] = [f"{r.get('type')} after step {r.get('asked_after_step')}: assumed reply {r.get('assumed_response')!r}"
                             for r in answer.get("evidence_requests") or []]
        snaps.append((REPLY_END, snap1))
    snaps.sort(key=lambda x: x[0])

    rows, si = [], 0
    tools = trace.get("tools") or []
    for i, t in enumerate(tools):
        step = int(t.get("step", 0))
        while si < len(snaps) and snaps[si][0] < step:
            rows.append({"type": "assessment", **snaps[si][1]})
            si += 1
        dg = t.get("result_digest") or {}
        rows.append({"type": "tool", "n": i + 1, "step": step, "phase": PHASES.get(step, f"STEP{step}"),
                     "tool": t.get("tool", ""), "params": t.get("params") or {}, "n_rows": t.get("n_rows"),
                     "digest_ids": list(dg.get("ids") or [])[:20], "digest_sha1": dg.get("sha1", ""),
                     "ms": t.get("ms"), "claims": _links(t, claims), "error": t.get("error")})
    rows += [{"type": "assessment", **s} for _, s in snaps[si:]]

    k = answer.get("case") or {}
    nba = answer.get("next_best_actions") or {}
    return {"case_id": case_id, "backend": side.get("backend", ""),
            "dataset_version": (side.get("dataset_version") or {}).get("version", "") if isinstance(side.get("dataset_version"), dict) else side.get("dataset_version", ""),
            "n_tool_calls": len(tools), "rows": rows, "log": trace.get("log") or [], "sources": trace.get("sources") or {},
            "decision": {"verdict": k.get("verdict"), "p": k.get("fraud_probability"), "pattern": k.get("pattern"),
                         "status": k.get("status"), "initial": nba.get("initial") or [], "final": nba.get("final") or [],
                         "what_changed": nba.get("what_changed", ""), "stop_reason": answer.get("stop_reason", "")},
            "n_claims": len(claims), "has_attestation": bool(side)}


def _short(t: str, n: int) -> str:
    t = str(t or "")
    return t if len(t) <= n else t[: n - 3] + "..."


def replay(case_id: str, traces_dir=None, cases_dir=None, attest_dir=None) -> str:
    d = replay_data(case_id, traces_dir, cases_dir, attest_dir)
    dec = d["decision"]
    L = [f"Replay {case_id}: {d['n_tool_calls']} tool calls"
         + (f", backend {d['backend']}" if d["backend"] else "")
         + (f", dataset {d['dataset_version']}" if d["dataset_version"] else "")
         + (f", {d['n_claims']} attested claims" if d["has_attestation"] else ", no attestation sidecar")]
    if dec["verdict"]:
        L.append(f"Decision: {dec['verdict']} p={dec['p']} pattern {dec['pattern']} status {dec['status']}; final "
                 + ", ".join(f"{a['action']} ({a['route']})" for a in dec["final"]))
    L += ["", f"{'#':>3s} {'step':>4s} {'phase':8s} {'tool':22s} {'params':60s} {'rows':>5s} {'ms':>7s}  digest / claims"]
    for r in d["rows"]:
        if r["type"] == "tool":
            dig = ""
            if r["digest_ids"] or r["digest_sha1"]:
                dig = ", ".join(map(str, r["digest_ids"][:3])) + (" ..." if len(r["digest_ids"]) > 3 else "")
                dig += f" sha1:{r['digest_sha1'][:8]}" if r["digest_sha1"] else ""
            link = f" -> {', '.join(r['claims'])}" if r["claims"] else ""
            ms = "" if r["ms"] is None else f"{r['ms']:.1f}"
            L.append(f"{r['n']:>3d} {r['step']:>4d} {r['phase']:8s} {_short(r['tool'], 22):22s} {_params_str(r['params']):60s} "
                     f"{'' if r['n_rows'] is None else r['n_rows']:>5} {ms:>7s}  {dig or '-'}{link}"
                     + (f"  ERROR {r['error']}" if r.get("error") else ""))
            continue
        L.append("")
        moved = f" (from {r['p_before']})" if r["p_before"] is not None else ""
        L.append(f"  >>> {r['label']}: p={r['p']}{moved} verdict {r['verdict']}")
        L.append("      groups: " + (", ".join(f"{g} {v:+.2f}" for g, v in r["by_group"].items()) or "none")
                 + f"  | for fraud: {', '.join(r['fraud_groups']) or '-'}; for legitimate: {', '.join(r['legit_groups']) or '-'}")
        for q in r.get("requests") or []:
            L.append(f"      evidence request: {q}")
        if r["label"].startswith("ASSESS1") and not r["signals"]:
            L.append("      no new signal from the reply")
        for m in r["moved"]:
            L.append(f"      moved: {m['group']} {m['before']:+.2f} -> {m['after']:+.2f}")
        if r["signals"]:
            L.append("      signals that appeared:")
            for s in sorted(r["signals"], key=lambda s: -abs(s["llr"] or 0)):
                ids = f" [{', '.join(s['ids'][:4])}{', ...' if len(s['ids']) > 4 else ''}]" if s["ids"] else ""
                L.append(f"        {s['llr']:+.2f} {s['group']:14s} {s['name']:28s} {_short(s['claim'], 90)}{ids}")
        L.append("")
    if dec["what_changed"] and dec["what_changed"] != "nothing":
        L += ["", f"What changed: {dec['what_changed']}"]
    if dec["stop_reason"]:
        L.append(f"Why it stopped: {dec['stop_reason']}")
    if d["log"]:
        L += ["", "Agent log:"] + [f"  {x}" for x in d["log"]]
    L += ["", f"Challenge any step: python -m kavach.attest {case_id} re-runs every attested query above."]
    return "\n".join(L)


def _utf8_stdout() -> None:
    """Windows consoles and pipes default to cp1252; policy text carries the section sign."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m kavach.audit", description="Step-by-step replay of an investigation.")
    ap.add_argument("case")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    _utf8_stdout()
    try:
        if a.json:
            print(json.dumps(replay_data(a.case), indent=2, default=str))
        else:
            print(replay(a.case))
    except FileNotFoundError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
