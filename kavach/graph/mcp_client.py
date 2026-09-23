"""TigerGraph MCP lane: the agent's installed-query tools called through the TigerGraph MCP server.

Starts `tigergraph-mcp` over stdio once, keeps one MCP client session on a background event
loop, and routes every installed-query call through the MCP tool `tigergraph__run_installed_query`.
Everything else (shapes, parsing) is inherited from TgTools, so the agent cannot tell the lanes apart.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import threading
from pathlib import Path

from kavach.config import env
from kavach.graph.client import TgTools

JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


class _McpSession:
    def __init__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        exe = shutil.which("tigergraph-mcp") or str(Path(sys.executable).parent / "tigergraph-mcp")
        host = env("TG_HOST")
        server_env = dict(os.environ, TG_HOST=host, TG_GRAPHNAME=env("TG_GRAPH", "Fraud"), TG_USERNAME=env("TG_USERNAME"),
                          TG_PASSWORD=env("TG_PASSWORD"), TG_SECRET=env("TG_SECRET"), TG_RESTPP_PORT="443", TG_GS_PORT="443")
        self.params = StdioServerParameters(command=exe, args=[], env=server_env)
        self._stdio_client, self._ClientSession = stdio_client, ClientSession
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(90):
            raise RuntimeError("TigerGraph MCP server did not start")
        if getattr(self, "error", None):
            raise self.error

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        try:
            async with self._stdio_client(self.params) as (r, w):
                async with self._ClientSession(r, w) as s:
                    await s.initialize()
                    tools = await s.list_tools()
                    self.tool_names = [t.name for t in tools.tools]
                    self.session = s
                    self.stop = asyncio.Event()
                    self.ready.set()
                    await self.stop.wait()
        except Exception as e:  # surface startup failures to the caller
            self.error = e
            self.ready.set()

    def call(self, tool: str, args: dict, timeout: float = 180):
        fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(tool, args), self.loop)
        res = fut.result(timeout)
        text = "".join(getattr(c, "text", "") for c in res.content)
        m = JSON_BLOCK.search(text)
        payload = json.loads(m.group(1)) if m else {"success": False, "error": text[:500]}
        if not payload.get("success"):
            raise RuntimeError(f"MCP {tool} failed: {payload.get('error') or payload.get('summary')}")
        return payload.get("data", {})


_SESSION = None


def session() -> _McpSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = _McpSession()
    return _SESSION


class McpTools(TgTools):
    """Same tools as TgTools; installed queries go through the TigerGraph MCP server."""

    def __init__(self, conn=None):
        super().__init__(conn)
        self.mcp = session()
        name = "tigergraph__run_installed_query"
        self.tool = name if name in self.mcp.tool_names else next(t for t in self.mcp.tool_names if t.endswith("run_installed_query"))

    def _q(self, name: str, **params):
        data = self.mcp.call(self.tool, {"query_name": name, "params": params, "graph_name": env("TG_GRAPH", "Fraud")})
        return data["result"]
