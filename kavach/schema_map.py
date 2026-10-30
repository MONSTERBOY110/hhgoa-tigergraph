"""Someone else's graph schema (challenge 13, schema form): map another bank's TigerGraph schema (a GSQL DDL file) onto
the canonical fraud-graph model, check every choice against the parsed schema, and score it against an answer key.

    python -m kavach.schema_map round2/bank1_iso20022_payments.gsql            # propose, validate, print the mapping
    python -m kavach.schema_map round2/*.gsql --score                           # also score each file's answer key
    options: --refresh (ignore the cached proposal)  --model <name>  --json <out>

How it works, in order:
 1. parse: only the DDL statements are read (ADD/CREATE VERTEX, ADD/CREATE DIRECTED|UNDIRECTED EDGE with FROM, TO,
    attributes and REVERSE_EDGE). Every comment line is dropped before anything else sees the file, so an answer key
    written in the comments can never leak into the mapping.
 2. propose: the language model (LLM_* settings in .env, temperature 0, JSON only) gets the canonical model below and
    the parsed schema, and proposes for each canonical vertex, edge and attribute: exact, approximate or unmapped, the
    foreign element(s), an optional condition (a role or flag filter) and a reason. The proposal is cached by the
    sha256 of (schema, canonical model, prompt version, model), so a re-run is identical.
 3. validate (deterministic, the model is never trusted): every foreign vertex, attribute and edge must exist; an edge
    path must connect the source type's foreign vertices to the target type's foreign vertices hop by hop in the
    stated direction (a reverse_X hop must be a declared REVERSE_EDGE or an undirected edge); an attribute must sit
    on a mapped vertex or one hop away and its type must fit (amounts numeric, dates DATETIME or a date string, which
    is approximate at best). A failing item is rejected to unmapped with the reason; nothing is guessed silently.
 4. score (--score): the file's "Expected mapping (ground truth)" block, read only now, is compared per canonical item:
    the target is right when both say unmapped or a foreign name we chose appears in the expected spec; the kind
    (exact / approximate / unmapped) is compared separately.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from kavach.config import DATA, env

OUT_DIR = DATA / "schema_map"
PROMPT_VERSION = "2"   # v2: the exact / approximate convention of the mentors' rubric (filters and reverse hops stay exact)
                       # (a v3 that also called payees roles scored worse, 68 vs 72 of 82, and was dropped)

# --------------------------------------------------------------------------------------- the canonical fraud-graph model
# The target model named in the mentors' schema files (vertex, edge and attribute names); meanings written by us.
CANON_VERTICES = {
    "Person": "a customer of this bank: an individual or an organisation that holds accounts here (not an external party)",
    "Account": "an account held at this bank (current, savings, cash or card account)",
    "Transaction": "one executed money movement as booked or settled (a payment, transfer or cash transaction); not a "
                   "payment order or batch, not a return, not an accounting or statement entry, not a message header",
    "Beneficiary": "an external payee or counterparty that receives money and is not a customer of this bank",
    "Device": "a device used to access the bank or to transact (phone, browser, terminal); a payment card is NOT a device",
    "IPAddress": "a network (IP) address used for access",
    "LoginEvent": "one authentication or sign-on event",
    "Card": "a payment card issued on an account",
    "Merchant": "a card acceptor or merchant",
    "InvestigationCase": "a fraud or AML investigation case record with a life (opened, worked, closed)",
}
CANON_EDGES = {
    "OWNS": ("Person", "Account", "the person owns or holds the account"),
    "INITIATED": ("Account", "Transaction", "the account was debited by (initiated) the transaction"),
    "PAYS_TO_ACCOUNT": ("Transaction", "Account", "the transaction credits an account held at this bank"),
    "PAYS": ("Transaction", "Beneficiary", "the transaction pays an external beneficiary"),
    "FROM_DEVICE": ("Transaction", "Device", "the transaction was made from the device"),
    "FROM_IP": ("Transaction", "IPAddress", "the transaction was made from the IP address"),
    "AT": ("Transaction", "Merchant", "the card transaction happened at the merchant"),
    "VIA": ("Transaction", "Card", "the transaction used the card"),
    "HOLDS": ("Account", "Card", "the card is issued on the account"),
    "LOGGED_IN": ("LoginEvent", "Account", "the login gave access to the account"),
    "LOGIN_FROM_DEVICE": ("LoginEvent", "Device", "the login came from the device"),
    "LOGIN_FROM_IP": ("LoginEvent", "IPAddress", "the login came from the IP address"),
    "INVESTIGATED_IN": ("Account", "InvestigationCase", "the account is a subject of the case"),
}
CANON_ATTRS = {
    "Transaction.amount": ("Transaction", "number", "the amount actually moved or settled"),
    "Transaction.timestamp": ("Transaction", "datetime", "when the transaction happened (date and time)"),
    "Transaction.channel": ("Transaction", "string", "how it was made: channel, payment mechanism or type code"),
    "Account.open_date": ("Account", "datetime", "when the account was opened"),
    "Account.status": ("Account", "string", "the account's status"),
    "Account.kyc_score": ("Account", "number", "a numeric KYC or risk score for the account or its holder"),
    "Person.anonymized_ssn_hash": ("Person", "string", "a hash of the person's national identity number"),
    "Person.kyc_verified_at": ("Person", "datetime", "when the person's KYC was last verified"),
    "Beneficiary.bank_identifier": ("Beneficiary", "string", "the BIC, sort code or other identifier of the beneficiary's bank"),
    "Beneficiary.name_hash": ("Beneficiary", "string", "a hash of the beneficiary's name"),
    "InvestigationCase.status": ("InvestigationCase", "string", "the case status"),
    "account_under_investigation": ("Account", "any", "how to tell that an account is under investigation: a link to an "
                                    "open case, or a flag or block on the account"),
}


# --------------------------------------------------------------------------------------------------------- 1. parse
def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#") or s.startswith("//"):
            continue
        out.append(re.sub(r"\s(#|//).*$", "", line))
    return "\n".join(out)


def _split_top(s: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "(" or ch == "<"
        depth -= ch == ")" or ch == ">"
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_gsql(text: str) -> dict:
    """{vertices: {name: {primary_id, attrs: {attr: type}}}, edges: {name: {from, to, directed, reverse, attrs}}}"""
    body = strip_comments(text)
    verts, edges = {}, {}
    for m in re.finditer(r"(?:ADD|CREATE)\s+VERTEX\s+(\w+)\s*\((.*?)\)\s*(?:WITH[^;]*)?;", body, re.S | re.I):
        name, inner = m.group(1), m.group(2)
        attrs, pid = {}, None
        for part in _split_top(inner):
            toks = part.split()
            if not toks:
                continue
            if toks[0].upper() == "PRIMARY_ID" and len(toks) >= 3:
                pid = toks[1]
                attrs[toks[1]] = toks[2].upper()
            elif len(toks) >= 2:
                attrs[toks[0]] = toks[1].upper()
        verts[name] = {"primary_id": pid, "attrs": attrs}
    for m in re.finditer(r"(?:ADD|CREATE)\s+(DIRECTED|UNDIRECTED)\s+EDGE\s+(\w+)\s*\((.*?)\)\s*(WITH[^;]*)?;", body, re.S | re.I):
        kind, name, inner, with_ = m.group(1).upper(), m.group(2), m.group(3), m.group(4) or ""
        pairs = []
        attrs = {}
        for chunk in inner.split("|"):
            fr = re.search(r"FROM\s+(\w+)", chunk, re.I)
            to = re.search(r"TO\s+(\w+)", chunk, re.I)
            if fr and to:
                pairs.append((fr.group(1), to.group(1)))
            for part in _split_top(chunk):
                toks = part.split()
                if len(toks) >= 2 and toks[0].upper() not in ("FROM", "TO"):
                    attrs[toks[0]] = toks[1].upper()
        rev = re.search(r'REVERSE_EDGE\s*=\s*"(\w+)"', with_, re.I)
        edges[name] = {"pairs": pairs, "from": pairs[0][0] if pairs else None, "to": pairs[0][1] if pairs else None,
                       "directed": kind == "DIRECTED", "reverse": rev.group(1) if rev else None, "attrs": attrs}
    graph = re.search(r"CREATE\s+GRAPH\s+(\w+)", body, re.I)
    return {"graph": graph.group(1) if graph else "", "vertices": verts, "edges": edges}


def schema_text(s: dict) -> str:
    lines = [f"GRAPH {s['graph']}", "VERTEX TYPES (name: attribute type, ...)"]
    for v, d in s["vertices"].items():
        lines.append(f"  {v} (primary id {d['primary_id']}): " + ", ".join(f"{a} {t}" for a, t in d["attrs"].items()))
    lines.append("EDGE TYPES (name: FROM -> TO; reverse edge name if declared; attributes)")
    for e, d in s["edges"].items():
        ends = " | ".join(f"{a} -> {b}" for a, b in d["pairs"])
        extra = (f"; reverse edge {d['reverse']}" if d["reverse"] else "") + ("" if d["directed"] else "; undirected")
        if d["attrs"]:
            extra += "; attrs " + ", ".join(f"{a} {t}" for a, t in d["attrs"].items())
        lines.append(f"  {e}: {ends}{extra}")
    return "\n".join(lines)


# -------------------------------------------------------------------------------------------------------- 2. propose
def _canon_text() -> str:
    v = "\n".join(f"  {k}: {d}" for k, d in CANON_VERTICES.items())
    e = "\n".join(f"  {k}: {a} -> {b}: {d}" for k, (a, b, d) in CANON_EDGES.items())
    a = "\n".join(f"  {k} ({t}): {d}" for k, (_, t, d) in CANON_ATTRS.items())
    return f"CANONICAL VERTICES\n{v}\nCANONICAL EDGES (source -> target)\n{e}\nCANONICAL ATTRIBUTES\n{a}"


PROMPT = """You map another bank's graph schema onto a canonical fraud-graph model. Use your knowledge of banking data
standards (ISO 20022 abbreviations, BIAN service domains, core-banking tables such as Temenos T24) to read the names.

