"""Graph and vector fused: precedent that is both connected and similar, and ring components by a graph algorithm.

    fused_precedent(lane, card_ids, query, t0, k=5, days=30)   ClosedCases reached by traversal from the case's cards
                                                                (own card, connected card, or another card sharing a rare
                                                                device profile, recipient email domain or billing region
                                                                in the window), ranked by cosine similarity of
                                                                ClosedCase.emb to the case's query vector
    ring_component(lane, card_id, t_from, t_to, template=None)  weakly connected component (label propagation) of the card
                                                                graph whose edges are rare elements shared by the cards'
                                                                ATYPICAL window transactions (a product, an amount or a
                                                                proxy the card never showed before the window): a linking
                                                                device profile on 25 or fewer cards, a recipient email
                                                                domain or access signature used by 2 to 25 cards in the
                                                                whole window, plus an optional ring template a typology
                                                                detector matched
    register(T), precedent_signal(...), ring_signal(...)       agent hooks: registry tools and zero-weight attested Signals

Why the atypical restriction: over all window transactions the same element rules percolate (one component of 600 to
1,100 cards on every one of the 18 HHF alerts, logs/tgdepth/ring_diag*.py); over atypical rows the window graph falls
apart into 2 to 25 small components and only real rings stay connected.

`lane` is None (DuckDB, kavach.duckq, local vectors from kavach.vectors), a TgTools / McpTools instance (installed
queries through its `_q`, so the MCP lane keeps its allowlist), or a raw pyTigerGraph connection. Both lanes apply
the same definitions (queries_fusion.gsql is the GSQL twin of the SQL below); tests/test_fusion.py checks the pure
parts offline and the lanes agree on the 18 HHF cases (logs/tgdepth/fusion_parity.log).

CLI: python -m kavach.graph.fusion precedent HHF-006 [--backend duck|tg] | ring HHF-006 [--backend ...] | study
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta

import numpy as np
import pandas as pd

MAX_DEV_CARDS = 25        # a device profile links cards only if 25 or fewer cards ever used it (DeviceProfile.n_cards)
MAX_SHARED_CARDS = 25     # an email domain / region / access signature links cards only if 2 to 25 cards used it in the window
PRECEDENT_DAYS = 30       # traversal window for fused precedent, ending at the alert
RING_DAYS = 30            # window for the ring component, ending at the alert
MIN_PRIOR = 5             # a card needs 5 or more transactions before the window for "atypical" to mean anything
PATH_ORDER = ("card", "connected_card", "device", "r_email", "region")   # plain-traversal rank: strongest link first

_PLACEHOLDER_DEV = "replace(replace(coalesce(device_key, ''), '|', ''), ' ', '') = ''"
# DeviceInfo values naming an OS or browser family, not a handset (kavach.detectors.network.FAMILY, written so GSQL can
# test it without a regex: exact names plus the "trident/" and "rv:" prefixes)
FAMILY_NAMES = ("windows", "ios device", "macos", "linux", "samsung")
FAMILY_PREFIXES = ("trident/", "rv:")


def linking_device(info, screen, n_cards: int, max_dev: int = MAX_DEV_CARDS) -> bool:
    """A device profile links cards only when it names a handset or a full fingerprint: DeviceInfo not empty, used by
    max_dev or fewer cards in all data, and not a bare OS/browser family name without a screen resolution (the
    definition of kavach.detectors.network.generic_device at the 25-card level)."""
    info = info or ""
    if not info or int(n_cards) > max_dev:
        return False
    low = info.lower()
    family = low in FAMILY_NAMES or low.startswith(FAMILY_PREFIXES)
    return not (family and not (screen or ""))


def _linking_sql(max_dev: int) -> str:
    fam = " or ".join([f"lower(info) in ({', '.join(repr(x) for x in FAMILY_NAMES)})"] +
                      [f"lower(info) like '{p}%'" for p in FAMILY_PREFIXES])
    return f"(info <> '' and n <= {int(max_dev)} and not (({fam}) and screen = ''))"


_DEVMETA = f"""select device_key, count(distinct card_id) n, any_value(coalesce(DeviceInfo, '')) info,
                      any_value(coalesce(cast(id_33 as varchar), '')) screen
               from txn where not ({_PLACEHOLDER_DEV}) group by 1"""


def _ts(t) -> str:
    return pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------------------------------------------
# pure helpers (shared by both lanes, unit-tested offline)

def components(elements: dict) -> dict:
    """Union-find over the bipartite card-element graph: {element: [cards]} -> {card: root}. The root is the smallest
    card id of the component, so the partition and the labels are the same whatever the input order."""
    parent: dict = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for cards in elements.values():
        for c in cards:
            parent.setdefault(c, c)
        cs = list(cards)
        for c in cs[1:]:
            a, b = find(cs[0]), find(c)
            if a != b:
                lo, hi = (a, b) if a < b else (b, a)
                parent[hi] = lo
    return {c: find(c) for c in parent}


def keep_elements(raw: dict, devmeta: dict | None = None, max_dev: int = MAX_DEV_CARDS,
                  max_shared: int = MAX_SHARED_CARDS, window_pop: dict | None = None) -> dict:
    """Linking elements: {key: cards} with key "<kind>:<value>" and 2 or more cards. Devices must be linking devices
    (linking_device over devmeta {device_key: (n_cards, info, screen)}); emails, regions and signatures must be used by
    max_shared or fewer distinct cards in the whole window (window_pop {key: cards}; when absent, the cards given); a
    template (named by a detector) always links."""
    out = {}
    for key, cards in raw.items():
        cards = sorted(set(cards))
        kind = key.split(":", 1)[0]
        if len(cards) < 2:
            continue
        if kind == "device":
            n, info, screen = (devmeta or {}).get(key.split(":", 1)[1], (10 ** 9, "", ""))
            if not linking_device(info, screen, n, max_dev):
                continue
        elif kind != "template" and (window_pop or {}).get(key, len(cards)) > max_shared:
            continue
        out[key] = cards
    return out


def component_of(seed: str, elements: dict) -> dict:
    """The seed's component: cards, the elements inside it, every card pair with the elements they share, and the
    component statistics of the whole window graph (number of components with 2 or more cards, largest size)."""
    lab = components(elements)
    sizes = pd.Series(list(lab.values())).value_counts() if lab else pd.Series(dtype=int)
    root = lab.get(seed)
    cards = sorted(c for c, r in lab.items() if r == root) if root is not None else [seed]
    cs = set(cards)
    els = {k: v for k, v in sorted(elements.items()) if cs.intersection(v)}
    pairs: dict = {}
    for k, v in els.items():
        for i, a in enumerate(v):
            for b in v[i + 1:]:
                pairs.setdefault((a, b), []).append(k)
    edges = [{"a": a, "b": b, "elements": sorted(ks)} for (a, b), ks in sorted(pairs.items())]
    return {"seed": seed, "cards": cards, "size": len(cards), "elements": els, "edges": edges,
            "n_components": int(len(sizes)), "largest": int(sizes.max()) if len(sizes) else 0,
            "n_elements": len(elements)}


def rank_fused(cands: pd.DataFrame, sims: dict, k: int) -> list[dict]:
    """Candidates (case_id, via, through, outcome, pattern, opened_at) ranked by similarity, then case id."""
    rows = []
    for r in cands.itertuples():
        s = sims.get(r.case_id)
        if s is None:
            continue
        rows.append({"case_id": r.case_id, "similarity": round(float(s), 4), "via": sorted(set(r.via)),
                     "through": sorted(set(r.through)), "outcome": r.outcome, "pattern": r.pattern,
                     "opened_at": str(r.opened_at)[:19], "path": path_type(r.via)})
    rows.sort(key=lambda x: (-x["similarity"], x["case_id"]))
    return rows[:k]


def path_type(via) -> str:
    """The strongest graph path that reached the case: card > connected_card > device > r_email > region."""
    kinds = {str(v).split(":", 1)[0] for v in via}
    return next((p for p in PATH_ORDER if p in kinds), "none")


def rank_traversal(cands: pd.DataFrame, k: int) -> list[str]:
    """Plain traversal ranking (no vectors): strongest path first, then the most recent closed case, then id."""
    if cands is None or not len(cands):
        return []
    d = cands.copy()
    d["_p"] = [PATH_ORDER.index(path_type(v)) if path_type(v) in PATH_ORDER else 9 for v in d.via]
    d["_t"] = pd.to_datetime(d.opened_at)
    d = d.sort_values(["_p", "_t", "case_id"], ascending=[True, False, True])
    return list(d.case_id[:k])


def query_text(answer: dict) -> str:
    """The case's retrieval text, built like the agent's recall query: outcome words plus the strongest claims."""
    c = answer["case"]
    head = "cleared false alarm" if c["verdict"] == "legitimate" else f"confirmed fraud {c['pattern']}"
    claims = [e["claim"] for e in c.get("evidence", []) if e.get("source") != "document" and not e["claim"].startswith(
        ("Case memory", "Model risk score", "Intake", "Data quality"))][:3]
    return f"{head}: " + " ".join(claims)


# ---------------------------------------------------------------------------------------------------------------
# lane plumbing

def _is_duck(lane) -> bool:
    return lane is None or lane == "duck"


def _run(lane, name: str, **params):
    """Run an installed query on the TigerGraph lane: through TgTools/McpTools._q when given one (traced allowlist on
    MCP), else on a raw pyTigerGraph connection."""
    if hasattr(lane, "_q"):
        return lane._q(name, **params)
    return lane.runInstalledQuery(name, params=params, timeout=120_000, usePost=True)


def _duck(con=None):
    if con is not None:
        return con
    from kavach import duckq
    return duckq.con()


def _qvec(query) -> np.ndarray:
    if isinstance(query, str):
        from kavach.vectors import embed
        return embed([query])[0]
    v = np.asarray(query, dtype=np.float32)
    return v / (np.linalg.norm(v) or 1.0)


# ---------------------------------------------------------------------------------------------------------------
# fused precedent

def precedent_candidates_duck(card_ids, t_from, t_to, max_dev: int = MAX_DEV_CARDS, max_shared: int = MAX_SHARED_CARDS,
                              con=None) -> pd.DataFrame:
    """Closed cases reachable from the seed cards (same definition as GSQL fused_precedent)."""
    cards = sorted({str(c) for c in card_ids}) or [""]
    sql = f"""
    with w as (select TransactionID, card_id, case when {_PLACEHOLDER_DEV} then null else device_key end device_key,
                      R_emaildomain, cast(cast(addr1 as integer) as varchar) region
               from txn where ts >= ? and ts <= ?),
    s as (select * from w where card_id in (select unnest(?))),
    devmeta as ({_DEVMETA}),
    use as (select 'device' kind, device_key val, card_id from w where device_key is not null
            union all select 'r_email', R_emaildomain, card_id from w where R_emaildomain is not null
            union all select 'region', region, card_id from w where region is not null),
    el as (select distinct 'device' kind, s.device_key val from s join devmeta p using (device_key) where {_linking_sql(max_dev)}
           union select distinct 'r_email', R_emaildomain from s where R_emaildomain is not null
           union select distinct 'region', region from s where region is not null),
    pop as (select u.kind, u.val, count(distinct u.card_id) n from use u join el using (kind, val) group by 1, 2),
    keep as (select kind, val from pop where kind = 'device' or n <= ?),
    c1 as (select distinct u.kind || ':' || u.val elem, u.card_id from use u join keep using (kind, val)
           where u.card_id not in (select unnest(?))),
    hit as (select case_id, 'card' via, null through from closed where card_id in (select unnest(?))
            union all select case_id, 'connected_card', null from closed_conn where card_id in (select unnest(?))
            union all select c.case_id, c1.elem, c1.card_id from closed c join c1 using (card_id)
            union all select cc.case_id, c1.elem, c1.card_id from closed_conn cc join c1 using (card_id))
    select h.case_id, list(distinct h.via order by h.via) via, list(distinct h.through order by h.through)
             filter (where h.through is not null) through, any_value(c.outcome) outcome, any_value(c.pattern) pattern,
           any_value(c.opened_at) opened_at
    from hit h join closed c using (case_id) group by 1 order by 1"""
    df = _duck(con).execute(sql, [pd.Timestamp(t_from).to_pydatetime(), pd.Timestamp(t_to).to_pydatetime(), cards,
                                  max_shared, cards, cards, cards]).df()
    if len(df):
        df["through"] = [list(x) if isinstance(x, (list, np.ndarray)) else [] for x in df.through]
        df["via"] = [list(x) for x in df.via]
    return df


def _parse_precedent(res: list) -> tuple[pd.DataFrame, dict, int]:
    """Rows of the GSQL fused_precedent result -> (candidates frame, {case_id: similarity}, n_candidates)."""
    def part(key):
        return next((r[key] for r in res if key in r), None)
    cands = part("candidates") or []
    dist = part("dist") or {}
    hits = part("hits") or []
    rows = []
    for v in cands:
        a = v["attributes"]
        g = lambda k: a.get(f"X.{k}", a.get(k))    # noqa: E731
        rows.append({"case_id": v["v_id"], "via": sorted(g("@via") or []), "through": sorted(g("@through") or []),
                     "outcome": g("outcome"), "pattern": g("pattern"), "opened_at": str(g("opened_at"))[:19]})
    df = pd.DataFrame(rows, columns=["case_id", "via", "through", "outcome", "pattern", "opened_at"])
    sims = {}
    for h in hits:
        d = dist.get(h["v_id"], dist.get(str(h["v_id"])))
        if d is not None:
            sims[h["v_id"]] = round(1.0 - float(d), 4)
    return df.sort_values("case_id").reset_index(drop=True), sims, int(part("n_candidates") or len(df))


def fused_precedent(lane, card_ids, query, t0, k: int = 5, days: float = PRECEDENT_DAYS, max_dev: int = MAX_DEV_CARDS,
                    max_shared: int = MAX_SHARED_CARDS, con=None, vecs: dict | None = None) -> dict:
    """Closed cases connected to the case by the graph, ranked by similarity to the case (see module docstring).

    Returns {"hits": [{case_id, similarity, path, via, through, outcome, pattern, opened_at}], "n_candidates",
    "candidates" (every reachable case with its paths), "how": {query, params}}. An empty candidate set is a valid
    answer (no connected precedent); a failing query raises (fail closed)."""
    t0 = pd.Timestamp(t0)
    t_from, t_to = t0 - timedelta(days=days), t0
    qv = _qvec(query)
    params = {"card_ids": sorted({str(c) for c in card_ids}), "t_from": _ts(t_from), "t_to": _ts(t_to), "k": int(k),
              "max_dev": int(max_dev), "max_shared": int(max_shared)}
    if _is_duck(lane):
        cands = precedent_candidates_duck(card_ids, t_from, t_to, max_dev, max_shared, con=con)
        if vecs is None:
            from kavach.vectors import index
            vecs = index()
        pos = {str(c): i for i, c in enumerate(vecs["case_ids"])}
        sims = {}
        for c in cands.case_id if len(cands) else []:
            if c in pos:
                sims[c] = round(float(np.asarray(vecs["case_vecs"][pos[c]]) @ qv), 4)
        hits = rank_fused(cands, sims, k)
        how = {"tool": "fused_precedent", "lane": "duck", "params": params,
               "query": "SQL traversal over txn/closed/closed_conn + cosine over data/vectors.npz"}
        n = len(cands)
    else:
        res = _run(lane, "fused_precedent", **params, qv=[round(float(x), 6) for x in qv])
        cands, sims, n = _parse_precedent(res)
        hits = rank_fused(cands, sims, k)
        how = {"tool": "fused_precedent", "lane": "tg", "params": params,
               "query": "GSQL fused_precedent: traversal then vectorSearch({ClosedCase.emb}, qv, k, {candidate_set: X})"}
    return {"hits": hits, "n_candidates": int(n), "candidates": cands, "how": how}


# ---------------------------------------------------------------------------------------------------------------
# ring component (weakly connected components over the atypical-activity card graph)

def signature_key(product, proxy, id_15, device_type) -> str | None:
    """Access signature of a proxied row (kavach.duckq_ext.ENTITY_FIELDS["access_signature"]); None without a proxy."""
    def s(x):
        return "" if x is None or (isinstance(x, float) and x != x) else str(x)
    return f"{s(product)}|{s(proxy)}|{s(id_15)}|{s(device_type)}" if s(proxy) else None


def atypical_rows_duck(t_from, t_to, min_prior: int = MIN_PRIOR, con=None) -> pd.DataFrame:
    """Window transactions that are atypical for their own card against the card's history BEFORE the window: the card
    has min_prior or more earlier transactions and the row is a product the card never used, an amount above every
    earlier amount, or its first proxied row. Carries the elements of each row (device_key, R_emaildomain, signature)."""
    sql = f"""
    with w as (select TransactionID, card_id, ts, TransactionAmt amt, ProductCD, id_15, id_23, DeviceType, R_emaildomain,
                      case when {_PLACEHOLDER_DEV} then null else device_key end device_key
               from txn where ts >= ? and ts <= ?),
    b as (select card_id, count(*) n_b, max(TransactionAmt) max_b, sum(case when id_23 is not null then 1 else 0 end) proxy_b
          from txn where ts < ? and card_id in (select distinct card_id from w) group by 1),
    bp as (select distinct card_id, ProductCD from txn where ts < ? and card_id in (select distinct card_id from w))
    select cast(w.TransactionID as varchar) tid, w.card_id, w.ProductCD, w.id_15, w.id_23, w.DeviceType, w.R_emaildomain,
           w.device_key
    from w join b using (card_id) left join bp on bp.card_id = w.card_id and bp.ProductCD = w.ProductCD
    where b.n_b >= ? and (bp.ProductCD is null or w.amt > b.max_b or (w.id_23 is not null and b.proxy_b = 0))
    order by 1"""
    tf, tt = pd.Timestamp(t_from).to_pydatetime(), pd.Timestamp(t_to).to_pydatetime()
    return _duck(con).execute(sql, [tf, tt, tf, tf, int(min_prior)]).df()


def elements_of_rows(rows: pd.DataFrame) -> tuple[dict, dict]:
    """{element key: cards} and {element key: txn ids} of atypical rows."""
    cards: dict = {}
    txns: dict = {}
    for r in rows.itertuples():
        keys = []
        if isinstance(r.device_key, str) and r.device_key:
            keys.append("device:" + r.device_key)
        if isinstance(r.R_emaildomain, str) and r.R_emaildomain:
            keys.append("r_email:" + r.R_emaildomain)
        sig = signature_key(r.ProductCD, r.id_23, r.id_15, r.DeviceType)
        if sig:
            keys.append("signature:" + sig)
        for k in keys:
            cards.setdefault(k, set()).add(r.card_id)
            txns.setdefault(k, set()).add((r.card_id, str(r.tid)))
    return {k: sorted(v) for k, v in cards.items()}, {k: sorted(v) for k, v in txns.items()}


def window_pop_duck(keys, t_from, t_to, con=None) -> dict:
    """Distinct cards per recipient email domain / access signature over ALL window transactions (rarity is judged on
    the whole window; the link itself comes from atypical rows)."""
    keys = sorted(k for k in keys if k.startswith(("r_email:", "signature:")))
    if not keys:
        return {}
    sql = f"""
    with w as (select card_id, R_emaildomain, case when id_23 is not null then ProductCD || '|' || id_23 || '|' ||
                      coalesce(id_15, '') || '|' || coalesce(DeviceType, '') end sig
               from txn where ts >= ? and ts <= ?),
    u as (select 'r_email:' || R_emaildomain k, card_id from w where R_emaildomain is not null
          union all select 'signature:' || sig, card_id from w where sig is not null)
    select k, count(distinct card_id) from u where k in (select unnest(?)) group by 1"""
    rows = _duck(con).execute(sql, [pd.Timestamp(t_from).to_pydatetime(), pd.Timestamp(t_to).to_pydatetime(), keys]).fetchall()
    return {k: int(n) for k, n in rows}


def _devmeta_duck(keys, con=None) -> dict:
    devs = sorted(k.split(":", 1)[1] for k in keys if k.startswith("device:"))
    meta = _duck(con).execute(f"select * from ({_DEVMETA}) where device_key in (select unnest(?))", [devs or [""]]).fetchall()
    return {k: (int(n), info, screen) for k, n, info, screen in meta}


def _parse_ring(res: list) -> dict:
    def part(key, default=None):
        return next((r[key] for r in res if key in r), default)
    els = {k: sorted(v) for k, v in (part("elements", {}) or {}).items()}
    txns = {k: sorted(str(x) for x in v) for k, v in (part("element_txns", {}) or {}).items()}
    return {"cards": sorted(part("component", []) or []), "elements": els, "element_txns": txns,
            "n_components": int(part("n_components", 0) or 0), "largest": int(part("largest", 0) or 0),
            "n_elements": int(part("n_elements", 0) or 0), "n_window_txns": int(part("n_window_txns", 0) or 0),
            "n_atypical": int(part("n_atypical", 0) or 0)}


def ring_component(lane, card_id: str, t_from, t_to, template: tuple | None = None, max_dev: int = MAX_DEV_CARDS,
                   max_shared: int = MAX_SHARED_CARDS, min_prior: int = MIN_PRIOR, con=None) -> dict:
    """The card's weakly connected component in the window's card graph: two cards are joined when their ATYPICAL
    transactions (atypical_rows_duck) share a rare element, i.e. a linking device profile (linking_device), a recipient
    email domain or an access signature carried by the atypical rows of 2 to max_shared cards. `template` = (name,
    [cards]) from a ring typology detector joins those cards as one more element.

    Returns cards, size, elements {key: cards} inside the component, edges [{a, b, elements}], element_txns {key: the
    component's atypical txn ids on it}, n_components / largest over the whole window graph, n_atypical, how."""
    tname, tcards = (template or ("", []))
    params = {"seed": str(card_id), "t_from": _ts(t_from), "t_to": _ts(t_to), "max_dev": int(max_dev),
              "max_shared": int(max_shared), "min_prior": int(min_prior), "tmpl_name": tname or "",
              "tmpl_cards": sorted({str(c) for c in tcards}) or [""]}
    if _is_duck(lane):
        rows = atypical_rows_duck(t_from, t_to, min_prior, con=con)
        raw, txns = elements_of_rows(rows)
        if tname and len(set(tcards)) >= 2:
            raw[f"template:{tname}"] = sorted({str(c) for c in tcards})
        raw = {k: v for k, v in raw.items() if len(v) >= 2}
        els = keep_elements(raw, _devmeta_duck(raw, con=con), max_dev, max_shared,
                            window_pop_duck(raw, t_from, t_to, con=con))
        out = component_of(str(card_id), els)
        cs = set(out["cards"])
        out["element_txns"] = {k: sorted(t for c, t in txns.get(k, []) if c in cs) for k in out["elements"]
                               if not k.startswith("template:")}
        out["n_atypical"] = int(len(rows))
        out["how"] = {"tool": "ring_component", "lane": "duck", "params": params,
                      "query": "SQL atypical window rows + rare elements + union-find (kavach.graph.fusion.components)"}
        return out
    r = _parse_ring(_run(lane, "ring_wcc", **params))
    out = component_of(str(card_id), r["elements"])
    out.update({"cards": r["cards"] or out["cards"], "size": len(r["cards"] or out["cards"]), "n_components": r["n_components"],
                "largest": r["largest"], "n_elements": r["n_elements"], "element_txns": r["element_txns"],
                "n_window_txns": r["n_window_txns"], "n_atypical": r["n_atypical"]})
    out["how"] = {"tool": "ring_component", "lane": "tg", "params": params,
                  "query": "GSQL ring_wcc: window scan, atypical rows, rare elements, label propagation (WCC)"}
    return out


def ring_cards_for_answer(comp: dict, seed: str, template_cards=()) -> dict:
    """Proposed use of the component in the answer. Every component card other than the seed has atypical activity of
    its own in the window that shares a rare element with the ring (or was matched by the ring template).
    connected = template cards plus component cards; added = what the graph adds to the detector's list."""
    comp_cards = set(comp["cards"]) - {seed}
    tmpl = {str(c) for c in template_cards} - {seed}
    return {"connected": sorted(comp_cards | tmpl), "added": sorted(comp_cards - tmpl),
            "template_not_reached": sorted(tmpl - comp_cards)}


# ---------------------------------------------------------------------------------------------------------------
# agent hooks: registry tools and zero-weight, attested Signals (integration: docs/round2/STATUS.md, agent "tg-depth")

ELEMENT_WORDS = {"device": "device profile", "r_email": "recipient email domain", "region": "billing region",
                 "signature": "access signature", "template": "ring template"}


def _element_words(key: str) -> str:
    kind, _, val = key.partition(":")
    return f"{ELEMENT_WORDS.get(kind, kind)} {val}"


def register(T) -> None:
    """Register fused_precedent and ring_component on a kavach.tools.ToolRegistry, so both run like every other tool:
    traced, counted, bounded, fail closed, and re-runnable by `kavach attest` (which builds a fresh registry). On the
    TigerGraph and MCP lanes they run the installed queries through the lane's own `_q` (the MCP allowlist applies)."""
    lane = None if getattr(T, "backend", "duck") == "duck" else T._impl

    def fused_precedent_tool(card_ids, text, t0, k: int = 5, days: float = PRECEDENT_DAYS):
        return fused_precedent(lane, list(card_ids), text, t0, k=int(k), days=float(days))["hits"]

    def ring_component_tool(card_id, t_from, t_to, template_name: str = "", template_cards=()):
        tmpl = (template_name, list(template_cards)) if template_name and len(set(template_cards)) >= 2 else None
        r = ring_component(lane, card_id, t_from, t_to, template=tmpl)
        return {"card_id": r["cards"], "TransactionID": sorted({t for v in r["element_txns"].values() for t in v}),
                "elements": r["elements"], "edges": r["edges"], "element_txns": r["element_txns"],
                "n_components": r["n_components"], "largest": r["largest"], "n_atypical": r.get("n_atypical", 0)}

    T.register("fused_precedent", fused_precedent_tool)
    T.register("ring_component", ring_component_tool)


def precedent_signal(hits: list[dict], params: dict):
    """The best precedent reached through ANOTHER card (a shared rare element), as a zero-weight history Signal; None
    when every connected precedent sits on the case's own card (the existing closed_cases_for claim covers those)."""
    from kavach.assess import Signal
    cross = [h for h in hits if h["path"] not in ("card", "connected_card") and h.get("through")]
    if not cross:
        return None
    h = cross[0]
    el = next((v for v in h["via"] if v.split(":", 1)[0] in ("device", "r_email", "region")), h["via"][0])
    claim = (f"Graph and vector fused: closed case {h['case_id']} ({h['outcome']}, {h['pattern']}) is reached through the "
             f"{_element_words(el)}, which card {h['through'][0]} also used in the {int(params.get('days', PRECEDENT_DAYS))} days to "
             f"the alert, and its analyst note is the closest to this case among the connected closed cases (similarity "
             f"{h['similarity']:.2f})")
    return Signal("fused_precedent", "history", 0.0, claim, [h["case_id"], h["through"][0]],
                  f"query:fused_precedent(card_ids={','.join(params['card_ids'])})", "graph",
                  {"tool": "fused_precedent", "params": params})


def ring_signal(comp: dict, seed: str, params: dict, min_cards: int = 3):
    """The seed's ring component as a zero-weight network Signal (the cards and the transactions behind each shared
    element are the evidence ids); None for a component smaller than min_cards."""
    from kavach.assess import Signal
    cards = comp.get("card_id") or comp.get("cards") or []
    if len(cards) < min_cards:
        return None
    els = sorted(comp["elements"].items(), key=lambda kv: (-len(kv[1]), kv[0]))
    shared = "; ".join(f"{_element_words(k)} ({len(v)} cards)" for k, v in els[:3])
    txns = sorted({t for v in comp["element_txns"].values() for t in v})
    claim = (f"Graph algorithm (weakly connected components of the cards whose atypical transactions share a rare element, "
             f"{params['t_from'][:10]} to {params['t_to'][:10]}): card {seed} is in a component of {len(cards)} cards "
             f"({comp['n_components']} components in the window, the largest {comp['largest']}); shared: {shared}")
    return Signal("ring_component", "network", 0.0, claim, sorted(set(cards) - {seed})[:25] + txns[:25],
                  f"query:ring_wcc(seed={seed})", "graph", {"tool": "ring_component", "params": params})


# ---------------------------------------------------------------------------------------------------------------
# study and CLI

def _lane(backend: str):
    if backend == "duck":
        return None
    from kavach.graph.client import TgTools
    return TgTools()


def _answers(path=None) -> dict:
    """HHF answers: logs/tgdepth/dryrun.json (a no-write dry run) when given, else cases/*.json."""
    from kavach.config import ROOT
    if path:
        d = json.loads(open(path, encoding="utf-8").read())
        return {k: {"answer": v["answer"], "row": v["row"]} for k, v in d["cases"].items()}
    from kavach import duckq
    pack = {r["case_id"]: r for r in duckq.case_pack(2).to_dict("records")}
    out = {}
    for p in sorted((ROOT / "cases").glob("HHF-*.json")):
        a = json.loads(p.read_text(encoding="utf-8"))
        out[a["case_id"]] = {"answer": a, "row": pack.get(a["case_id"], {})}
    return out


def study(backend: str = "duck", answers_path=None, k: int = 5) -> list[dict]:
    """On every HHF case: fused precedent vs plain vector search (top k over all 5,585) vs plain traversal (same
    candidates, strongest path then most recent)."""
    from kavach import vectors
    lane = _lane(backend)
    rows = []
    for cid, v in sorted(_answers(answers_path).items()):
        a, row = v["answer"], v["row"]
        text = query_text(a)
        cards = [row["card_id"]]
        fp = fused_precedent(lane, cards, text, row["opened_at"], k=k)
        vec = [c for c, _ in vectors.search_cases(text, k=k)]
        trav = rank_traversal(fp["candidates"], k)
        fused = [h["case_id"] for h in fp["hits"]]
        rows.append({"case_id": cid, "verdict": a["case"]["verdict"], "n_candidates": fp["n_candidates"], "fused": fp["hits"],
                     "vector": vec, "traversal": trav, "fused_not_in_vector": [c for c in fused if c not in vec],
                     "fused_not_in_traversal": [c for c in fused if c not in trav]})
    return rows


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m kavach.graph.fusion")
    ap.add_argument("cmd", choices=["precedent", "ring", "study"])
    ap.add_argument("case_id", nargs="?")
    ap.add_argument("--backend", default="duck", choices=["duck", "tg"])
    ap.add_argument("--answers", default=None, help="dry-run cache (logs/tgdepth/dryrun.json); default cases/*.json")
    ap.add_argument("--k", type=int, default=5)
    ns = ap.parse_args(argv)
    if ns.cmd == "study":
        for r in study(ns.backend, ns.answers, ns.k):
            print(json.dumps({k: v for k, v in r.items()}, default=str))
        return 0
    v = _answers(ns.answers)[ns.case_id]
    a, row = v["answer"], v["row"]
    lane = _lane(ns.backend)
    if ns.cmd == "precedent":
        r = fused_precedent(lane, [row["card_id"]], query_text(a), row["opened_at"], k=ns.k)
        print(json.dumps({"hits": r["hits"], "n_candidates": r["n_candidates"], "how": r["how"]}, indent=1, default=str))
    else:
        t0 = pd.Timestamp(row["opened_at"])
        r = ring_component(lane, row["card_id"], t0 - timedelta(days=RING_DAYS), t0)
        print(json.dumps({k: r[k] for k in ("cards", "size", "n_components", "largest", "n_elements", "elements", "how")},
                         indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
