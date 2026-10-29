"""Miner: search every pattern family's grid over the bank's closed cases and a population sample.

For each grid point:
  support          confirmed-fraud closed cases whose first fraud transaction fires
  cleared_hits     cleared closed cases (legitimate customers) it would have acted against
  population_rate  share of a random sample of transactions that fires
  lift             P(fire | fraud) / P(fire | population)
  precision        support / (support + cleared_hits) among the closed cases
Candidates need support >= min_support, lift >= min_lift and cleared_hits <= max_cleared; they are ranked by
lift * ln(support) and accepted greedily when their fraud hit set overlaps less than 60 percent with every
already accepted candidate. A candidate whose hits overlap 60 percent or more with the hand-written detector of the
same family is kept but marked `rediscovery` (the miner found it without being told); the rest are `novel`.

`seed_from_notes()` clusters the analysts' `undocumented` notes with the stored embeddings and maps clusters to
families by keyword, so the report can say which analyst language led to which family. The mining itself never
reads the notes: it is blind to them, and the notes are only used afterwards to check what it found.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from kavach.grow import templates
from kavach.grow.templates import FAMILIES

NOVELTY = 0.60


@dataclass
class Candidate:
    name: str
    family: str
    params: dict
    rule: str
    support: int
    cleared_hits: int
    n_fraud: int
    n_cleared: int
    pop_hits: int
    n_pop: int
    population_rate: float
    lift: float
    precision: float
    score: float
    fraud_case_ids: list = field(default_factory=list)
    cleared_case_ids: list = field(default_factory=list)
    example_txn_ids: list = field(default_factory=list)
    status: str = "novel"                 # novel | rediscovery
    rediscovers: str = ""                 # hand-written detector it reproduces, if any
    overlaps: dict = field(default_factory=dict)
    rank: int = 0
    lane: str = "standard"                # standard | rare
    p_adj: float = 1.0                    # Bonferroni-corrected binomial p-value of the fraud hits
    patterns: dict = field(default_factory=dict)   # bank pattern labels among its fraud hits
    forward: dict = field(default_factory=dict)    # forward test on the newest month (no labels)

    def backtest(self) -> dict:
        return {"support": self.support, "cleared_hits": self.cleared_hits, "n_fraud_cases": self.n_fraud,
                "n_cleared_cases": self.n_cleared, "population_hits": self.pop_hits, "population_sample": self.n_pop,
                "population_rate": self.population_rate, "lift": self.lift, "precision": self.precision,
                "fraud_case_ids": self.fraud_case_ids[:50], "cleared_case_ids": self.cleared_case_ids,
                "example_txn_ids": self.example_txn_ids[:10], "patterns": self.patterns, "rank": self.rank,
                "lane": self.lane, "p_adj": self.p_adj,
                "status": self.status, "rediscovers": self.rediscovers, "forward": self.forward}


# results of the last mine() call, for reports (all grid rows, built-ins, seeds)
LAST: dict = {}


def _labels(con, labels_path=None) -> pd.DataFrame:
    """Closed cases with the transaction the miner evaluates: the first fraud transaction for fraud, the single
    transaction for cleared cases. Cleared cases without txn ids (the Round 1 investigations) fall back to the
    flagged transaction of the case-pack case their note names."""
    from kavach.config import RAW2
    path = Path(labels_path) if labels_path else RAW2 / "closed_cases_history.csv"
    if path.exists():
        con.execute(f"create or replace temp table lab_src as select * from read_csv('{templates._p(path)}', header=true, all_varchar=true)")
    else:
        con.execute("create or replace temp table lab_src as select * from src.closed")
    has_cases = bool(con.execute("select count(*) from duckdb_tables() where database_name = 'src' and table_name = 'cases'").fetchone()[0])
    pack = ("left join (select case_id k_id, any_value(flagged_txn_id) k_txn from src.cases group by 1) k "
            "on regexp_extract(c.analyst_notes, 'HH[A-Z]-[0-9]{3}') = k.k_id") if has_cases else \
        "left join (select null k_id, null k_txn) k on false"
    return con.execute(f"""
        create or replace temp table lab as
        select c.case_id, c.outcome, c.pattern,
               coalesce(try_cast(try_cast(c.first_fraud_txn_id as double) as bigint), try_cast(split_part(c.txn_ids, '|', 1) as bigint),
                        try_cast(k.k_txn as bigint)) TransactionID
        from lab_src c {pack};
        delete from lab where TransactionID is null or TransactionID not in (select TransactionID from t);
        select * from lab""").df()


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), summed upward (exact enough for small tail probabilities)."""
    if k <= 0:
        return 1.0
    p = min(max(p, 1e-12), 1 - 1e-12)
    total = 0.0
    for i in range(k, min(n, k + 400) + 1):
        lp = math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * math.log(p) + (n - i) * math.log1p(-p)
        total += math.exp(lp)
    return min(1.0, total)


