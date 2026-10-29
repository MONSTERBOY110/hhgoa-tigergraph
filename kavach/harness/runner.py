"""run_case(): one investigation inside the harness. Fail closed.

On success it returns Outcome(status="ok", answer=..., trace=...). On any failure (a tool that failed twice, a bound
exceeded, the graph store down, invalid input, any unexpected exception) it returns Outcome(status="incident",
answer=None) and writes incidents/<case_id>-<UTC ts>.json with the step, tool, redacted parameters, error, redacted
traceback, the partial trace and the dataset version, and prints one clear line. It never writes cases/<id>.json:
the caller writes the answer only when outcome.answer is not None (and exits 2 on an incident).

A tool failure that some code path caught and ignored still ends the run: the runner rejects any answer whose trace
holds a failed tool call, so a swallowed exception can never turn into a complete-looking result.
"""
import json
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kavach.config import CASES_DIR, ROOT
from kavach.harness import bounds, faults
from kavach.harness.redact import redact

INCIDENTS = ROOT / "incidents"           # tests point KAVACH_INCIDENTS_DIR (or this attribute) at a temp folder
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
TRIGGERS = ("risk_score", "customer_report", "analyst_request")
REQUIRED = ("case_id", "trigger_type", "flagged_txn_id", "card_id", "customer_id")


@dataclass
class Outcome:
    case_id: str
    status: str                       # "ok" | "incident"
    answer: dict | None
    trace: dict = field(default_factory=dict)
    incident: dict | None = None
    incident_path: str | None = None
    rerun: dict | None = None         # comparison with an earlier answer for the same case, when one exists


class InvalidInput(faults.HarnessError):
    kind = "invalid_input"


def incidents_dir() -> Path:
    p = os.environ.get("KAVACH_INCIDENTS_DIR", "").strip()
    return Path(p) if p else Path(INCIDENTS)


def decisions(ans: dict) -> dict:
    """The decision-bearing fields of an answer (what must be identical when the same case runs twice)."""
    c = ans["case"]
    return {
        "verdict": c["verdict"], "p": c["fraud_probability"], "pattern": c["pattern"], "affected": list(c["affected_txn_ids"]),
        "first": c["first_suspicious_txn_id"], "exposure": c["exposure_usd"], "connected": list(c["connected_card_ids"]),
        "devices": list(c["connected_device_profiles"]),
        "initial": [(x["action"], x["route"]) for x in ans["next_best_actions"]["initial"]],
        "final": [(x["action"], x["route"]) for x in ans["next_best_actions"]["final"]], "sar": ans["sar"]["file"],
    }


def compare(a: dict, b: dict) -> dict:
    da, db = decisions(a), decisions(b)
    changed = sorted(k for k in da if da[k] != db.get(k))
    return {"identical": not changed, "changed": changed}


def validate_case_row(row) -> None:
    if not isinstance(row, dict):
        raise InvalidInput("case row is not an object")
    missing = [k for k in REQUIRED if row.get(k) in (None, "")]
    if missing:
        raise InvalidInput(f"case row is missing {missing}")
    if not CASE_ID_RE.match(str(row["case_id"])):
        raise InvalidInput("case_id must be 1 to 32 letters, digits, '-' or '_'")
    if row["trigger_type"] not in TRIGGERS:
        raise InvalidInput(f"trigger_type must be one of {list(TRIGGERS)}")
    try:
        int(float(str(row["flagged_txn_id"])))
    except ValueError:
        raise InvalidInput("flagged_txn_id is not a transaction id") from None


def connect_graph(backend: str = "duck", persist: bool = False):
    """The graph connection a run needs, or None. Fail closed: when the graph is needed (tg/mcp lane or --persist) and
    cannot be reached, raise GraphDown instead of carrying on with written_to_graph=false."""
    if backend not in ("tg", "mcp") and not persist:
        return None
    faults.check_graph()
    try:
        from kavach.graph.client import connect
        return connect()
    except Exception as e:  # noqa: BLE001
        raise faults.GraphDown(0, "graph.connect", {"backend": backend}, e) from e


def _dataset_version() -> dict:
    try:
        from kavach import duckq
        return duckq.dataset_version()
    except Exception as e:  # noqa: BLE001  (the incident must still be written)
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _casefile_event(case_id: str, kind: str, detail: dict) -> None:
    if os.environ.get("KAVACH_CASEFILE_EVENTS", "1") == "0":
        return
    try:
        from kavach import casefile
    except ImportError:
        return
    try:
        casefile.append(case_id, kind, detail)
    except Exception as e:  # noqa: BLE001  (a casefile problem must never hide the incident itself)
        print(f"  casefile event {kind} not recorded ({type(e).__name__})", flush=True)


