"""Hard limits per case. Exceeding a tool or time limit stops the run with BoundsExceeded (an incident, no answer).
Exceeding an LLM limit ends in the deterministic template instead (prose is non-decisional), recorded in
Llm.status_log, so it never stops a run.

Defaults (env overrides for drills and tests, read at call time):
    KAVACH_MAX_TOOL_CALLS     80    tool calls per case (per ToolRegistry)
    KAVACH_MAX_TOOL_SECONDS   90    seconds per tool call (DuckDB lane: interrupted by a watchdog; other lanes: checked on return)
    KAVACH_MAX_CASE_SECONDS   900   seconds of wall clock per case (checked before every tool call)
    KAVACH_MAX_LLM_CALLS      4     prose-writing calls (Llm.write) per case
    KAVACH_MAX_LLM_ATTEMPTS   4     completions per writing call (garbage and transient errors are retried)
"""
import os
import threading
import time

DEFAULTS = {"tool_calls": 80, "tool_seconds": 90.0, "case_seconds": 900.0, "llm_calls": 4, "llm_attempts": 4}
_ENV = {"tool_calls": "KAVACH_MAX_TOOL_CALLS", "tool_seconds": "KAVACH_MAX_TOOL_SECONDS", "case_seconds": "KAVACH_MAX_CASE_SECONDS",
        "llm_calls": "KAVACH_MAX_LLM_CALLS", "llm_attempts": "KAVACH_MAX_LLM_ATTEMPTS"}


def limit(name: str):
    d = DEFAULTS[name]
    v = os.environ.get(_ENV[name], "").strip()
    if not v:
        return d
    try:
        return int(float(v)) if isinstance(d, int) else float(v)
    except ValueError:
        return d


def limits() -> dict:
    return {k: limit(k) for k in DEFAULTS}


# ---------------------------------------------------------------- per-case context (thread-local, so the server's
# worker thread and the CLI never share it)
_local = threading.local()


def begin_case(case_id: str) -> None:
    _local.case_id = case_id
    _local.t0 = time.monotonic()
    _local.registries = []


def end_case() -> None:
    _local.case_id, _local.t0, _local.registries = None, None, []


def case_started() -> float | None:
    return getattr(_local, "t0", None)


def note_registry(reg) -> None:
    regs = getattr(_local, "registries", None)
    if regs is None:
        _local.registries = regs = []
    if not any(r is reg for r in regs):
        regs.append(reg)


def registries() -> list:
    return list(getattr(_local, "registries", None) or [])


def partial_trace() -> list:
    out = []
    for r in registries():
        out += list(getattr(r, "trace", []))
    return out


class Watchdog:
    """Calls interrupt() (DuckDB: connection.interrupt) when a tool call runs past its time bound."""

    def __init__(self, seconds: float, interrupt=None):
        self.fired = False
        self._timer = None
        if interrupt is not None and seconds and seconds > 0:
            def fire():
                self.fired = True
                try:
                    interrupt()
                except Exception:
                    pass
            self._timer = threading.Timer(seconds, fire)
            self._timer.daemon = True

    def __enter__(self):
        if self._timer:
            self._timer.start()
        return self

    def __exit__(self, *exc):
        if self._timer:
            self._timer.cancel()
        return False