def _evaluate(con, fam, params, ev: str) -> set:
    s = fam.stats(con, params, ev)
    if not len(s):
        return set()
    return set(s.TransactionID[fam.rule(s, params)].astype("int64").tolist())


def mine(duck_path=None, features_path=None, min_support: int = 15, min_lift: float = 3.0, max_cleared: int = 3,
         sample: int = 100000, seed: int = 42, families=None, labels_path=None, forward: bool = True,
         rare_support: int = 5, alpha: float = 0.01, verbose: bool = True) -> list[Candidate]:
    """Standard lane: support >= min_support, lift >= min_lift, cleared_hits <= max_cleared.
    Rare lane (patterns the bank has seen only a handful of times, like the analysts' own undocumented typologies):
    support >= rare_support, zero cleared hits, lift >= min_lift, and a one-sided binomial test of the fraud hits
    against the reference population rate that survives a Bonferroni correction over the whole grid (p * grid <= alpha)."""
    from kavach.config import FEATURES
    t_start = time.perf_counter()
    con = duckdb.connect()
    templates.clear_cache()
    fp = features_path if features_path else (FEATURES if Path(FEATURES).exists() else None)
    templates.prepare(con, duck_path, fp)
    lab = _labels(con, labels_path)
    # reference population: transactions in the labelled months that the bank did NOT tie to a confirmed-fraud case
    # (neither a case transaction nor a transaction on one of its connected cards within 30 days of the case), so a
    # known ring's own unlabelled members do not count against the pattern that describes them
    con.execute(f"""create or replace temp table tied as
        select distinct TransactionID from lab_src c, unnest(string_split(c.txn_ids, '|')) u(x)
          join t on t.TransactionID = try_cast(u.x as bigint) where c.outcome = 'confirmed_fraud'
        union
        select distinct t.TransactionID from lab_src c, unnest(string_split(c.connected_card_ids, '|')) u(x)
          join t on t.card_id = u.x and abs(epoch(t.ts) - epoch(cast(c.opened_at as timestamp))) <= 30 * 86400
          where c.outcome = 'confirmed_fraud'""")
    con.execute(f"""create or replace temp table pop as select TransactionID from
                    (select TransactionID from t where ts <= (select max(cast(opened_at as timestamp)) from lab_src)
                     and TransactionID not in (select TransactionID from tied))
                    order by hash(TransactionID + {int(seed)}), TransactionID limit {int(sample)}""")
    con.execute("create or replace temp table _ev as select TransactionID from lab union select TransactionID from pop")
    pop_ids = con.execute("select TransactionID from pop").df().TransactionID.to_numpy()
    fr = lab[lab.outcome == "confirmed_fraud"].reset_index(drop=True)
    cl = lab[lab.outcome == "cleared"].reset_index(drop=True)
    n_f, n_c, n_p = len(fr), len(cl), len(pop_ids)
    if verbose:
        print(f"mine: {n_f} confirmed-fraud and {n_c} cleared closed cases resolved, population sample {n_p:,}")

    rows, hitsets = [], {}
    fams = [FAMILIES[f] for f in (families or FAMILIES)]
    for fam in fams:
        t0 = time.perf_counter()
        for params in fam.grid():
            hit = _evaluate(con, fam, params, "_ev")
            arr = np.fromiter(hit, dtype="int64", count=len(hit)) if hit else np.array([], dtype="int64")
            fm = np.isin(fr.TransactionID.to_numpy(), arr)
            cm = np.isin(cl.TransactionID.to_numpy(), arr)
            ph = int(np.isin(pop_ids, arr).sum())
            sup, ch = int(fm.sum()), int(cm.sum())
            pf = sup / max(n_f, 1)
            pr = (ph + 0.5) / (n_p + 1)
            lift = pf / pr
            name = fam.slug(params)
            hitsets[name] = set(fr.case_id[fm])
            rows.append({"name": name, "family": fam.name, "params": params, "support": sup, "cleared_hits": ch,
                         "p_value": binom_sf(sup, n_f, (ph + 0.5) / (n_p + 1)),
                         "pop_hits": ph, "population_rate": round(ph / max(n_p, 1), 6), "lift": round(lift, 2),
                         "precision": round(sup / (sup + ch), 3) if sup + ch else 0.0,
                         "score": round(lift * math.log(sup), 2) if sup > 1 else 0.0,
                         "_fm": fm, "_cm": cm})
        if verbose:
            print(f"  {fam.name:18s} {len(fam.grid()):3d} grid points  {time.perf_counter() - t0:6.1f}s")

    # hand-written detectors of the same family, as hit sets on the same closed cases
    builtins = {}
    for fam in fams:
        for bname, params in fam.builtin():
            hit = _evaluate(con, fam, params, "_ev")
            arr = np.fromiter(hit, dtype="int64", count=len(hit)) if hit else np.array([], dtype="int64")
            b = builtins.setdefault((fam.name, bname), {"family": fam.name, "detector": bname, "params": [], "cases": set(), "cleared": set()})
            b["params"].append(params)
            b["cases"] |= set(fr.case_id[np.isin(fr.TransactionID.to_numpy(), arr)])
            b["cleared"] |= set(cl.case_id[np.isin(cl.TransactionID.to_numpy(), arr)])

    n_grid = max(len(rows), 1)
    for r in rows:
        r["p_adj"] = min(1.0, r["p_value"] * n_grid)
        std = r["support"] >= min_support and r["lift"] >= min_lift and r["cleared_hits"] <= max_cleared
        rare = (rare_support <= r["support"] < min_support and r["cleared_hits"] == 0 and r["lift"] >= min_lift
                and r["p_adj"] <= alpha)
        r["lane"] = "standard" if std else "rare" if rare else ""
    passing = [r for r in rows if r["lane"]]
    passing.sort(key=lambda r: (-r["score"], -r["support"], r["name"]))
    accepted: list[Candidate] = []
    rank = {r["name"]: i + 1 for i, r in enumerate(sorted(rows, key=lambda r: (-r["score"], -r["support"], r["name"])))}
    for r in passing:
        A = hitsets[r["name"]]
        ov = {c.name: round(len(A & set(c.fraud_case_ids)) / len(A), 3) for c in accepted}
        if ov and max(ov.values()) >= NOVELTY:
            r["dup_of"] = max(ov, key=ov.get)
            continue
        bov = {f"builtin:{b['detector']}": round(len(A & b["cases"]) / len(A), 3) for (fam, _), b in builtins.items() if fam == r["family"]}
        red = [k for k, v in bov.items() if v >= NOVELTY]
        fm, cm = r["_fm"], r["_cm"]
        c = Candidate(name=r["name"], family=r["family"], params=r["params"], rule=FAMILIES[r["family"]].text(r["params"]),
                      support=r["support"], cleared_hits=r["cleared_hits"], n_fraud=n_f, n_cleared=n_c, pop_hits=r["pop_hits"],
                      n_pop=n_p, population_rate=r["population_rate"], lift=r["lift"], precision=r["precision"], score=r["score"],
                      fraud_case_ids=sorted(fr.case_id[fm]), cleared_case_ids=sorted(cl.case_id[cm]),
                      example_txn_ids=[str(int(x)) for x in fr.TransactionID[fm].head(10)],
                      status="rediscovery" if red else "novel", rediscovers=red[0].split(":", 1)[1] if red else "",
                      overlaps={**bov, **ov}, rank=rank[r["name"]], lane=r["lane"], p_adj=r["p_adj"],
                      patterns=fr.pattern[fm].value_counts().to_dict())
        accepted.append(c)

    fwd = {}
    if forward:
        fwd = forward_test(con, accepted, verbose=verbose)
    LAST.clear()
    LAST.update({
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows], "hitsets": hitsets,
        "builtins": [{"family": b["family"], "detector": b["detector"], "params": b["params"], "support": len(b["cases"]),
                      "cleared_hits": len(b["cleared"])} for b in builtins.values()],
        "labels": lab, "n_fraud": n_f, "n_cleared": n_c, "n_pop": n_p, "forward": fwd,
        "settings": {"duck_path": str(duck_path or ""), "min_support": min_support, "min_lift": min_lift, "max_cleared": max_cleared,
                     "sample": sample, "seed": seed}, "seconds": round(time.perf_counter() - t_start, 1),
    })
    con.close()
    return accepted