{canon}

FOREIGN SCHEMA
{schema}

Rules:
- kind is "exact" when the foreign element means the same thing, "approximate" when the concept is only a role, an
  inference across relationships, a different granularity or a lossy conversion, and "unmapped" when nothing in the
  schema carries the concept. Never force a mapping: unmapped is a correct answer.
- A condition that selects exactly the concept out of a mixed type (a customer flag, a party-type code, an application
  or event code) keeps the mapping exact. Walking a declared reverse edge keeps an edge mapping exact.
- vertices: foreign = the foreign vertex type(s). Several types may map to one canonical type. When one foreign type
  carries two canonical types (customers and external parties in one type), give each a condition, written on an
  attribute of that type (IsCustomer = true, partyType in (...)) or, if no attribute tells them apart, on the presence of
  an edge (has <edgeName>). If the concept exists only as an attribute (a terminal id or IP address stored on another
  vertex), use kind approximate and give it in attribute_only as "Vertex.attr".
- edges: path = the list of foreign edge names walked from the canonical source to the canonical target. Write a hop
  walked against its declared direction as reverse_<edge> (use the declared reverse edge name when there is one). Prefer
  the plain debtor/creditor edges over ultimate or agent edges. When several foreign edges carry the relationship (two
  ownership edges, a debit and a credit marker on one edge), give the main one in path and name the others in reason.
  A path of more than one hop is approximate unless the intermediate vertex is only a link record (a relationship plan
  or role), in which case it is exact.
