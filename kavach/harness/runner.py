"""run_case(): one investigation inside the harness. Fail closed.

On success it returns Outcome(status="ok", answer=..., trace=...). On any failure (a tool that failed twice, a bound
exceeded, the graph store down, invalid input, any unexpected exception) it returns Outcome(status="incident",
answer=None) and writes incidents/<case_id>-<UTC ts>.json with the step, tool, redacted parameters, error, redacted
traceback, the partial trace and the dataset version, and prints one clear line. It never writes cases/<id>.json:
the caller writes the answer only when outcome.answer is not None (and exits 2 on an incident).

A tool failure that some code path caught and ignored still ends the run: the runner rejects any answer whose trace
holds a failed tool call, so a swallowed exception can never turn into a complete-looking result.

Failures before any case runs fail closed the same way: connect_graph() and case_pack_rows() write an incident and
raise (graph unreachable, DuckDB index missing), so the caller prints one line and exits 2.

The model is prose-only. When its prose is not used (no key configured, timeout, garbage, empty) the trace records
why (harness.llm_mode, harness.llm_status) and one NOTE line is printed; with KAVACH_LLM_REQUIRED=1 the same
situation is an incident (kind model_failure) and no answer is written.

Casefile events from the runner (failed, rerun) are written only for recorded runs (KAVACH_RECORD=1, as the agent's
own events), so a --no-write dry run leaves nothing in data/casefiles/.
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
REQUIRED = ("case_id", "trigger_type", "flagged_txn_id", "card_id", "customer_id", "opened_at")
TXN_ID_RE = re.compile(r"^\d{1,12}(?:\.0+)?$")        # a whole, positive transaction id ("3660153", or "3660153.0" from a float column)
MAX_TRIGGER_TEXT = 10_000                              # characters; organizer alert texts are under 300
MAX_ID_LEN = 64


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


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, float) and v != v)


def validate_case_row(row) -> None:
    """Reject a malformed alert before anything runs (InvalidInput: a 422 on the server, an incident in run_case).
    Only structure is checked here; the free text stays data and is handled by kavach.intake."""
    if not isinstance(row, dict):
        raise InvalidInput("case row is not an object")
    missing = [k for k in REQUIRED if _blank(row.get(k))]
    if missing:
        raise InvalidInput(f"case row is missing {missing}")
    if not CASE_ID_RE.match(str(row["case_id"])):
        raise InvalidInput("case_id must be 1 to 32 letters, digits, '-' or '_'")
    if not isinstance(row["trigger_type"], str) or row["trigger_type"] not in TRIGGERS:
        raise InvalidInput(f"trigger_type must be one of {list(TRIGGERS)}")
    tid = row["flagged_txn_id"]
    if isinstance(tid, bool):
        tid_s = ""
    elif isinstance(tid, float):                      # NaN was rejected as blank above
        tid_s = f"{tid:.1f}" if abs(tid) < 1e13 else ""
    else:
        tid_s = str(tid).strip()
    if not TXN_ID_RE.match(tid_s):
        raise InvalidInput("flagged_txn_id must be a whole, positive transaction id (digits only)")
    for k in ("card_id", "customer_id"):
        v = row[k]
        if not isinstance(v, (str, int)) or isinstance(v, bool) or len(str(v)) > MAX_ID_LEN or not str(v).isprintable():
            raise InvalidInput(f"{k} must be a printable id of at most {MAX_ID_LEN} characters")
    try:
        import pandas as pd
        ok = not isinstance(row["opened_at"], bool) and not pd.isna(pd.Timestamp(row["opened_at"]))
    except (TypeError, ValueError, OverflowError):
        ok = False
    if not ok:
        raise InvalidInput("opened_at must be a timestamp (YYYY-MM-DD HH:MM:SS)")
    rs = row.get("risk_score")
    if not _blank(rs):
        try:
            score = float(rs) if not isinstance(rs, bool) else float("nan")
        except (TypeError, ValueError):
            score = float("nan")
        if not 0.0 <= score <= 1.0:   # also false for NaN and infinities
            raise InvalidInput("risk_score must be a number between 0 and 1, or empty")
    tt = row.get("trigger_text")
    if not _blank(tt):
        if not isinstance(tt, str):
            raise InvalidInput("trigger_text must be a string")
        if len(tt) > MAX_TRIGGER_TEXT:
            raise InvalidInput(f"trigger_text is {len(tt):,} characters; the limit is {MAX_TRIGGER_TEXT:,} "
                               "(organizer alert texts are under 300)")


def _shown(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _preflight_incident(label: str | None, exc: faults.HarnessError, backend: str, t0: float,
                        case_event: bool = False) -> faults.HarnessError:
    """A dependency failed before any case ran: write the incident, put its path in the error message, return the
    exception for the caller to raise. One clear line: '<error>. No answer was written. Record: incidents/...'."""
    safe = label if CASE_ID_RE.match(label or "") else "preflight"
    inc, p = _write_incident(safe, exc, backend, None, t0)
    exc.incident_path = str(p)
    exc.args = (f"{exc.args[0] if exc.args else exc}. No answer was written. Record: {_shown(p)}",)
    if case_event:
        _casefile_event(safe, "failed", f"{inc['kind']}: {inc['error']}"[:600], {"kind": inc["kind"], "incident": _shown(p)})
    return exc


def connect_graph(backend: str = "duck", persist: bool = False, case_id: str | None = None, write_incident: bool = True):
    """The graph connection a run needs, or None. Fail closed: when the graph is needed (tg/mcp lane or --persist) and
    cannot be reached, raise GraphDown instead of carrying on with written_to_graph=false. The incident is written
    here (named after case_id, else 'graph') unless the caller writes its own (run_case passes write_incident=False)."""
    if backend not in ("tg", "mcp") and not persist:
        return None
    t0 = time.perf_counter()
    try:
        faults.check_graph()
        from kavach.graph.client import connect
        return connect()
    except faults.GraphDown as e:
        exc = e
    except Exception as e:  # noqa: BLE001
        exc = faults.GraphDown(0, "graph.connect", {"backend": backend, "persist": persist}, e)
        exc.__cause__ = e
    if write_incident:
        exc = _preflight_incident(case_id or "graph", exc, backend, t0, case_event=bool(case_id))
    raise exc


def case_pack_rows(round: int | None = None, label: str | None = None) -> list[dict]:
    """The case-pack rows from the DuckDB index (kavach.duckq.case_pack), fail closed: when the index is missing or
    unreadable, write an incident (kind index_unavailable) and raise faults.IndexUnavailable instead of a traceback."""
    t0 = time.perf_counter()
    try:
        from kavach import duckq
        return duckq.case_pack(round).to_dict("records")
    except Exception as e:  # noqa: BLE001
        from kavach.data import duck
        exc = faults.IndexUnavailable(0, "duckdb.case_pack", {"index": duck.DUCK_PATH.name, "round": round}, e)
        exc.__cause__ = e
        raise _preflight_incident(label or "index", exc, "duck", t0) from e


def _dataset_version() -> dict:
    try:
        from kavach import duckq
        return duckq.dataset_version()
    except Exception as e:  # noqa: BLE001  (the incident must still be written)
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _casefile_event(case_id: str, kind: str, detail: str, extra: dict | None = None) -> None:
    """Runner events (failed, rerun) follow the agent's rule: only recorded runs (KAVACH_RECORD=1) write the case file,
    so a --no-write dry run, a test or the in-memory server leaves data/casefiles/ untouched."""
    if os.environ.get("KAVACH_CASEFILE_EVENTS", "1") == "0" or os.environ.get("KAVACH_RECORD") != "1":
        return
    try:
        from kavach import casefile
    except ImportError:
        return
    try:
        casefile.append(case_id, kind, detail, extra=extra or {})
    except Exception as e:  # noqa: BLE001  (a casefile problem must never hide the incident itself)
        print(f"  casefile event {kind} not recorded ({type(e).__name__})", flush=True)


UNKNOWN_INPUT_RE = re.compile(r"\b(?:does not exist|not found on its own card)\b")


def _kind(exc: BaseException) -> str:
    """Incident kind. An alert naming a transaction the dataset does not hold (raised by kavach.context as a ValueError)
    is invalid input, not an unexpected error."""
    k = getattr(exc, "kind", None)
    if k:
        return k
    if isinstance(exc, (ValueError, KeyError)) and UNKNOWN_INPUT_RE.search(str(exc)):
        return "invalid_input"
    return "unexpected_error"


def llm_mode(llm) -> str:
    """Why prose came from the model or the template, for the trace and the console."""
    if getattr(llm, "enabled", False):
        return "enabled"
    if os.environ.get("KAVACH_NO_LLM") == "1":
        return "disabled: KAVACH_NO_LLM=1 (--no-llm)"
    if not getattr(llm, "api_key", ""):
        return "disabled: no LLM_API_KEY configured"
    return "disabled"


def _llm_problem(mode: str, log: list[dict]) -> str | None:
    """What went wrong with the model in this case, or None. A deliberate --no-llm is not a problem."""
    fb = [s for s in log if str(s.get("status", "")).startswith("fallback")]
    if fb:
        why = sorted({str(s["status"]).split("fallback: ", 1)[-1] for s in fb})
        return f"model output not used for {len(fb)} of {len(log)} prose piece(s) ({'; '.join(why)[:200]})"
    if mode.startswith("disabled: no LLM_API_KEY"):
        return "the LLM is disabled because no LLM_API_KEY is configured"
    return None


def _llm_required() -> bool:
    return os.environ.get("KAVACH_LLM_REQUIRED", "").strip() == "1"


def _write_incident(case_id: str, exc: BaseException, backend: str, llm, t_start: float) -> tuple[dict, Path]:
    kind = _kind(exc)
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
        mode = llm_mode(llm)
        if _llm_required() and mode.startswith("disabled: no LLM_API_KEY"):
            raise faults.ModelFailure("the model is required (KAVACH_LLM_REQUIRED=1) but no LLM_API_KEY is configured; "
                                      "nothing was investigated")
        if graph_conn is None and persist:
            graph_conn = connect_graph(backend, persist, write_incident=False)
        from kavach.agent import investigate
        ans, trace = investigate(case_row, backend=backend, llm=llm, graph_conn=graph_conn, verbose=verbose)
        failed = [t for t in (trace or {}).get("tools", []) if t.get("status") == "failed"]
        if failed:
            f = failed[0]
            raise faults.ToolFailure(f.get("step"), f.get("tool"), f.get("params"),
                                     f"a failed tool call was caught inside the agent and ignored ({f.get('error')}); "
                                     "the result would be incomplete", f.get("attempts", 1), trace.get("tools"))
        llm_log = list(getattr(llm, "status_log", []) or [])
        problem = _llm_problem(mode, llm_log)
        if problem and _llm_required():
            graph_note = " (the case vertex was already written to the graph)" if ans.get("case", {}).get("written_to_graph") else ""
            raise faults.ModelFailure(f"the model is required (KAVACH_LLM_REQUIRED=1) but {problem}; template prose is not "
                                      f"accepted in this mode{graph_note}")
        trace = dict(trace or {})
        trace["harness"] = {"status": "ok", "backend": backend, "tool_calls": ans.get("tool_calls"),
                            "llm_mode": mode, "llm_problem": problem, "llm_status": llm_log, "bounds": bounds.limits(),
                            "fault_injected": faults.raw() or None}
        # The answer goes through the same masking as the trace: alert free text quoted in an evidence note may carry an
        # email address or a pasted credential. Ids, enums and numbers are never touched (on the 38 pack cases redact is
        # a no-op: tests/test_drills.py).
        ans = redact(ans)
        out = Outcome(case_id, "ok", ans, redact(trace))
        if problem and not quiet:
            print(f"NOTE {case_id}: {problem}; the deterministic template was written instead. Decisions are unaffected "
                  "(prose is non-decisional); recorded in the trace under harness.llm_status.", flush=True)
        prev, where = _previous_answer(case_id, previous)
        if prev is not None:
            try:
                out.rerun = {"previous": where, **compare(prev, ans)}
            except (KeyError, TypeError):
                out.rerun = None
            if out.rerun is not None:
                what = "decisions identical to the previous answer" if out.rerun["identical"] else \
                    f"decisions changed against the previous answer: {', '.join(out.rerun['changed'])}"
                _casefile_event(case_id, "rerun", f"same case run again; {what}",
                                {"previous": _shown(Path(where)) if where and where != "given" else where, **out.rerun})
        return out
    except Exception as exc:  # noqa: BLE001  (every failure ends in an incident; nothing is swallowed)
        inc, p = _write_incident(safe_id, exc, backend, llm, t_start)
        shown = _shown(p)
        msg = f"INCIDENT {safe_id}: {inc['kind']}: {inc['error']}. No answer was written. Record: {shown}"
        if not quiet:
            print(msg, flush=True)
        _casefile_event(safe_id, "failed", f"{inc['kind']}: {inc['error']}"[:600], {"kind": inc["kind"], "incident": shown})
        trace = {"harness": {"status": "incident", "kind": inc["kind"], "incident": str(p)}, "tools": inc["partial_trace"]}
        return Outcome(safe_id, "incident", None, trace, inc, str(p))
    finally:
        bounds.end_case()
