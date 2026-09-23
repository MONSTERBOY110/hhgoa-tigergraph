"""Text embeddings for GraphRAG: closed-case analyst notes, the README's pattern and policy sections.

Embeddings are 384-d (BAAI/bge-small-en-v1.5 via fastembed, local, no API). They are stored in
data/vectors.npz for the local lane and upserted into TigerVector attributes on the TigerGraph lane.
"""
import re

import numpy as np

from kavach.config import DATA, ROOT

VEC_PATH = DATA / "vectors.npz"
MODEL = "BAAI/bge-small-en-v1.5"
_model = None
_index = None


def model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL, cache_dir=str(DATA / "models"))
    return _model


def embed(texts: list[str]) -> np.ndarray:
    v = np.array(list(model().embed(texts)), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def policy_docs() -> list[dict]:
    """Sections of the organizer README that the agent cites: the five patterns, rules R1 to R10, 3a, 3b, 4, 5, 6."""
    text = (ROOT / "docs" / "TASK-README.md").read_text(encoding="utf-8")
    docs = []
    pat = text[text.index("## The five known fraud patterns"):text.index("## Regulatory references")]
    for m in re.finditer(r"\*\*(\d)\. ([^*]+)\*\*(.*?)(?=\n\*\*\d\.|\Z)", pat, re.S):
        docs.append({"id": f"PATTERN-{m.group(1)}", "section": f"Known fraud patterns: {m.group(2).strip('. ')}", "text": m.group(3).strip()})
    pol = text[text.index("### 3. Rules"):text.index("### 4. Exposure")]
    for m in re.finditer(r"\*\*(R\d+)\. ([^*]+)\*\*(.*?)(?=\n\*\*R\d+\.|\n### |\Z)", pol, re.S):
        docs.append({"id": f"POLICY-{m.group(1)}", "section": f"Fraud Policy {m.group(1)}: {m.group(2).strip('. ')}", "text": m.group(3).strip()})
    for head, key in (("### 3a. A case is not a report", "POLICY-3a"), ("### 3b. The next best action can change", "POLICY-3b"),
                      ("### 5. Gathering more evidence", "POLICY-5"), ("### 6. Stopping", "POLICY-6")):
        i = text.index(head)
        j = text.index("\n### ", i + 5)
        docs.append({"id": key, "section": f"Fraud Policy {head.lstrip('# ')}", "text": text[i + len(head):j].strip()})
    return docs


def build() -> dict:
    from kavach import duckq
    closed = duckq.q("select case_id, outcome, pattern, analyst_notes from closed order by case_id")
    notes = [f"{r.outcome} {r.pattern}: {r.analyst_notes}" for r in closed.itertuples()]
    docs = policy_docs()
    cv = embed(notes)
    dv = embed([f"{d['section']}. {d['text']}" for d in docs])
    np.savez_compressed(VEC_PATH, case_ids=np.array(closed.case_id, dtype=str), case_vecs=cv,
                        doc_ids=np.array([d["id"] for d in docs], dtype=str), doc_sections=np.array([d["section"] for d in docs], dtype=str),
                        doc_texts=np.array([d["text"] for d in docs], dtype=str), doc_vecs=dv)
    print(f"embedded {len(notes)} closed-case notes and {len(docs)} policy/pattern sections -> {VEC_PATH}")
    return {"cases": len(notes), "docs": len(docs)}


def index():
    global _index
    if _index is None:
        if not VEC_PATH.exists():
            build()
        _index = dict(np.load(VEC_PATH, allow_pickle=False))
    return _index


def search_cases(text: str, k: int = 5, outcome_ids: set | None = None) -> list[tuple[str, float]]:
    ix = index()
    q = embed([text])[0]
    s = ix["case_vecs"] @ q
    order = np.argsort(-s)
    out = []
    for i in order:
        cid = str(ix["case_ids"][i])
        if outcome_ids is not None and cid not in outcome_ids:
            continue
        out.append((cid, round(float(s[i]), 4)))
        if len(out) >= k:
            break
    return out


def search_docs(text: str, k: int = 2) -> list[dict]:
    ix = index()
    q = embed([text])[0]
    s = ix["doc_vecs"] @ q
    return [{"id": str(ix["doc_ids"][i]), "section": str(ix["doc_sections"][i]), "text": str(ix["doc_texts"][i]),
             "score": round(float(s[i]), 4)} for i in np.argsort(-s)[:k]]
