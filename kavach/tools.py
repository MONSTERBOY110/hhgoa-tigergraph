"""Tool registry: every graph or retrieval call goes through here, is counted, timed and traced.

Backend "duck" serves the calls from DuckDB (kavach.duckq); backend "tg" serves the
same names from installed GSQL queries on TigerGraph via pyTigerGraph (kavach.graph.client);
backend "mcp" runs the same installed queries through the TigerGraph MCP server. Both return
the same shapes, so detectors never know which lane answered.
"""
import time
from dataclasses import dataclass, field

import pandas as pd

TOOL_NAMES = (
    "txn_detail", "card_history", "card_window", "customer_cards", "card_profile", "device_neighbors",
    "device_txns", "device_popularity", "region_activity", "email_neighbors", "holder_cards",
    "closed_cases_for", "recurring_check", "closed_case", "similar_cases",
)


def _fmt(v) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(str(x) for x in list(v)[:5]) + (",..." if len(v) > 5 else "") + "]"
    return str(v)


@dataclass
class ToolRegistry:
    backend: str = "duck"
    calls: int = 0
    trace: list = field(default_factory=list)
    step: int = 0
    _impl: object = None

    def __post_init__(self):
        self._ext = None
        if self.backend == "duck":
            from kavach import duckq, duckq_ext
            self._impl = duckq
            self._ext = duckq_ext            # Round 2 tools without a duckq equivalent (typologies, grow, dq)
        elif self.backend == "tg":
            from kavach.graph.client import TgTools
            self._impl = TgTools()
        elif self.backend == "mcp":
            from kavach.graph.mcp_client import McpTools
            self._impl = McpTools()
        else:
            raise ValueError(self.backend)
        self._extra = {}
        from kavach.graph import fusion
        fusion.register(self)          # fused_precedent + ring_component on every lane; attest re-runs find them too

    def register(self, name: str, fn) -> None:
        self._extra[name] = fn

    def call(self, name: str, **kw):
        """Run one tool, fail closed. Bounded (kavach.harness.bounds), retried once on a transient error, then a
        ToolFailure: an exception never becomes an empty result. Every call, failed or not, lands in the trace with
        its duration, attempts and a result digest (ids that came back, sha1 of them)."""
        from kavach.harness import bounds, faults
        from kavach.harness.digest import result_digest

        bounds.note_registry(self)
        st = self.__dict__.setdefault("_harness", {"per_tool": {}, "n": 0, "t0": time.monotonic()})
        params = {k: _fmt(v) for k, v in kw.items()}
        lim = bounds.limits()
        if self.calls + 1 > lim["tool_calls"]:
            raise faults.BoundsExceeded("tool calls per case", lim["tool_calls"], self.calls + 1, self.step, name, list(self.trace))
        started = bounds.case_started() or st["t0"]
        if time.monotonic() - started > lim["case_seconds"]:
            raise faults.BoundsExceeded("seconds per case", lim["case_seconds"], round(time.monotonic() - started, 1),
                                        self.step, name, list(self.trace))
        st["n"] += 1
        st["per_tool"][name] = st["per_tool"].get(name, 0) + 1
        ext = getattr(self, "_ext", None)
        fn = self._extra.get(name) or getattr(self._impl, name, None) or (getattr(ext, name, None) if ext is not None else None)
        interrupt = None
        if self.backend == "duck":
            interrupt = lambda: __import__("kavach.duckq", fromlist=["con"]).con().interrupt()  # noqa: E731
        t0, err, attempts, out = time.perf_counter(), None, 0, None
        for attempt in (1, 2):
            attempts = attempt
            wd = bounds.Watchdog(lim["tool_seconds"], interrupt)
            try:
                if fn is None:
                    raise LookupError(f"unknown tool {name!r} on backend {self.backend}")
                inj = faults.tool_fault(name, st["per_tool"][name], st["n"], attempt)
                if inj is not None:
                    raise inj
                with wd:
                    out = fn(**kw)
                err = None
                break
            except Exception as e:  # noqa: BLE001  (classified below; nothing is swallowed)
                if wd.fired:
                    self.trace.append({"step": self.step, "tool": name, "params": params, "ms": round((time.perf_counter() - t0) * 1000, 1),
                                       "n_rows": 0, "attempts": attempt, "status": "failed", "error": "interrupted: time bound"})
                    raise faults.BoundsExceeded("seconds per tool call", lim["tool_seconds"], round(time.perf_counter() - t0, 1),
                                                self.step, name, list(self.trace)) from e
                err = e
                if attempt == 1 and faults.is_transient(e):
                    time.sleep(0 if isinstance(e, faults.InjectedFault) else faults.retry_delay())
                    continue
                break
        ms = (time.perf_counter() - t0) * 1000
        if err is not None:
            self.trace.append({"step": self.step, "tool": name, "params": params, "ms": round(ms, 1), "n_rows": 0,
                               "attempts": attempts, "status": "failed", "error": f"{type(err).__name__}: {str(err)[:300]}"})
            raise faults.ToolFailure(self.step, name, params, err, attempts, list(self.trace)) from err
        self.calls += 1
        dg = result_digest(out)
        self.trace.append({"step": self.step, "tool": name, "params": params, "ms": round(ms, 1), "n_rows": dg["n_rows"],
                           "attempts": attempts, "status": "ok", "result_digest": dg})
        if ms / 1000 > lim["tool_seconds"]:
            raise faults.BoundsExceeded("seconds per tool call", lim["tool_seconds"], round(ms / 1000, 1), self.step, name, list(self.trace))
        return out

    @staticmethod
    def ref(name: str, **kw) -> str:
        """README-style evidence ref, e.g. query:card_window(card_id=C00377-K1, hours=2)."""
        return f"query:{name}(" + ", ".join(f"{k}={_fmt(v)}" for k, v in kw.items()) + ")"