- attributes: foreign = "Vertex.attr" on the mapped vertex, or on a vertex reached through edges with via = [edge names]
  (for example a bank identifier on the agent or bank vertex the payment points to). A date held as a string, a rating
  or yes/no flag instead of a score, a record-maintenance status instead of a business status, or a time taken from a
  message header instead of the transaction itself is approximate; if only a yes/no flag exists for a score, unmapped.
- account_under_investigation: a path from the account to an open case (exact) when a case type exists, else a flag or
  block attribute on the account (approximate).
Return JSON only:
{{"vertices": [{{"canonical": "...", "kind": "...", "foreign": ["..."], "condition": "", "attribute_only": "", "reason": "..."}}],
 "edges": [{{"canonical": "...", "kind": "...", "path": ["..."], "condition": "", "reason": "..."}}],
 "attributes": [{{"canonical": "...", "kind": "...", "foreign": "Vertex.attr", "via": [], "reason": "..."}}]}}
Cover every canonical vertex, edge and attribute exactly once."""


def _llm_json(prompt: str, model: str | None) -> tuple[dict, dict]:
    from openai import OpenAI
    base, key = env("LLM_BASE_URL"), env("LLM_API_KEY")
    model = model or env("LLM_MODEL", "")
    if not key or not model:
        raise SystemExit("schema_map: no LLM configured (LLM_API_KEY / LLM_MODEL in .env); nothing proposed")
    client = OpenAI(base_url=base, api_key=key, timeout=180, max_retries=2)
    t0 = time.time()
    kw = {"reasoning_effort": "medium"} if ("gemini" in model or "gpt-oss" in model) else {}
    r = client.chat.completions.create(model=model, temperature=0, response_format={"type": "json_object"},
                                       messages=[{"role": "user", "content": prompt}], max_tokens=16000, **kw)
    txt = (r.choices[0].message.content or "").strip()
    txt = txt[txt.find("{"): txt.rfind("}") + 1]
    meta = {"model": model, "tokens": getattr(r.usage, "total_tokens", None), "seconds": round(time.time() - t0, 1),
            "finish": r.choices[0].finish_reason}
    return json.loads(txt), meta


def propose(schema: dict, model: str | None = None, refresh: bool = False) -> dict:
    prompt = PROMPT.format(canon=_canon_text(), schema=schema_text(schema))
    key = hashlib.sha256((prompt + "|" + PROMPT_VERSION + "|" + (model or env("LLM_MODEL", ""))).encode()).hexdigest()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cache = OUT_DIR / f"{schema['graph'] or 'schema'}.{key[:12]}.proposal.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    raw, meta = _llm_json(prompt, model)
    rec = {"proposal": raw, "meta": {**meta, "prompt_version": PROMPT_VERSION, "sha256": key,
                                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")}}
    cache.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return rec


# ------------------------------------------------------------------------------------------------------- 3. validate
NUMERIC = {"INT", "UINT", "FLOAT", "DOUBLE"}


def _hop_index(schema: dict) -> dict:
    """name -> list of (from, to) for every walkable hop: forward edges, declared reverse edges, undirected both ways."""
    idx = {}
    for e, d in schema["edges"].items():
        for a, b in d["pairs"]:
            idx.setdefault(e, []).append((a, b))
            if not d["directed"]:
                idx[e].append((b, a))
                idx.setdefault(f"reverse_{e}", []).append((b, a))
            if d["reverse"]:
                idx.setdefault(d["reverse"], []).append((b, a))
    return idx


def _walk(path: list[str], start: set[str], end: set[str], hops: dict) -> tuple[bool, str]:
    cur = set(start)
    for h in path:
        if h not in hops:
            return False, f"edge {h} is not in the schema"
        nxt = {b for a, b in hops[h] if a in cur}
        if not nxt:
            return False, f"edge {h} does not start at {sorted(cur)}"
        cur = nxt
    if not (cur & end):
        return False, f"the path ends at {sorted(cur)}, not at {sorted(end)}"
    return True, ""


def validate(schema: dict, proposal: dict) -> dict:
    V, hops = schema["vertices"], _hop_index(schema)
    out = {"vertices": {}, "edges": {}, "attributes": {}}
    got = {i.get("canonical"): i for i in proposal.get("vertices", [])}
    for c in CANON_VERTICES:
        it = dict(got.get(c) or {"canonical": c, "kind": "unmapped", "foreign": [], "reason": "not proposed"})
        it["foreign"] = [f for f in (it.get("foreign") or []) if f]
        bad = [f for f in it["foreign"] if f not in V]
        attr_only = (it.get("attribute_only") or "").strip()
        if attr_only:
            vv, _, aa = attr_only.partition(".")
            if vv not in V or aa not in V.get(vv, {}).get("attrs", {}):
                bad.append(attr_only)
        cond = it.get("condition") or ""
        if cond and it["foreign"] and not bad:
            names = [n for n in re.findall(r"[A-Za-z_]\w*", cond) if n.lower() not in ("has", "in", "and", "or", "not",
                                                                                     "is", "null", "true", "false")]
            known = {a for f in it["foreign"] if f in V for a in V[f]["attrs"]}
            known |= {e for e, d in schema["edges"].items() for a, b in d["pairs"] if a in it["foreign"] or b in it["foreign"]}
            known |= {d["reverse"] for d in schema["edges"].values() if d["reverse"]}
            if names and names[0] not in known:   # a filter we cannot check: keep the type, drop the filter, say so
                it.update(kind="approximate", condition="",
                          condition_note=f"proposed filter '{cond}' names {names[0]}, which is not an attribute or edge here")
        if bad:
            it.update(kind="unmapped", rejected=f"not in the schema: {bad}", foreign=[])
        if it.get("kind") == "unmapped":
            it["foreign"] = []
        out["vertices"][c] = it
    vmap = {c: set(i["foreign"]) for c, i in out["vertices"].items()}
    got = {i.get("canonical"): i for i in proposal.get("edges", [])}
    for c, (src, dst, _) in CANON_EDGES.items():
        it = dict(got.get(c) or {"canonical": c, "kind": "unmapped", "path": [], "reason": "not proposed"})
        it["path"] = [p for p in (it.get("path") or []) if p]
        if it.get("kind") != "unmapped":
            if not vmap[src] or not vmap[dst]:
                it.update(kind="unmapped", rejected=f"{src} or {dst} is unmapped", path=[])
            elif not it["path"]:
                it.update(kind="unmapped", rejected="no path given")
            else:
                ok, why = _walk(it["path"], vmap[src], vmap[dst], hops)
                if not ok:
                    it.update(kind="unmapped", rejected=why, path=[])
                elif len(it["path"]) > 1 and it.get("kind") == "exact" and not it.get("reason"):
                    it["kind"] = "approximate"
        else:
            it["path"] = []
        # an edge is no more certain than its endpoints: landing on a role-derived (approximate) vertex caps it
        if it.get("kind") == "exact" and "approximate" in (out["vertices"][src].get("kind"), out["vertices"][dst].get("kind")):
            it["kind"], it["cap_note"] = "approximate", "an endpoint is an approximate (role or attribute) mapping"
        out["edges"][c] = it
    got = {i.get("canonical"): i for i in proposal.get("attributes", [])}
    for c, (owner, typ, _) in CANON_ATTRS.items():
        it = dict(got.get(c) or {"canonical": c, "kind": "unmapped", "foreign": "", "reason": "not proposed"})
        f = (it.get("foreign") or "").strip()
        if it.get("kind") != "unmapped" and f:
            vv, _, aa = f.partition(".")
            via = [p for p in (it.get("via") or []) if p]
            if vv not in V or aa not in V[vv]["attrs"]:
                it.update(kind="unmapped", rejected=f"{f} is not in the schema", foreign="")
            else:
                reach = vmap.get(owner, set())
                if via:
                    ok, why = _walk(via, reach, {vv}, hops)
                    if not ok:
                        it.update(kind="unmapped", rejected=f"via: {why}", foreign="")
                elif c != "account_under_investigation" and vv not in reach and reach:
                    one_hop = {b for h in hops.values() for a, b in h if a in reach}
                    if vv not in one_hop:
                        it.update(kind="unmapped", rejected=f"{vv} is not {owner}'s mapped vertex or one hop away",
                                  foreign="")
                t = V.get(vv, {}).get("attrs", {}).get(aa, "")
                if it.get("kind") != "unmapped":
                    if typ == "number" and t not in NUMERIC and t != "BOOL":
                        it["kind"], it["type_note"] = "approximate", f"{aa} is {t}, not a number"
                    if typ == "datetime" and t != "DATETIME":
                        it["kind"], it["type_note"] = "approximate", f"{aa} is {t}, not DATETIME"
        elif it.get("kind") != "unmapped" and c == "account_under_investigation" and it.get("via"):
            ok, why = _walk(it["via"], vmap.get("Account", set()), vmap.get("InvestigationCase", set()), hops)
            if not ok:
                it.update(kind="unmapped", rejected=why)
        else:
            it.update(kind="unmapped" if not f and not it.get("via") else it.get("kind"), foreign=f)
        out["attributes"][c] = it
    return out


# ---------------------------------------------------------------------------------------------------------- 4. score
def answer_key(text: str) -> dict:
    """The "Expected mapping (ground truth)" comment block: {canonical item: (kind, spec)}."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if "Expected mapping" in l)
    except StopIteration:
        return {}
    key, last = {}, None
    for l in lines[start + 1:]:
        if not l.lstrip().startswith("#"):
            break
        body = l.lstrip()[1:].rstrip()
        m = re.match(r"\s{1,4}(\S.*?)\s+(exact|approximate|unmapped)\b\s*(.*)$", body)
        if m:
            names, kind, spec = m.group(1), m.group(2), m.group(3)
            for n in [x.strip() for x in names.split(",") if x.strip()]:
                key[n] = (kind, spec)
                last = n
        elif last and body.strip():
            k, s = key[last]
            key[last] = (k, s + " " + body.strip())
    return key