def forward_test(con, cands: list[Candidate], verbose: bool = True) -> dict:
    """Fire every accepted candidate on the newest month (the grader-scope transactions, no labels yet) and list the
    cards and case-pack alerts it touches. Purely a report: it never changes a decision."""
    has = con.execute("select count(*) from duckdb_tables() where database_name = 'src' and table_name = 'grader_scope'").fetchone()[0]
    if not has:
        return {}
    templates.set_ids(con, con.execute("select TransactionID from src.grader_scope").df().TransactionID.tolist(), table="_fwd")
    n = con.execute("select count(*) from _fwd").fetchone()[0]
    pack = con.execute("""select case_id, cast(flagged_txn_id as bigint) tid, card_id from src.cases
                          where round = (select max(round) from src.cases)""").df() \
        if "round" in [r[0] for r in con.execute("describe src.cases").fetchall()] else pd.DataFrame(columns=["case_id", "tid", "card_id"])
    out = {}
    for c in cands:
        fam = FAMILIES[c.family]
        hit = _evaluate(con, fam, c.params, "_fwd")
        if hit:
            h = con.execute("select TransactionID, card_id from t where TransactionID in (select unnest(?))", [sorted(hit)]).df()
        else:
            h = pd.DataFrame(columns=["TransactionID", "card_id"])
        cards = h.card_id.value_counts()
        ent = c.params.get("entity") or ("device_key" if c.family == "new_device_proxy" else None)
        values = {}
        if ent and hit:
            values = {str(k): int(v) for k, v in con.execute(
                f"select {ent} v, count(distinct card_id) n from t where TransactionID in (select unnest(?)) group by 1 order by 2 desc limit 5",
                [sorted(hit)]).fetchall()}
        pack_txn = sorted(pack.case_id[pack.tid.isin(hit)])
        pack_card = sorted(pack.case_id[pack.card_id.isin(set(h.card_id))])
        c.forward = {"month_txns": int(n), "fired_txns": len(hit), "fired_cards": int(len(cards)),
                     "top_cards": cards.head(10).to_dict(), "entity_values": values,
                     "pack_flagged_txn_hits": pack_txn, "pack_card_hits": pack_card}
        out[c.name] = c.forward
        if verbose:
            print(f"  forward {c.name:42s} {len(hit):5d} txns {len(cards):4d} cards  pack alerts {pack_txn}  pack cards {pack_card}")
    return out


