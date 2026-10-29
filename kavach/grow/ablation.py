"""Ablation: run the case pack with and without the approved grown detectors and diff the decisions.

Both arms run with KAVACH_NO_LLM=1 (prose is non-decisional) and never write cases/. The result is written to
docs/round2/ablation.md (or `out`). It only measures something once detectors.run_all calls
kavach.grow.registry.signals_for and at least one detector is approved; the report says so when either is missing.
"""
from __future__ import annotations

import inspect
import os
import time
from datetime import datetime
from pathlib import Path

from kavach.config import ROOT
from kavach.grow import registry

DOC = ROOT / "docs" / "round2" / "ablation.md"


def wired() -> bool:
    try:
        from kavach import detectors
        return "signals_for" in inspect.getsource(detectors.run_all)
    except Exception:
        return False


def summary(ans: dict, trace: dict | None) -> dict:
    case = ans["case"]
    grown = [s for s in (trace or {}).get("signals", []) if str(s.get("name", "")).startswith("grown:")]
    return {"verdict": case["verdict"], "p": case["fraud_probability"], "pattern": case["pattern"],
            "final": sorted(x["action"] for x in ans["next_best_actions"]["final"]), "sar": bool(ans["sar"]["file"]),
            "n_affected": len(case["affected_txn_ids"]), "exposure": case["exposure_usd"],
            "n_connected": len(case["connected_card_ids"]),
            "grown": [{"name": s["name"], "llr": s["llr"], "group": s["group"]} for s in grown]}


def _run(case: dict, backend: str) -> dict:
    from kavach.agent import investigate
    from kavach.llm import Llm
    out = investigate(case, backend=backend, llm=Llm())
    ans, trace = out if isinstance(out, tuple) else (out, None)
    if ans is None:
        return {"error": "investigation failed closed (no answer)"}
    return summary(ans, trace)


def ablate(case_ids=None, backend: str = "duck", pack_round: int | None = None, out=None) -> dict:
    from kavach import duckq
    keep = {k: os.environ.get(k) for k in ("KAVACH_NO_LLM", "KAVACH_GROWN")}
    os.environ["KAVACH_NO_LLM"] = "1"
    pack = duckq.case_pack(round=pack_round) if pack_round is not None else duckq.case_pack()
    cases = [r for r in pack.to_dict("records") if not case_ids or r["case_id"] in set(case_ids)]
    approved = registry.load_approved(verbose=False)
    res: dict = {}
    t0 = time.perf_counter()
    try:
        for arm in ("1", "0"):
            os.environ["KAVACH_GROWN"] = arm
            for case in cases:
                try:
                    res.setdefault(case["case_id"], {})[arm] = _run(case, backend)
                except Exception as e:  # one failing case must not hide the others; it is reported as an error row
                    res.setdefault(case["case_id"], {})[arm] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    rows = []
    for cid, arms in res.items():
        on, off = arms.get("1", {}), arms.get("0", {})
        keys = ("verdict", "p", "pattern", "final", "sar", "n_affected", "exposure", "n_connected")
        changed = [k for k in keys if on.get(k) != off.get(k)]
        rows.append({"case_id": cid, "with": on, "without": off, "changed": changed})
    report = {"run_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "backend": backend, "wired": wired(),
              "approved": [s["name"] for s in approved], "cases": rows, "seconds": round(time.perf_counter() - t0, 1),
              "dataset_version": registry.dataset_version()}
    write(report, Path(out) if out else DOC)
    return report


def _fmt(a: dict) -> str:
    if "error" in a:
        return f"error: {a['error']}"
    return f"{a['verdict']} p={a['p']} {','.join(a['final'])}" + (" SAR" if a["sar"] else "")


def write(report: dict, path: Path) -> Path:
    n_changed = sum(1 for r in report["cases"] if r["changed"])
    n_fired = sum(1 for r in report["cases"] if r["with"].get("grown"))
    lines = [
        "# Ablation: approved grown detectors on and off",
        "",
        f"Run {report['run_at']} IST, backend `{report['backend']}`, index `{report['dataset_version'].get('index')}` "
        f"version `{report['dataset_version'].get('version')}`, {report['seconds']} s, `KAVACH_NO_LLM=1`, no answer files written.",
        f"Command: `python -m kavach.grow ablate` (arm A `KAVACH_GROWN=1`, arm B `KAVACH_GROWN=0`).",
        "",
        f"- Approved grown detectors loaded: {', '.join(report['approved']) or 'none'}",
        f"- `detectors.run_all` calls `registry.signals_for`: {'yes' if report['wired'] else 'NO (integration pending, so both arms are identical by construction)'}",
        f"- Cases where a grown detector fired: {n_fired} of {len(report['cases'])}; cases whose decision changed: {n_changed}",
        "",
        "| case | grown signals (arm A) | with grown detectors | without | changed fields |",
        "|---|---|---|---|---|",
    ]
    for r in report["cases"]:
        g = "; ".join(f"{s['name']} ({s['group']}, llr {s['llr']})" for s in r["with"].get("grown", [])) or "none"
        lines.append(f"| {r['case_id']} | {g} | {_fmt(r['with'])} | {_fmt(r['without'])} | {', '.join(r['changed']) or 'none'} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def preview(name: str, kind: str = "proposed", pack_round: int | None = None, backend: str = "duck") -> list[dict]:
    """What a proposed detector would add on the open case pack, before anyone approves it: gather each case through
    a fresh ToolRegistry and run the detector's runtime rule. Read-only; it changes no decision."""
    from kavach import duckq
    from kavach.context import gather
    from kavach.grow import templates
    from kavach.tools import ToolRegistry
    spec = registry.read(name, kind)
    pack = duckq.case_pack(round=pack_round) if pack_round is not None else duckq.case_pack()
    out = []
    for case in pack.to_dict("records"):
        T = ToolRegistry(backend)
        try:
            ctx = gather(case, T)
            sigs = templates.apply(ctx, spec)
        except Exception as e:
            out.append({"case_id": case["case_id"], "error": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        out.append({"case_id": case["case_id"], "fired": bool(sigs), "group": sigs[0].group if sigs else "",
                    "llr": sigs[0].llr if sigs else 0.0, "claim": sigs[0].claim if sigs else "",
                    "entity_ids": sigs[0].entity_ids if sigs else [], "how": sigs[0].how if sigs else {}})
    return out