def _path_vertices(path: list[str], start: set[str], schema: dict | None) -> list[str]:
    """The vertex types a path walks through (the answer key sometimes names a path by its vertices)."""
    if not schema or not path:
        return []
    hops, cur, seen = _hop_index(schema), set(start), []
    for h in path:
        cur = {b for a, b in hops.get(h, []) if a in cur}
        seen += sorted(cur)
    return seen


def _our_targets(section: str, it: dict, schema: dict | None = None, vmap: dict | None = None, canon: str = "") -> list[str]:
    if section == "vertices":
        return list(it.get("foreign") or []) + ([it["attribute_only"]] if it.get("attribute_only") else [])
    if section == "edges":
        src = (vmap or {}).get(CANON_EDGES[canon][0], set()) if canon in CANON_EDGES else set()
        return list(it.get("path") or []) + _path_vertices(list(it.get("path") or []), src, schema)
    f = it.get("foreign") or ""
    return ([f, f.partition(".")[2]] if f else []) + list(it.get("via") or [])


def _names_match(o: str, spec_names: set[str]) -> bool:
    cands = {o, o.partition(".")[0], o.partition(".")[2], re.sub(r"^reverse_", "", o)}
    return any(c and c in spec_names for c in cands)


def score(mapping: dict, key: dict, schema: dict | None = None) -> dict:
    rows = []
    vmap = {c: set(i.get("foreign") or []) for c, i in mapping["vertices"].items()}
    for section in ("vertices", "edges", "attributes"):
        for c, it in mapping[section].items():
            if c not in key:
                continue
            gk, spec = key[c]
            ours = _our_targets(section, it, schema, vmap, c)
            spec_names = set(re.findall(r"[A-Za-z_][\w]*", spec))
            if gk == "unmapped":
                target_ok = it["kind"] == "unmapped"
            else:
                target_ok = it["kind"] != "unmapped" and any(_names_match(o, spec_names) for o in ours if o)
            rows.append({"item": c, "section": section, "expected": gk, "expected_spec": spec.strip(), "ours": it["kind"],
                         "our_target": " -> ".join(it.get("path") or []) if section == "edges" else ", ".join(ours),
                         "target_ok": target_ok, "kind_ok": gk == it["kind"]})
    n = len(rows)
    return {"items": n, "target_ok": sum(r["target_ok"] for r in rows), "kind_ok": sum(r["kind_ok"] for r in rows),
            "both_ok": sum(r["target_ok"] and r["kind_ok"] for r in rows), "rows": rows}