# ----------------------------------------------------------------------------------------------------------------
# analyst notes -> families (used to check the blind miner, never to steer it)

_NORM = [(re.compile(r"\b(?:C\d{5}-K\d|C\d{5}|CC-\d{4}|HH[A-Z]-\d{3})\b"), "<ID>"), (re.compile(r"\$?\d[\d,]*(?:\.\d+)?"), "<N>")]


def normalise(note: str) -> str:
    for rx, tok in _NORM:
        note = rx.sub(tok, note)
    return re.sub(r"\s+", " ", note).strip()


def _cluster(vecs: np.ndarray, threshold: float) -> list[int]:
    """Average-link agglomerative clustering on cosine similarity (numpy only)."""
    clusters = [[i] for i in range(len(vecs))]
    sim = vecs @ vecs.T
    while len(clusters) > 1:
        best, pair = -1.0, None
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                s = float(sim[np.ix_(clusters[a], clusters[b])].mean())
                if s > best:
                    best, pair = s, (a, b)
        if best < threshold:
            break
        a, b = pair
        clusters[a] += clusters.pop(b)
    lab = [0] * len(vecs)
    for k, members in enumerate(clusters):
        for i in members:
            lab[i] = k
    return lab


def seed_from_notes(labels: pd.DataFrame | None = None, threshold: float = 0.85, pattern: str = "undocumented") -> list[dict]:
    """Cluster the analyst notes of `pattern` cases and map each cluster to pattern families by keyword."""
    from kavach import vectors
    from kavach.config import RAW2
    if labels is None or "analyst_notes" not in labels:
        labels = pd.read_csv(RAW2 / "closed_cases_history.csv", dtype=str)
    notes = labels[labels.pattern == pattern][["case_id", "analyst_notes"]].dropna().reset_index(drop=True)
    if notes.empty:
        return []
    ix = vectors.index()
    pos = {str(c): i for i, c in enumerate(ix["case_ids"])}
    vecs, source = [], []
    missing = [i for i, c in enumerate(notes.case_id) if c not in pos]
    extra = {}
    if missing:
        try:
            ev = vectors.embed([f"confirmed_fraud {pattern}: {notes.analyst_notes[i]}" for i in missing])
            extra = {i: ev[k] for k, i in enumerate(missing)}
        except Exception as e:  # the local embedding model is optional; stored vectors cover the older notes
            print(f"  seed_from_notes: could not embed {len(missing)} newer notes ({type(e).__name__}); they are clustered alone")
    for i, c in enumerate(notes.case_id):
        if c in pos:
            vecs.append(ix["case_vecs"][pos[c]]); source.append("stored")
        elif i in extra:
            vecs.append(extra[i]); source.append("embedded")
        else:
            v = np.zeros(ix["case_vecs"].shape[1], dtype=np.float32); v[i % len(v)] = 1.0
            vecs.append(v); source.append("none")
    V = np.vstack(vecs)
    V = V / np.maximum(np.linalg.norm(V, axis=1, keepdims=True), 1e-9)
    lab = _cluster(V, threshold)
    out = []
    for k in sorted(set(lab)):
        idx = [i for i, x in enumerate(lab) if x == k]
        sub = V[idx]
        medoid = idx[int(np.argmax((sub @ sub.T).sum(axis=1)))]
        text = " ".join(notes.analyst_notes[i].lower() for i in idx)
        fams = []
        for name, fam in FAMILIES.items():
            kws = [w for w in fam.keywords if w in text]
            if kws:
                fams.append({"family": name, "keywords": kws})
        fams.sort(key=lambda x: -len(x["keywords"]))
        out.append({"cluster": k, "n": len(idx), "case_ids": [notes.case_id[i] for i in idx],
                    "exemplar": normalise(notes.analyst_notes[medoid]), "families": fams,
                    "vectors": sorted(set(source[i] for i in idx))})
    return out


