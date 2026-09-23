# TigerGraph experience notes

Running notes kept while building, for the submission form feedback field. Updated as we go.

## What worked
- The Savanna workspace on TigerGraph 4.2.5 came up quickly, and `/api/version` reports the exact build, which confirmed vector support (TigerVector needs 4.2 or later).
- The GSQL model fits investigation work naturally. "What else happened on this device?" is a two-hop traversal (Transaction to DeviceProfile to Transaction to Card) with accumulators, and it is exactly the question that solves the hardest cases.
- Installed queries give a clean tool boundary for an agent. Each tool is one query with typed parameters, so the same agent code runs against TigerGraph or our analysis copy.
- The TigerGraph MCP server (`tigergraph-mcp`) is easy to install (`pip install tigergraph-mcp`), its `run_installed_query` tool maps one-to-one to our tools, and its responses carry a machine-readable JSON block.

## What was slow or confusing
- Signing up with Google SSO leaves you without a database password. It took a docs search to learn that API access needs a separate database user, or a secret from the Admin Portal, and the Connect from API panel does not say this.
- The REST++ `echo` endpoint answers without auth, while `version` and every data endpoint need a token, so a quick connectivity check is misleading.
- The MCP server defaults to ports 9000/14240 (`TG_RESTPP_PORT`, `TG_GS_PORT`), while Savanna serves everything on 443; the README does not say which values a Savanna user needs. (Verify on the live workspace.)

## What we wish existed
- A "Connect from Python" snippet in the Savanna console that works for SSO users end to end (create a DB user or secret, host, port 443, token).
- A bulk vector loader in the loading-job language, the same as for scalar attributes, instead of upserting lists through REST.
- A dry-run or lint mode for GSQL queries that reports syntax errors without installing, so an agent can validate generated queries cheaply.