# ------------------------------------------------------------------------------------------------------------ CLI
def run_file(path: Path, model: str | None, refresh: bool, do_score: bool) -> dict:
    text = path.read_text(encoding="utf-8")
    schema = parse_gsql(text)
    rec = propose(schema, model, refresh)
    mapping = validate(schema, rec["proposal"])
    res = {"file": str(path), "graph": schema["graph"], "n_vertex_types": len(schema["vertices"]),
           "n_edge_types": len(schema["edges"]), "meta": rec["meta"], "mapping": mapping}
    if do_score:
        res["score"] = score(mapping, answer_key(text), schema)
    return res


def print_result(res: dict) -> None:
    m = res["mapping"]
    print(f"\n== {Path(res['file']).name}: graph {res['graph']}, {res['n_vertex_types']} vertex types, "
          f"{res['n_edge_types']} edge types (model {res['meta'].get('model')}, cached proposal {res['meta'].get('sha256', '')[:12]})")
    for c, it in m["vertices"].items():
        tgt = ", ".join(it.get("foreign") or []) or it.get("attribute_only") or "-"
        cond = f" where {it['condition']}" if it.get("condition") else ""
        print(f"  {c:18s} {it['kind']:12s} {tgt}{cond}" + (f"   [rejected: {it['rejected']}]" if it.get("rejected") else ""))
    for c, it in m["edges"].items():
        print(f"  {c:18s} {it['kind']:12s} {' -> '.join(it.get('path') or []) or '-'}"
              + (f"   [rejected: {it['rejected']}]" if it.get("rejected") else ""))
    for c, it in m["attributes"].items():
        via = f" via {' -> '.join(it['via'])}" if it.get("via") else ""
        note = f" ({it['type_note']})" if it.get("type_note") else ""
        print(f"  {c:28s} {it['kind']:12s} {it.get('foreign') or '-'}{via}{note}"
              + (f"   [rejected: {it['rejected']}]" if it.get("rejected") else ""))
    if "score" in res:
        s = res["score"]
        print(f"  SCORE vs the answer key: target right {s['target_ok']}/{s['items']}, kind right {s['kind_ok']}/{s['items']}, "
              f"both {s['both_ok']}/{s['items']}")
        for r in s["rows"]:
            if not (r["target_ok"] and r["kind_ok"]):
                print(f"    miss {r['item']:28s} expected {r['expected']:11s} {r['expected_spec'][:70]:70s} | ours {r['ours']}: {r['our_target'][:60]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.schema_map")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--score", action="store_true", help="score against each file's Expected mapping block")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached proposal")
    ap.add_argument("--model", default=None)
    ap.add_argument("--json", default=None, help="write all results to this file")
    a = ap.parse_args(argv)
    results = []
    # PowerShell hands round2/*.gsql over unexpanded; a pattern that matches nothing stays as typed and fails as a missing file
    for f in [p for f in a.files for p in (sorted(glob.glob(f)) or [f])]:
        if not Path(f).is_file():
            raise SystemExit(f"schema_map: no such file: {f}")
        res = run_file(Path(f), a.model, a.refresh, a.score)
        print_result(res)
        results.append(res)
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    if a.score and results:
        t = sum(r["score"]["items"] for r in results)
        print(f"\nTOTAL over {len(results)} schemas: target right {sum(r['score']['target_ok'] for r in results)}/{t}, "
              f"kind right {sum(r['score']['kind_ok'] for r in results)}/{t}, "
              f"both {sum(r['score']['both_ok'] for r in results)}/{t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