def seed_coverage(seeds: list[dict], cands: list[Candidate]) -> list[dict]:
    """For each note cluster: which grid point of each mapped family covers most of its cases (blind ranks), and which
    accepted candidates fire on its cases."""
    rows, hitsets = LAST.get("rows", []), LAST.get("hitsets", {})
    out = []
    for s in seeds:
        cases = set(s["case_ids"])
        best = []
        for m in s["families"]:              # a cluster without analyst language is linked through accepted candidates only
            fr = [r for r in rows if r["family"] == m["family"]]
            if not fr:
                continue
            r = max(fr, key=lambda r: (len(hitsets[r["name"]] & cases), r["score"]))
            if len(hitsets[r["name"]] & cases):
                best.append({"family": m["family"], "grid_point": r["name"], "covers": len(hitsets[r["name"]] & cases),
                             "of": len(cases), "support": r["support"], "cleared_hits": r["cleared_hits"], "lift": r["lift"]})
        acc = [{"candidate": c.name, "covers": len(set(c.fraud_case_ids) & cases)} for c in cands if set(c.fraud_case_ids) & cases]
        out.append({**s, "best_grid_points": best, "accepted_candidates": acc})
    return out


def save(cands: list[Candidate], path, seeds=None) -> Path:
    from kavach.grow.registry import dataset_version
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"dataset_version": dataset_version(LAST.get("settings", {}).get("duck_path") or None), "settings": LAST.get("settings", {}),
           "n_fraud": LAST.get("n_fraud"), "n_cleared": LAST.get("n_cleared"), "n_pop": LAST.get("n_pop"),
           "seconds": LAST.get("seconds"), "builtins": LAST.get("builtins", []),
           "candidates": [asdict(c) for c in cands],
           "grid": sorted(LAST.get("rows", []), key=lambda r: -r["score"]), "seeds": seeds or []}
    path.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    return path