def _write_incident(case_id: str, exc: BaseException, backend: str, llm, t_start: float) -> tuple[dict, Path]:
    kind = getattr(exc, "kind", "unexpected_error")
    info = exc.to_dict() if isinstance(exc, faults.HarnessError) else {"kind": kind, "error": f"{type(exc).__name__}: {exc}"}
    partial = getattr(exc, "partial_trace", None)
    if partial is None:
        partial = bounds.partial_trace()
    ts = datetime.now(timezone.utc)
    inc = {
        "case_id": case_id, "status": "failed", "kind": kind, "ts": ts.isoformat(timespec="seconds"), "backend": backend,
        "step": info.get("step"), "tool": info.get("tool"), "params": info.get("params", {}),
        "error": info.get("error"), "error_type": type(exc).__name__,
        "cause": (f"{type(exc.cause).__name__}: {exc.cause}" if isinstance(getattr(exc, "cause", None), BaseException) else None),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:],
        "partial_trace": partial, "n_tool_calls_before_failure": sum(1 for t in partial if t.get("status") == "ok"),
        "dataset_version": _dataset_version(), "fault_injected": faults.raw() or None,
        "llm_status": list(getattr(llm, "status_log", []) or []), "bounds": bounds.limits(),
        "elapsed_s": round(time.perf_counter() - t_start, 2),
        "answer_written": False,
    }
    if kind == "bounds_exceeded":
        inc.update({"bound": info.get("what"), "limit": info.get("limit"), "value": info.get("value")})
    inc = redact(inc)
    d = incidents_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{case_id}-{ts:%Y%m%dT%H%M%S%fZ}.json"
    p.write_text(json.dumps(inc, indent=2, default=str, ensure_ascii=True), encoding="utf-8")
    return inc, p


def _previous_answer(case_id: str, previous) -> tuple[dict | None, str | None]:
    if isinstance(previous, dict):
        return previous, "given"
    p = Path(previous) if previous else CASES_DIR / f"{case_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")), str(p)
        except (OSError, json.JSONDecodeError):
            return None, None
    return None, None


def run_case(case_row: dict, backend: str = "duck", llm=None, graph_conn=None, verbose: bool = False,
             persist: bool = False, previous=None, quiet: bool = False) -> Outcome:
    """Investigate one case-pack row inside the harness. See the module docstring."""
    t_start = time.perf_counter()
    case_id = str(case_row.get("case_id", "")) if isinstance(case_row, dict) else ""
    safe_id = case_id if CASE_ID_RE.match(case_id or "") else "invalid-case"
    bounds.begin_case(safe_id)
    if llm is None:
        from kavach.llm import Llm
        llm = Llm()
    if hasattr(llm, "begin_case"):
        llm.begin_case(safe_id)
    try:
        validate_case_row(case_row)
        faults.check_graph()
        if graph_conn is None and persist:
            graph_conn = connect_graph(backend, persist)
        from kavach.agent import investigate
        ans, trace = investigate(case_row, backend=backend, llm=llm, graph_conn=graph_conn, verbose=verbose)
        failed = [t for t in (trace or {}).get("tools", []) if t.get("status") == "failed"]
        if failed:
            f = failed[0]
            raise faults.ToolFailure(f.get("step"), f.get("tool"), f.get("params"),
                                     f"a failed tool call was caught inside the agent and ignored ({f.get('error')}); "
                                     "the result would be incomplete", f.get("attempts", 1), trace.get("tools"))
        trace = dict(trace or {})
        trace["harness"] = {"status": "ok", "backend": backend, "tool_calls": ans.get("tool_calls"),
                            "llm_status": list(getattr(llm, "status_log", []) or []), "bounds": bounds.limits(),
                            "fault_injected": faults.raw() or None}
        out = Outcome(case_id, "ok", ans, redact(trace))
        prev, where = _previous_answer(case_id, previous)
        if prev is not None:
            try:
                out.rerun = {"previous": where, **compare(prev, ans)}
            except (KeyError, TypeError):
                out.rerun = None
            if out.rerun is not None:
                _casefile_event(case_id, "rerun", {"previous": where, **out.rerun})
        return out
    except Exception as exc:  # noqa: BLE001  (every failure ends in an incident; nothing is swallowed)
        inc, p = _write_incident(safe_id, exc, backend, llm, t_start)
        try:
            shown = p.relative_to(ROOT)
        except ValueError:
            shown = p
        msg = f"INCIDENT {safe_id}: {inc['kind']}: {inc['error']}. No answer was written. Record: {shown}"
        if not quiet:
            print(msg, flush=True)
        _casefile_event(safe_id, "failed", {"kind": inc["kind"], "error": inc["error"], "incident": str(shown)})
        trace = {"harness": {"status": "incident", "kind": inc["kind"], "incident": str(p)}, "tools": inc["partial_trace"]}
        return Outcome(safe_id, "incident", None, trace, inc, str(p))
    finally:
        bounds.end_case()
