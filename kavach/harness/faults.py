"""Typed failures and fault injection.

Failures are exceptions with enough context to write an incident: which step, which tool, which parameters,
what went wrong. The runner turns them into incidents/<case>-<ts>.json and no answer is written.

Fault injection (tests and live drills) through the env var KAVACH_FAULT, read at call time. Several specs may
be combined with commas:
    tool:<name>:<nth>            the nth call of tool <name> fails with a simulated connection error, on every
                                 attempt (so the retry fails too and the run fails closed)
    tool:<name>:<nth>:transient  same, but only the first attempt fails (shows the retry recovering)
    tool:any:<nth>               the nth tool call of the case, whatever the tool
    llm:timeout | llm:garbage | llm:empty
                                 the model times out, returns a JSON-looking blob with a foreign id, or returns ""
    graph:down                   the graph store is unreachable: connect fails and every tool call fails
"""
import os


class HarnessError(Exception):
    """Base class: anything that stops a run and must end in an incident rather than an answer."""

    kind = "harness_error"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "error": str(self)}


class ToolFailure(HarnessError):
    kind = "tool_failure"

    def __init__(self, step, tool: str, params: dict | None, cause, attempts: int = 1, partial_trace: list | None = None):
        self.step, self.tool, self.params, self.cause, self.attempts = step, tool, dict(params or {}), cause, attempts
        self.partial_trace = partial_trace
        why = f"{type(cause).__name__}: {cause}" if isinstance(cause, BaseException) else str(cause)
        super().__init__(f"tool {tool} failed at step {step} after {attempts} attempt(s) ({why})")

    def to_dict(self) -> dict:
        c = self.cause
        return {"kind": self.kind, "step": self.step, "tool": self.tool, "params": self.params, "attempts": self.attempts,
                "error": str(self), "cause_type": type(c).__name__ if isinstance(c, BaseException) else "str"}


class GraphDown(ToolFailure):
    kind = "graph_down"


class IndexUnavailable(ToolFailure):
    """The DuckDB index (lane A data, dataset version, case pack) is missing or unreadable."""

    kind = "index_unavailable"


class ModelFailure(HarnessError):
    """The model failed. Llm.write never raises it (prose falls back to the template); it exists for callers that
    need the model and must stop when it is unusable."""

    kind = "model_failure"


class BoundsExceeded(HarnessError):
    kind = "bounds_exceeded"

    def __init__(self, what: str, limit, value, step=None, tool: str | None = None, partial_trace: list | None = None):
        self.what, self.limit, self.value, self.step, self.tool = what, limit, value, step, tool
        self.partial_trace = partial_trace
        super().__init__(f"bound exceeded: {what} = {value} over the limit {limit}" + (f" (step {step}, tool {tool})" if tool else ""))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "what": self.what, "limit": self.limit, "value": self.value, "step": self.step,
                "tool": self.tool, "error": str(self)}


class Forbidden(HarnessError):
    """An approval attempted by a role that may not approve that action (or by an unknown token)."""

    kind = "forbidden"


class InjectedFault(ConnectionError):
    """A simulated dependency failure (KAVACH_FAULT). A ConnectionError, so the registry treats it as transient."""


TRANSIENT_TYPES = (ConnectionError, TimeoutError, OSError)


def is_transient(e: BaseException) -> bool:
    """Worth one retry: connection resets, timeouts, OS-level I/O errors, or a message that says so."""
    if isinstance(e, TRANSIENT_TYPES):
        return True
    text = f"{type(e).__name__} {e}".lower()
    return "timeout" in text or "timed out" in text or "connection" in text


def retry_delay() -> float:
    try:
        return max(0.0, float(os.environ.get("KAVACH_RETRY_DELAY", "0.5")))
    except ValueError:
        return 0.5


# ---------------------------------------------------------------- fault specs
def raw() -> str:
    return os.environ.get("KAVACH_FAULT", "").strip()


def specs() -> list[str]:
    return [s.strip().lower() for s in raw().split(",") if s.strip()]


def active(spec: str) -> bool:
    return spec.lower() in specs()


def graph_down() -> bool:
    return active("graph:down")


def llm_fault() -> str | None:
    """'timeout' | 'garbage' | 'empty' | None"""
    for s in specs():
        if s.startswith("llm:"):
            return s.split(":", 1)[1]
    return None


def tool_fault(name: str, nth_of_tool: int, nth_overall: int, attempt: int) -> InjectedFault | None:
    """The fault to raise for this attempt of this call, or None. nth_* are 1-based logical call numbers."""
    if graph_down():
        return InjectedFault("graph store unreachable (injected KAVACH_FAULT=graph:down)")
    for s in specs():
        parts = s.split(":")
        if len(parts) < 3 or parts[0] != "tool":
            continue
        target, transient = parts[1], len(parts) > 3 and parts[3] == "transient"
        try:
            nth = int(parts[2])
        except ValueError:
            continue
        hit = (target == "any" and nth_overall == nth) or (target == name.lower() and nth_of_tool == nth)
        if hit and (not transient or attempt == 1):
            return InjectedFault(f"connection reset by peer (injected KAVACH_FAULT={s}, attempt {attempt})")
    return None


def check_graph() -> None:
    """Call before connecting to the graph store; raises GraphDown when the fault is injected."""
    if graph_down():
        raise GraphDown(0, "graph.connect", {}, InjectedFault("graph store unreachable (injected KAVACH_FAULT=graph:down)"))