def load(path) -> list[Candidate]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Candidate(**c) for c in doc["candidates"]]


def table(cands: list[Candidate]) -> str:
    lines = ["| rank | name | lane | status | support | cleared | pop rate | lift | precision | p (Bonferroni) | score |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in cands:
        st = c.status + (f" of {c.rediscovers}" if c.rediscovers else "")
        lines.append(f"| {c.rank} | {c.name} | {c.lane} | {st} | {c.support} | {c.cleared_hits} | {100 * c.population_rate:.3f}% | "
                     f"{c.lift:.1f} | {c.precision:.3f} | {c.p_adj:.1e} | {c.score:.1f} |")
    return "\n".join(lines)


def parity_check(spec: dict, txn_ids, backend: str = "duck") -> dict:
    """Run the runtime apply() for each transaction (through a real ToolRegistry) and compare with the SQL fire()
    on the same ids: the backtest numbers only describe the runtime detector if the two agree."""
    from types import SimpleNamespace

    from kavach import duckq
    from kavach.tools import ToolRegistry
    con = duckdb.connect()
    templates.prepare(con)
    fam = FAMILIES[spec["family"]]
    fired = fam.fire(con, spec["params"], list(txn_ids)).set_index("TransactionID").fired
    con.close()
    out = {"n": 0, "agree": 0, "disagree": []}
    for tid in txn_ids:
        f = duckq.txn_detail(int(tid))
        if not f:
            continue
        dev = templates._clean_device(f.get("device_key"))
        T = ToolRegistry(backend)
        ctx = SimpleNamespace(f=f, card_id=f["card_id"], t0=pd.Timestamp(f["ts"]), device=dev,
                              device_pop=duckq.device_popularity(dev) if dev else 0, tools=T)
        got = bool(fam.apply(ctx, {**spec, "backtest": spec.get("backtest", {}), "approval": spec.get("approval")}))
        out["n"] += 1
        if got == bool(fired.get(int(tid), False)):
            out["agree"] += 1
        else:
            out["disagree"].append({"txn": int(tid), "sql": bool(fired.get(int(tid), False)), "runtime": got})
    return out
