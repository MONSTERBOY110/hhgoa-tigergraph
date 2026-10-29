"""Detector registry: proposed -> (L2 approval) -> approved, or rejected. Approved specs run at DETECT time.

Files: detectors/proposed/<name>.json, detectors/approved/<name>.json, detectors/rejected/<name>.json.
Approval goes through kavach.harness.approvals with action FILE_REPORT (route L2), so only an L2 token passes; the
approval record (id, role, ts, chain hash) and a sha256 of the approved spec are stored in the file. At load time a
spec is used only if it carries an L2 approval and its content still hashes to the approved sha: a file copied into
approved/ by hand, or edited after approval, is refused and the refusal is printed.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from kavach.config import ROOT
from kavach.grow import templates

DET_DIR = ROOT / "detectors"
KINDS = ("proposed", "approved", "rejected")


def _dir(kind: str) -> Path:
    d = Path(os.environ.get("KAVACH_DETECTORS_DIR") or DET_DIR) / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def spec_sha(spec: dict) -> str:
    core = {k: spec.get(k) for k in ("name", "family", "params", "backtest")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()


def dataset_version(duck_path=None) -> dict:
    """Version of the index the backtest ran on: the dataset_version table when the index has one, else file sizes."""
    import duckdb
    from kavach.config import DUCK_PATH
    p = Path(duck_path) if duck_path else Path(DUCK_PATH)
    try:
        con = duckdb.connect(str(p), read_only=True)
        try:
            rows = con.execute("select name, bytes, sha256, rows, built_at from dataset_version order by name").df()
        finally:
            con.close()
        combined = hashlib.sha256("".join(rows.sha256).encode()).hexdigest()[:12]
        return {"index": p.name, "version": combined, "built_at": str(rows.built_at.iloc[0]) if len(rows) else "",
                "files": {r.name: {"bytes": int(r.bytes), "sha256": str(r.sha256)[:16], "rows": int(r.rows)} for r in rows.itertuples()}}
    except Exception:
        size = p.stat().st_size if p.exists() else 0
        return {"index": p.name, "version": f"size-{size}", "built_at": "", "files": {p.name: {"bytes": size}}}


def _write(kind: str, spec: dict) -> Path:
    path = _dir(kind) / f"{spec['name']}.json"
    path.write_text(json.dumps(spec, indent=1, default=str), encoding="utf-8")
    return path


def read(name: str, kind: str = "proposed") -> dict:
    path = _dir(kind) / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"no {kind} detector named {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def make_spec(c, description: str, source: str, dv: dict) -> dict:
    from kavach.grow.describe import title
    spec = {"name": c.name, "title": title(c), "family": c.family, "params": c.params, "rule": c.rule,
            "description": description, "description_source": source, "backtest": c.backtest(),
            "proposed_at": _now(), "dataset_version": dv, "status": "proposed", "approval": None}
    spec["spec_sha"] = spec_sha(spec)
    return spec


def propose(candidates, llm=None, include_rediscoveries: bool = False, duck_path=None) -> list[Path]:
    """Write a proposal per candidate (novel ones by default). Existing approved or rejected names are not re-proposed."""
    from kavach.grow.describe import describe
    dv = dataset_version(duck_path)
    out = []
    for c in candidates:
        if c.status != "novel" and not include_rediscoveries:
            continue
        if (_dir("approved") / f"{c.name}.json").exists() or (_dir("rejected") / f"{c.name}.json").exists():
            continue
        text, source = describe(c, llm)
        out.append(_write("proposed", make_spec(c, text, source, dv)))
    return out


def _record(rec) -> dict:
    if isinstance(rec, dict):
        return rec
    if hasattr(rec, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(rec)
    return dict(vars(rec))


def approve(name: str, token: str, note: str = "") -> dict:
    """L2 approval through the harness. Raises kavach.harness.faults.Forbidden for any other role (the harness logs
    the denied attempt); the proposal then stays where it is."""
    from kavach.harness import approvals
    spec = read(name, "proposed")
    sha = spec_sha(spec)
    if sha != spec.get("spec_sha"):
        raise ValueError(f"proposal {name} was edited after it was proposed (sha mismatch); re-run propose")
    rec = _record(approvals.approve(case_id=f"DETECTOR-{name}", action="FILE_REPORT", token=token, exposure=0.0,
                                    note=note or f"approve grown detector {name} spec {sha[:12]}"))
    spec["approval"] = {"id": rec.get("id"), "role": rec.get("role"), "route": rec.get("route"), "ts": rec.get("ts"),
                        "approver": rec.get("approver"), "hash": rec.get("hash"), "note": note, "spec_sha": sha}
    spec["status"] = "approved"
    path = _write("approved", spec)
    (_dir("proposed") / f"{name}.json").unlink(missing_ok=True)
    return {"name": name, "path": str(path), **spec["approval"]}


def reject(name: str, token: str, reason: str) -> dict:
    """Any analyst or above can reject (it lowers risk); the decision is recorded in the approvals chain as an
    auto-route CLOSE_NO_FRAUD on DETECTOR-<name> with the reason in the note."""
    from kavach.harness import approvals
    spec = read(name, "proposed")
    rec = _record(approvals.approve(case_id=f"DETECTOR-{name}", action="CLOSE_NO_FRAUD", token=token, exposure=0.0,
                                    note=f"reject grown detector {name}: {reason}"))
    spec["status"] = "rejected"
    spec["rejection"] = {"id": rec.get("id"), "role": rec.get("role"), "ts": rec.get("ts"), "hash": rec.get("hash"), "reason": reason}
    path = _write("rejected", spec)
    (_dir("proposed") / f"{name}.json").unlink(missing_ok=True)
    return {"name": name, "path": str(path), **spec["rejection"]}


def listing() -> dict:
    out = {}
    for kind in KINDS:
        out[kind] = []
        for p in sorted(_dir(kind).glob("*.json")):
            s = json.loads(p.read_text(encoding="utf-8"))
            bt = s.get("backtest", {})
            out[kind].append({"name": s["name"], "family": s["family"], "support": bt.get("support"), "cleared_hits": bt.get("cleared_hits"),
                              "lift": bt.get("lift"), "approval": (s.get("approval") or {}).get("id")})
    return out


def load_approved(verbose: bool = True) -> list[dict]:
    """Approved specs that pass the gate: known family, an L2 approval with an id, and an unchanged spec hash."""
    out = []
    for p in sorted(_dir("approved").glob("*.json")):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            print(f"  grown detector {p.name} refused: not valid JSON")
            continue
        ap = s.get("approval") or {}
        why = ("unknown family" if s.get("family") not in templates.FAMILIES else
               "no approval record" if not ap.get("id") else
               f"approved by role {ap.get('role')!r}, needs L2" if ap.get("role") != "L2" else
               "spec changed after approval" if spec_sha(s) != ap.get("spec_sha") else "")
        if why:
            if verbose:
                print(f"  grown detector {s.get('name', p.stem)} refused: {why}")
            continue
        out.append(s)
    return out


def enabled() -> bool:
    return os.environ.get("KAVACH_GROWN", "1").strip() != "0"


def signals_for(ctx) -> list:
    """Signals from every approved grown detector for one case (empty when KAVACH_GROWN=0).

    A detector that needs a tool the current backend does not serve is skipped and the skip is recorded on
    ctx.grown_skipped; any other error propagates (fail closed)."""
    if not enabled():
        return []
    sigs = []
    skipped = []
    for spec in load_approved(verbose=False):
        try:
            sigs += templates.apply(ctx, spec)
        except templates.ToolUnavailable as e:
            skipped.append({"detector": spec["name"], "missing_tool": str(e)})
    try:
        ctx.grown_skipped = skipped
    except Exception:
        pass
    return sigs


def llr_for(spec: dict) -> float:
    lift = float(spec.get("backtest", {}).get("lift") or 1.0)
    return round(max(0.0, min(2.0, 0.5 * math.log(max(lift, 1.0)))), 2)
