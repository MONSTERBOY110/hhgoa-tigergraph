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
        if self.backend == "duck":
            from kavach import duckq
            self._impl = duckq
        elif self.backend == "tg":
            from kavach.graph.client import TgTools
            self._impl = TgTools()
        elif self.backend == "mcp":
            from kavach.graph.mcp_client import McpTools
            self._impl = McpTools()
        else:
            raise ValueError(self.backend)
        self._extra = {}

    def register(self, name: str, fn) -> None:
        self._extra[name] = fn

    def call(self, name: str, **kw):
        fn = self._extra.get(name) or getattr(self._impl, name)
        t0 = time.perf_counter()
        out = fn(**kw)
        ms = (time.perf_counter() - t0) * 1000
        self.calls += 1
        n = len(out) if isinstance(out, (pd.DataFrame, list)) else (1 if out else 0)
        self.trace.append({"step": self.step, "tool": name, "params": {k: _fmt(v) for k, v in kw.items()},
                           "ms": round(ms, 1), "n_rows": n})
        return out

    @staticmethod
    def ref(name: str, **kw) -> str:
        """README-style evidence ref, e.g. query:card_window(card_id=C00377-K1, hours=2)."""
        return f"query:{name}(" + ", ".join(f"{k}={_fmt(v)}" for k, v in kw.items()) + ")"
