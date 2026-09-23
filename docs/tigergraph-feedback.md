# TigerGraph experience notes

Running notes kept while building, for the submission form feedback field. Updated as we go.

## What worked
- Loading 590,742 transactions, 3.3M edges and 5,565 closed cases through a GSQL loading job posted in 50k-line chunks took about 6 minutes on the free tier, with every line valid on the first run.
- All 17 installed queries compiled on the first attempt and answer in about 100 ms each (card history 370 ms); installing them took 31 seconds.
- TigerVector: adding 384-d vector attributes with a schema change job and `vectorSearch()` worked as documented; a self-check (a case's own note is its nearest neighbour) passed immediately.
- The Savanna workspace on TigerGraph 4.2.5 came up quickly, and `/api/version` reports the exact build, which confirmed vector support (TigerVector needs 4.2 or later).
- The GSQL model fits investigation work naturally. "What else happened on this device?" is a two-hop traversal (Transaction to DeviceProfile to Transaction to Card) with accumulators, and it is exactly the question that solves the hardest cases.
- Installed queries give a clean tool boundary for an agent. Each tool is one query with typed parameters, so the same agent code runs against TigerGraph or our analysis copy.
- The TigerGraph MCP server (`tigergraph-mcp`) is easy to install (`pip install tigergraph-mcp`), its `run_installed_query` tool maps one-to-one to our tools, and its responses carry a machine-readable JSON block.

## What was slow or confusing
- Signing up with Google SSO leaves you without a database password, and it is not obvious that the answer is the "Database Secrets" page in the left menu (create a secret for the workspace, then use it as the GSQL secret). Once found, it took one minute.
- The REST++ `echo` endpoint answers without auth, while `version` and every data endpoint need a token, so a quick connectivity check is misleading.
- The MCP server defaults to ports 9000/14240 (`TG_RESTPP_PORT`, `TG_GS_PORT`), while Savanna serves everything on 443; the README does not say this. Setting both to 443 plus `TG_SECRET` worked first time.

- `proxy` is a reserved word in vertex attribute lists, but only when another attribute follows it: `(..., proxy STRING)` at the end is accepted, `(..., proxy STRING, card_id STRING)` fails with "Encountered ',' ... expecting ')'" pointing at the preceding comma. We renamed it to `proxy_type`. A clearer error ("proxy is a reserved word") would have saved a bisecting session.
- A fresh Savanna workspace can arrive with a sample solution already installed whose global vertex types (for example `Card`) collide with a natural schema for the task, so we created our graph with graph-local types in a schema change job.

## What we wish existed
- A "Connect from Python" snippet in the Savanna console that works for SSO users end to end (create a DB user or secret, host, port 443, token).
- A bulk vector loader in the loading-job language, the same as for scalar attributes, instead of upserting lists through REST.
- A dry-run or lint mode for GSQL queries that reports syntax errors without installing, so an agent can validate generated queries cheaply.
