"""Challenge 4, evidence to SAR: a suspicious activity report an analyst could send with minor edits.

    python -m kavach.filing HHF-006 | --all [--cases-dir cases] [--attest-dir attestations] [--out sar]
    python -m kavach.filing verify HHF-006 | --all [--no-rerun]

For every answer file whose sar.file is true, and only when policy section 3a requires a report, this writes
sar/<case>.md and sar/<case>.html (self-contained: inline CSS, inline SVG, no external assets, readable printed):
header (filing institution placeholders, the customer and cards concerned, activity dates, total amount, the
activity category in plain words and the policy basis from sar.reason), the narrative with a citation footnote on
every sentence, the transaction timeline (every affected transaction), the connected cards with what links them, and
the evidence graph drawn from the data. sar/manifest.json records every answer's outcome: filed, refused (sar.file is
true but 3a does not require a report, or the answer is inconsistent), missing (3a requires a report but sar.file is
false), not_required, or error (fail closed), with the reason.

Citations. The narrative is the answer's own sar.narrative, sentence by sentence, taken from the attestation sidecar
(attestations/<case>.json, kind sar_sentence). Each footnote names the sentence's claim, the evidence claims that
support it, and the ids it rests on. A sentence whose claim cites no ids is replaced by the closest template sentence
that has ids (kavach.attest.sar_sentences_template over facts rebuilt from the answer and the index), keeping only ids
the sidecar attests; if none remain the sentence is dropped and the drop is recorded. No uncited sentence is written.

Fail closed: no sidecar, a sidecar whose SAR sentences do not reproduce the answer's narrative, an affected
transaction or a cited id that is not in the index -> no filing for that case, the reason in the manifest, exit 1.
Nothing here is shown to the customer; every filing carries the instruction not to disclose it to the subject.
The alert's free text (trigger_text) is never read: it is data, never instruction, and has no place in a filing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
import sys
from pathlib import Path

from kavach.attest import split_ids
from kavach.config import CASES_DIR, ROOT
from kavach.llm import DASH_RE

OUT_DIR = ROOT / "sar"
SAR_EXPOSURE = 1000.0      # section 3a: exposure over $1,000
STRONG_P = 0.70            # section 3a "strongly suspected": the R1 bar for acting without verification
TXNS_IN_GRAPH = 14
CARDS_IN_GRAPH = 24
ID_KINDS = (("txn_ids", "Transactions"), ("card_ids", "Cards"), ("customer_ids", "Customers"),
            ("case_ids", "Closed cases"), ("device_keys", "Device profiles"))
KEYS = tuple(k for k, _ in ID_KINDS)
FILED, REFUSED, MISSING, NOT_REQUIRED, ERROR = "filed", "refused", "missing", "not_required", "error"

CATEGORY = {
    "card_testing": "Card fraud: card testing (small authorizations probing a stolen card number before a larger purchase)",
    "card_not_present_fraud": "Card fraud: card-not-present (the card number used online without the card)",
    "card_not_present_new_device": "Card fraud: card-not-present from a device new to the account",
    "out_of_region_use": "Card fraud: card-present use in a billing region inconsistent with the cardholder",
    "account_takeover": "Account takeover: an unauthorized party controlling the cardholder's account",
    "undocumented": "Card fraud: coordinated activity across customers that fits none of the documented typologies",
}
CONTAINMENT = ("BLOCK_CARD", "BLOCK_ALL_CARDS", "DECLINE_TRANSACTION", "STEP_UP_AUTH", "MONITOR_CONNECTED_CARDS")
TIMELINE_SQL = ("select TransactionID, strftime(ts, '%Y-%m-%d %H:%M:%S') ts, TransactionAmt amt, ProductCD, channel, "
                "cast(addr1 as integer) addr1, device_key, card_id, customer_id, card4, card6 "
                "from txn where TransactionID in (select unnest(?)) order by ts, TransactionID")
CARDS_SQL = ("select card_id, any_value(customer_id) customer_id, any_value(card4) card4, any_value(card6) card6 "
             "from txn where card_id in (select unnest(?)) group by card_id")
NO_TIPOFF = ("Confidential. Do not disclose this report, or that a report is being considered or has been filed, to the "
             "subject, the cardholder or anyone outside the bank's fraud and compliance staff (no tip-off). "
             "Nothing in this document is sent to the customer.")


class FilingError(Exception):
    """A reason to produce no filing for a case (fail closed)."""


# ---------------------------------------------------------------------------------------------------------------
# small helpers
def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(p) -> str:
    p = Path(p)
    try:
        return str(p.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:            # outside the repo: never ship a local machine path
        return "/".join(p.parts[-2:])


def _read_json(p: Path):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() in ("", "nan", "None", "NaT", "<NA>")


def _money(x) -> str:
    return f"${float(x):,.2f}"


def _undash(t: str) -> str:
    """The team rule (no en or em dashes) holds for every byte we write, attested text included."""
    return DASH_RE.sub(", ", str(t))


def _claim_num(cid: str) -> int:
    m = re.search(r"\d+", cid or "")
    return int(m.group()) if m else 0


def _ids(ev: dict | None) -> dict:
    ev = ev or {}
    return {k: [str(x) for x in ev.get(k) or []] for k in KEYS}


def _flat(ev: dict) -> list[str]:
    return [x for k in KEYS for x in ev.get(k, [])]


def _attest_dir(d=None) -> Path:
    from kavach.attest import attest_dir
    return Path(d) if d else attest_dir()


def _q(sql: str, *params):
    from kavach import duckq
    return duckq.q(sql, *params)


def _lookup(ev: dict) -> set:
    """Which of the given ids exist in the index: kavach.attest's lookup_ids (the re-run of every SAR sentence) for
    transactions, cards, customers and closed cases. Device keys are matched with surrounding whitespace trimmed on
    both sides: the sidecar records ids stripped (attest._norm_scalar), while a device key with an empty screen or
    device field ends or starts with a space in the index ("SM-J700T1 |  |  | ")."""
    from kavach.attest import LOCAL_TOOLS
    found = set(LOCAL_TOOLS["lookup_ids"](**{k: v for k, v in ev.items() if v and k != "device_keys"}))
    devs = [str(x).strip() for x in ev.get("device_keys") or []]
    if devs:
        found |= set(_q("select distinct trim(device_key) k from txn where trim(device_key) in (select unnest(?))", devs).k)
    return found


# ---------------------------------------------------------------------------------------------------------------
# section 3a: does a report exist at all
def section_3a(answer: dict) -> dict:
    """Policy section 3a, read from the answer alone: a report when fraud is confirmed or strongly suspected AND at
    least one of: exposure over $1,000; a connection to a shared device profile or another card's fraud (connected
    cards or device profiles listed); a coordinated or undocumented pattern (R9)."""
    c = answer.get("case") or {}
    verdict = str(c.get("verdict") or "")
    p = float(c.get("fraud_probability") or 0.0)
    exposure = float(c.get("exposure_usd") or 0.0)
    conn = [str(x) for x in c.get("connected_card_ids") or []]
    devs = [str(x) for x in c.get("connected_device_profiles") or []]
    pattern = str(c.get("pattern") or "none")
    suspected = verdict == "fraud" or (verdict != "legitimate" and p >= STRONG_P)
    conds = {"exposure_over_1000": exposure > SAR_EXPOSURE, "connected": bool(conn or devs),
             "undocumented_pattern": pattern == "undocumented"}
    words = {"exposure_over_1000": f"exposure {_money(exposure)} exceeds $1,000",
             "connected": (f"the activity connects to {len(conn)} other card(s)" if conn else "the activity connects")
                          + (f" and {len(devs)} shared device profile(s)" if devs else ""),
             "undocumented_pattern": "the pattern is coordinated and undocumented (R9)"}
    if not suspected:
        reason = (f"§3a: no report: the verdict is {verdict or 'missing'} with fraud probability {p:.2f}; a report needs "
                  f"fraud confirmed or strongly suspected (fraud verdict, or p at or above {STRONG_P:.2f})")
    elif not any(conds.values()):
        reason = (f"§3a: no report: fraud is {'confirmed' if verdict == 'fraud' else 'strongly suspected'} but exposure "
                  f"{_money(exposure)} is not over $1,000, no connected card or shared device profile is listed and the "
                  f"pattern ({pattern}) is documented, so a case without a report")
    else:
        reason = (f"§3a: report owed: fraud {'confirmed' if verdict == 'fraud' else 'strongly suspected'} "
                  f"(verdict {verdict}, p {p:.2f}) and " + "; ".join(words[k] for k, v in conds.items() if v))
    return {"required": suspected and any(conds.values()), "suspected": suspected, "verdict": verdict, "p": p,
            "exposure": exposure, "n_connected": len(conn), "n_devices": len(devs), "pattern": pattern,
            "conditions": conds, "reason": reason}


def decide(answer: dict) -> tuple[str, str, dict]:
    """(status, reason, section_3a) for one answer: filed, refused, missing or not_required."""
    sar = answer.get("sar") or {}
    final = {x.get("action") for x in (answer.get("next_best_actions") or {}).get("final") or []}
    s = section_3a(answer)
    if sar.get("file") is True:
        if not s["required"]:
            return REFUSED, "sar.file is true but section 3a does not require a report. " + s["reason"], s
        if "FILE_REPORT" not in final:
            return REFUSED, "sar.file is true but FILE_REPORT is not in the final actions; the filing would not match the recommendation", s
        if "CREATE_CASE" not in final:
            return REFUSED, "§3a: a report always has a case behind it, and CREATE_CASE is not in the final actions", s
        if not _norm(sar.get("narrative")):
            return REFUSED, "sar.file is true but the answer has no narrative to file", s
        return FILED, s["reason"], s
    if s["required"]:
        return MISSING, ("section 3a requires a report but sar.file is false; no filing is drafted and the answer needs "
                         "review. " + s["reason"]), s
    return NOT_REQUIRED, s["reason"], s


# ---------------------------------------------------------------------------------------------------------------
# facts from the index
def _case_row(case_id: str) -> dict:
    try:
        df = _q("select case_id, flagged_txn_id, card_id, customer_id, opened_at, trigger_type from cases where case_id = ?",
                case_id)
    except Exception:
        return {}
    return {k: (None if _blank(v) else str(v)) for k, v in df.iloc[0].to_dict().items()} if len(df) else {}


def _timeline(answer: dict, flagged: str | None) -> list[dict]:
    c = answer["case"]
    ids = [str(x) for x in c.get("affected_txn_ids") or []]
    if not ids:
        raise FilingError("the answer lists no affected transactions")
    bad = [x for x in ids if not x.isdigit()]
    if bad:
        raise FilingError(f"affected transaction ids are not transaction ids: {bad[:4]}")
    df = _q(TIMELINE_SQL, [int(x) for x in ids])
    found = {str(int(x)) for x in df.TransactionID}
    missing = [x for x in ids if x not in found]
    if missing:
        raise FilingError(f"{len(missing)} affected transaction(s) are not in the index: {', '.join(missing[:4])}")
    first = str(c.get("first_suspicious_txn_id") or "")
    rows = []
    for r in df.itertuples():
        tid = str(int(r.TransactionID))
        rows.append({"id": tid, "ts": str(r.ts), "amt": round(float(r.amt), 2), "product": str(r.ProductCD),
                     "channel": str(r.channel).replace("_", " "), "region": None if _blank(r.addr1) else str(int(r.addr1)),
                     "device": None if _blank(r.device_key) else str(r.device_key), "card": str(r.card_id),
                     "customer": str(r.customer_id), "card4": None if _blank(r.card4) else str(r.card4),
                     "card6": None if _blank(r.card6) else str(r.card6), "flagged": tid == str(flagged or ""),
                     "first": tid == first})
    return rows


def _cards(card_ids: list[str]) -> dict:
    if not card_ids:
        return {}
    df = _q(CARDS_SQL, list(card_ids))
    return {str(r.card_id): {"customer": str(r.customer_id), "desc": " ".join(str(x) for x in (r.card4, r.card6) if not _blank(x))}
            for r in df.itertuples()}


def _ring(answer: dict, sidecar: dict, flagged: str | None) -> dict | None:
    """kavach.site.ring_frames: every transaction of the case card and the connected cards in the window, the element
    they share and when each card joined, built from the DuckDB index (read-only)."""
    if not (answer["case"].get("connected_card_ids") or []):
        return None
    try:
        from kavach.site import ring_frames
        return ring_frames(answer, sidecar, flagged_txn_id=flagged)
    except Exception as e:  # noqa: BLE001  (the link details are supplementary; the cards themselves are attested)
        return {"available": False, "reason": f"{type(e).__name__}: {str(e)[:160]}"}


LINK_LABEL = {"R_emaildomain": "recipient email domain", "P_emaildomain": "purchaser email domain", "addr1": "billing region",
              "device_key": "device profile", "product_band": "product and amount band",
              "access_signature": "access signature (product, proxy, device flag, device type)", "holder_key": "account key"}


def _link_from_ring(ring: dict, affected: set) -> dict | None:
    p = ring.get("primary") if ring and ring.get("available") else None
    if not p:
        return None
    per = {m["card"]: {"n": int(m.get("n_primary") or 0), "amt": float(m.get("amt_primary") or 0.0), "first_ts": m.get("joined_ts"),
                       "first_txn": m.get("joined_txn")} for m in ring.get("members") or [] if m.get("n_primary")}
    w = ring.get("window") or {}
    return {"label": p["label"], "value": p["value"], "field": p["field"], "per_card": per,
            "case_txns": {r["id"] for r in ring.get("txns") or [] if r.get("primary") and r["id"] in affected},
            "source": (f"the ring query over the case card and the connected cards, {w.get('query_from')} to {w.get('query_to')} "
                       f"({w.get('source')}); re-run with {(ring.get('built') or {}).get('rerun', '')}")}


def _link_from_claim(answer: dict, sidecar: dict, affected: set) -> dict | None:
    """When no single field links the ring (a template ring: cards that share no device), the link is what the
    attested claim naming the most connected cards queried: its recorded query is re-run on the DuckDB index and the
    rows it returns give each card's transactions and amount through that value."""
    conn = {str(x) for x in answer["case"].get("connected_card_ids") or []}
    best = None
    for cl in sidecar.get("claims") or []:
        p = (cl.get("how") or {}).get("params") or {}
        value = p.get("value") or p.get("device_key") or p.get("domain")
        field = p.get("entity_field") or p.get("entity") or ("device_key" if p.get("device_key") else p.get("field") or "")
        n = len(conn & set(_ids(cl.get("evidence"))["card_ids"]))
        if cl.get("kind") in ("evidence", "detector_hit") and value and n >= 2 and (best is None or n > best[0]):
            best = (n, cl, str(field), str(value))
    if best is None:
        return None
    _, cl, field, value = best
    from kavach.tools import ToolRegistry
    df = ToolRegistry("duck").call(cl["how"]["tool"], **cl["how"]["params"])
    if not hasattr(df, "columns") or "card_id" not in df.columns:
        return None
    per = {}
    for card, g in df.groupby("card_id"):
        g = g.sort_values([c for c in ("ts", "TransactionID") if c in g.columns]) if "ts" in g.columns else g
        per[str(card)] = {"n": len(g), "amt": round(float(g["amt"].sum()), 2) if "amt" in g.columns else None,
                          "first_ts": str(g["ts"].iloc[0])[:19] if "ts" in g.columns else None,
                          "first_txn": str(int(g["TransactionID"].iloc[0])) if "TransactionID" in g.columns else None}
    txns = {str(int(x)) for x in df["TransactionID"]} if "TransactionID" in df.columns else set()
    return {"label": LINK_LABEL.get(field, field or "shared value"), "value": value, "field": field, "per_card": per,
            "case_txns": txns & affected, "claim": cl["id"],
            "source": f"a re-run of the query recorded in attested claim {cl['id']}: {cl['how']['tool']}("
                      f"{json.dumps(cl['how']['params'], sort_keys=True, default=str)}) on the DuckDB index"}


def _approval(case_id: str) -> dict:
    try:
        from kavach.harness import approvals
        recs = [r for r in approvals.records(case_id) if r.get("action") == "FILE_REPORT"]
        chain = approvals.verify_detail()
    except Exception as e:  # noqa: BLE001
        return {"status": "unknown", "text": f"approval log unreadable ({type(e).__name__}); treat as not approved"}
    ok = [r for r in recs if r.get("status") == "approved" and r.get("role") == "L2"]
    if ok:
        r = ok[-1]
        return {"status": "approved", "record": r.get("id"), "ts": r.get("ts"), "chain_ok": chain.get("ok"),
                "text": f"FILE_REPORT approved by role L2 at {r.get('ts')} (approval record {r.get('id')}); "
                        f"approval chain {'verified' if chain.get('ok') else 'BROKEN: ' + str(chain.get('reason'))}"}
    denied = len([r for r in recs if r.get("status") == "denied"])
    return {"status": "pending", "chain_ok": chain.get("ok"),
            "text": "FILE_REPORT is an L2 (fraud manager) action and no L2 approval is recorded yet; this draft must not be "
                    "sent before one is" + (f" ({denied} denied attempt(s) on record)" if denied else "")}


# ---------------------------------------------------------------------------------------------------------------
# narrative sentences with citations
def _template(answer: dict, ctx: dict) -> list[tuple[str, list[str]]]:
    """The deterministic SAR sentences (kavach.attest.sar_sentences_template) over facts rebuilt from the answer and
    the index; the source a sentence without ids is rewritten from."""
    from kavach.attest import sar_sentences_template
    from kavach.narrative import PATTERN_WORDS
    c, sar, rows = answer["case"], answer["sar"], ctx["rows"]
    pattern = c.get("pattern") if c.get("pattern") in PATTERN_WORDS else "none"
    chans = sorted({r["channel"] for r in rows})
    prods = sorted({r["product"] for r in rows})
    regions = sorted({r["region"] for r in rows if r["region"]})
    devs = sorted({r["device"] for r in rows if r["device"]})
    where = (f"{' and '.join(chans)}, product code(s) {', '.join(prods)}"
             + (f", billing region(s) {', '.join(regions)}" if regions else "")
             + (f", from device profile(s) {'; '.join(devs[:2])}" if devs else ""))
    how = {"card_testing": "small online authorizations followed by a larger purchase",
           "undocumented": "purchases linked across several unrelated customers' cards",
           "account_takeover": "made through an account controlled by an unauthorized party"}.get(
        pattern, "online purchases inconsistent with the cardholder" if "online" in chans
        else "card-present purchases inconsistent with the cardholder")
    ev = [cl for cl in ctx["sidecar"].get("claims") or [] if cl.get("kind") == "evidence" and _flat(_ids(cl.get("evidence")))]
    final = (answer.get("next_best_actions") or {}).get("final") or []
    facts = {
        "customer_id": ctx["customer"], "card_id": ctx["card"], "card_desc": ctx["card_desc"] or "card",
        "episode_txns": [{"id": r["id"], "device": r["device"]} for r in rows],
        "date_from": rows[0]["ts"][:10], "date_to": rows[-1]["ts"][:10], "n_txns": len(rows),
        "exposure": float(c.get("exposure_usd") or 0.0), "first_txn": str(c.get("first_suspicious_txn_id") or rows[0]["id"]),
        "where": where, "how": how, "pattern": pattern, "top_fraud": ev[0]["what"] if ev else "see the attested evidence",
        "n_connected": len(c.get("connected_card_ids") or []), "connected": [str(x) for x in c.get("connected_card_ids") or []][:8],
        "shared": ctx["shared_label"] or "shared element", "prior_cases": [str(x) for x in c.get("similar_prior_cases") or []][:3],
        "customer_statement": ("The cardholder's reply to the bank's request is recorded in the case file."
                               if answer.get("evidence_requests") else "The cardholder has not yet been contacted."),
        "containment": ", ".join(x["action"] for x in final if x.get("action") in CONTAINMENT),
        "rule": ", ".join(dict.fromkeys(re.findall(r"R\d+|§3a", str(sar.get("reason") or "")))) or "§3a",
    }
    try:
        return sar_sentences_template(facts)
    except Exception:  # noqa: BLE001  (no template: a sentence without ids is then dropped, never emitted uncited)
        return []


def _words(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9$.,-]+", t.lower()) if len(w) > 2}


def _closest(text: str, template: list) -> tuple[str, list[str]] | None:
    cands = [(s, ids) for s, ids in template if ids]
    if not cands:
        return None
    w = _words(text)
    return max(cands, key=lambda ts: len(w & _words(ts[0])) / (len(w | _words(ts[0])) or 1))


def _claims_with(ids: list[str], claims: dict, kinds=("evidence", "detector_hit")) -> list[str]:
    s = set(ids)
    return [cid for cid, c in claims.items() if c.get("kind") in kinds and s & set(_flat(_ids(c.get("evidence"))))]


def sentences(answer: dict, sidecar: dict, template: list | None = None) -> tuple[list[dict], list[dict]]:
    """The narrative as footnoted sentences, from the sidecar's sar_sentence claims. Returns (sentences, dropped)."""
    claims = {c["id"]: c for c in sidecar.get("claims") or [] if c.get("id")}
    s_claims = sorted([c for c in claims.values() if c.get("kind") == "sar_sentence"], key=lambda c: _claim_num(c["id"]))
    if not s_claims:
        raise FilingError("the attestation sidecar holds no SAR sentence claims")
    if _norm(" ".join(c.get("what", "") for c in s_claims)) != _norm(answer["sar"].get("narrative")):
        raise FilingError("the sidecar's SAR sentences do not reproduce the answer's narrative (stale sidecar?); "
                          "re-run the case so answer and attestation come from the same run")
    attested = {x for c in claims.values() for x in _flat(_ids(c.get("evidence")))}
    first = str(answer["case"].get("first_suspicious_txn_id") or "")
    episode = (next((c for c in s_claims if first and first in _ids(c.get("evidence"))["txn_ids"]), None)
               or max(s_claims, key=lambda c: len(_ids(c.get("evidence"))["txn_ids"])))
    if not _ids(episode.get("evidence"))["txn_ids"]:
        episode = None
    out, dropped = [], []
    for c in s_claims:
        ev = _ids(c.get("evidence"))
        text, sup, rewritten, original = c.get("what", ""), [x for x in c.get("supported_by") or [] if x in claims], False, ""
        if not _flat(ev):
            alt = _closest(text, template or [])
            keep = [str(i) for i in (alt[1] if alt else []) if str(i) in attested]
            if not keep:
                dropped.append({"claim": c["id"], "text": text,
                                "reason": "the sentence cites no ids and no template sentence with attested ids replaces it"})
                continue
            original, text, rewritten = text, alt[0], True
            ev = {k: v for k, v in split_ids(keep).items() if k in KEYS}
            sup = _claims_with(keep, claims, kinds=("sar_sentence", "evidence", "detector_hit"))
            sup = [x for x in sup if x != c["id"]]
        ep_from, ep_txns = None, []
        if not ev["txn_ids"] and episode is not None:
            ep_from, ep_txns = episode["id"], list(_ids(episode.get("evidence"))["txn_ids"])
        out.append({"n": len(out) + 1, "text": _norm(text), "claim": c["id"], "supported_by": sup, "ids": ev,
                    "episode_from": ep_from, "episode_txns": ep_txns, "rewritten": rewritten, "original": original})
    if not out:
        raise FilingError("no narrative sentence carries an attested id")
    return out, dropped


def footnote_ids(s: dict) -> list[str]:
    return list(dict.fromkeys(_flat(s["ids"]) + s["episode_txns"]))


def footnote_claims(s: dict) -> list[str]:
    return list(dict.fromkeys([s["claim"]] + s["supported_by"] + ([s["episode_from"]] if s["episode_from"] else [])))


# ---------------------------------------------------------------------------------------------------------------
# the filing model
def model(case_id: str, answer: dict, sidecar: dict, s3a: dict) -> dict:
    c, sar = answer["case"], answer["sar"]
    if (sidecar.get("case_id") or case_id) != case_id:
        raise FilingError(f"the attestation sidecar is for {sidecar.get('case_id')}, not {case_id}")
    row = _case_row(case_id)
    flagged = row.get("flagged_txn_id")
    rows = _timeline(answer, flagged)
    card = row.get("card_id") or rows[0]["card"]
    customer = row.get("customer_id") or rows[0]["customer"]
    conn = [str(x) for x in c.get("connected_card_ids") or []]
    info = _cards([card] + conn)
    missing_cards = [x for x in [card] + conn if x not in info]
    if missing_cards:
        raise FilingError(f"card(s) not in the index: {', '.join(missing_cards[:4])}")
    ring = _ring(answer, sidecar, flagged)
    affected = {r["id"] for r in rows}
    link = None
    if conn:
        link = _link_from_ring(ring, affected)
        if link is None:
            try:
                link = _link_from_claim(answer, sidecar, affected)
            except Exception as e:  # noqa: BLE001  (supplementary: the connected cards stay listed as attested)
                link = None
                ring = {**(ring or {}), "link_error": f"{type(e).__name__}: {str(e)[:120]}"}
    shared_label = f"{link['label']} {link['value']}" if link else ""
    ctx = {"rows": rows, "sidecar": sidecar, "customer": customer, "card": card, "card_desc": info[card]["desc"],
           "shared_label": shared_label}
    sents, dropped = sentences(answer, sidecar, _template(answer, ctx))

    # every id a footnote cites must exist in the index (the same lookup the SAR sentence claims re-run)
    cited = split_ids([i for s in sents for i in footnote_ids(s)])
    cited = {k: v for k, v in cited.items() if k in KEYS}
    found = _lookup(cited)
    absent = [i for i in _flat(cited) if i not in found]
    if absent:
        raise FilingError(f"cited id(s) not in the index: {', '.join(absent[:4])}")

    claims = {cl["id"]: cl for cl in sidecar.get("claims") or [] if cl.get("id")}
    cites_txn: dict = {}
    for cid, cl in claims.items():
        for t in _ids(cl.get("evidence"))["txn_ids"]:
            cites_txn.setdefault(t, []).append(cid)
    for r in rows:
        r["claims"] = cites_txn.get(r["id"], [])

    connected = []
    for cc in conn:
        m = (link or {}).get("per_card", {}).get(cc) or {}
        named = [cid for cid, cl in claims.items() if cc in _ids(cl.get("evidence"))["card_ids"]]
        linked = bool(m.get("n"))
        connected.append({"card": cc, "customer": info[cc]["customer"], "desc": info[cc]["desc"],
                          "link": shared_label if linked else ("named in attested claims " + ", ".join(named) if named else "listed by the agent"),
                          "via": link["value"] if linked else None, "n_link": m.get("n") if linked else None, "amt_link": m.get("amt") if linked else None,
                          "first_ts": m.get("first_ts") if linked else None, "first_txn": m.get("first_txn") if linked else None,
                          "claims": named})

    total = round(sum(r["amt"] for r in rows), 2)
    dates = [rows[0]["ts"][:10], rows[-1]["ts"][:10]]
    final = (answer.get("next_best_actions") or {}).get("final") or []
    checks = [
        ("exposure_usd equals the sum of the affected transactions", abs(total - float(c.get("exposure_usd") or 0)) < 0.005,
         f"{_money(total)} from the index, {_money(c.get('exposure_usd') or 0)} in the answer"),
        ("sar.total_amount_usd equals exposure_usd", abs(float(sar.get("total_amount_usd") or 0) - float(c.get("exposure_usd") or 0)) < 0.005,
         f"{_money(sar.get('total_amount_usd') or 0)}"),
        ("first_suspicious_txn_id is the earliest affected transaction", str(c.get("first_suspicious_txn_id")) == rows[0]["id"],
         f"{c.get('first_suspicious_txn_id')} in the answer, {rows[0]['id']} earliest in the index"),
        ("sar.activity_dates match the first and last affected transaction", list(sar.get("activity_dates") or []) == dates,
         f"{' to '.join(sar.get('activity_dates') or [])} in the answer, {dates[0]} to {dates[1]} in the index"),
        ("every narrative sentence carries a citation", all(footnote_ids(s) for s in sents), f"{len(sents)} of {len(sents)} cited"
         + (f", {len(dropped)} uncited sentence(s) dropped" if dropped else "")
         + (f", {sum(1 for s in sents if s['rewritten'])} rewritten from the template" if any(s["rewritten"] for s in sents) else "")),
    ]
    dv = sidecar.get("dataset_version") or {}
    devices = list(dict.fromkeys([str(x) for x in c.get("connected_device_profiles") or []] + [r["device"] for r in rows if r["device"]]))
    return {
        "case_id": case_id, "generated_at": _now(), "s3a": s3a, "sar": sar, "row": row,
        "customer": customer, "card": card, "card_desc": info[card]["desc"], "devices": devices,
        "category": CATEGORY.get(c.get("pattern"), "Card fraud"), "pattern": c.get("pattern"),
        "pattern_description": c.get("pattern_description") or "", "verdict": c.get("verdict"), "p": c.get("fraud_probability"),
        "exposure": float(c.get("exposure_usd") or 0.0), "dates": list(sar.get("activity_dates") or dates), "total": total,
        "sentences": sents, "dropped": dropped, "timeline": rows, "connected": connected, "ring": ring, "link": link,
        "shared_label": shared_label, "claims": claims, "checks": checks, "approval": _approval(case_id),
        "final": final, "dataset_version": dv.get("version") if isinstance(dv, dict) else str(dv),
        "attested_at": sidecar.get("run_at", ""), "backend": sidecar.get("backend", ""),
        "similar": [str(x) for x in c.get("similar_prior_cases") or []],
    }


# ---------------------------------------------------------------------------------------------------------------
# evidence graph (inline SVG, built from the timeline and the ring query; never a stored picture)
def graph(f: dict) -> dict:
    """Nodes and edges of the evidence graph: the case card, its affected transactions, the element(s) they share
    (or, with no ring, the device profile and billing region they carry), and the connected cards."""
    rows, link = f["timeline"], f["link"]
    elements = []
    if link:
        elements.append({"label": link["label"], "value": link["value"], "field": link["field"], "link": True,
                         "edge": f"{len(link['case_txns'])} of {len(rows)} affected transaction(s)"})
    for d in [x for x in f["devices"] if not link or x != link["value"]][:2]:
        n = sum(1 for r in rows if r["device"] == d)
        elements.append({"label": "device profile", "value": d, "field": "device_key", "link": False,
                         "edge": f"{n} of {len(rows)} affected transaction(s)" if n else "connected device profile"})
    if not f["connected"]:
        for reg in sorted({r["region"] for r in rows if r["region"]})[:2]:
            n = sum(1 for r in rows if r["region"] == reg)
            elements.append({"label": "billing region", "value": reg, "field": "addr1", "link": False,
                             "edge": f"{n} of {len(rows)} affected transaction(s)"})
    txns = [{"id": r["id"], "sub": f"{_money(r['amt'])}, {r['ts'][:10]}", "mark": bool(link and r["id"] in link["case_txns"]),
             "flagged": r["flagged"]} for r in rows]
    # connected cards hang off the linking element only when the data shows the link; otherwise off the case card,
    # labelled with the claims that name them (never off an element they do not share)
    cards = [{"id": x["card"], "sub": (f"{x['n_link']} txn(s)" + (f", {_money(x['amt_link'])}" if x["amt_link"] is not None else "")
                                       if x["n_link"] else x["link"][:44]),
              "element": 0 if (link and x["n_link"]) else None} for x in f["connected"]]
    return {"center": {"id": f["card"], "sub": f"customer {f['customer']}"}, "txns": txns[:TXNS_IN_GRAPH],
            "more_txns": max(0, len(txns) - TXNS_IN_GRAPH), "elements": elements, "cards": cards[:CARDS_IN_GRAPH],
            "more_cards": max(0, len(cards) - CARDS_IN_GRAPH)}


def _arc(n: int, centre_deg: float, step: float, max_span: float) -> list[float]:
    if n <= 0:
        return []
    span = min(max_span, step * (n - 1))
    return [math.radians(centre_deg + (0 if n == 1 else -span / 2 + span * i / (n - 1))) for i in range(n)]


def svg(g: dict, title: str) -> str:
    e = html.escape
    W, R1, R2, BW = 1000, 200, 390, 180          # txn arc radius, card arc radius, element box width
    t_ang = _arc(len(g["txns"]), 180, 13, 150)
    c_ang = _arc(len(g["cards"]), 0, 10, 130)
    half = max([R1 * abs(math.sin(a)) for a in t_ang] + [R2 * abs(math.sin(a)) for a in c_ang] + [36 * len(g["elements"])])
    H = int(max(360, 2 * half + 120))
    cx, cy = 420, H // 2 - 14
    ex = cx + 150                                  # left edge of the element boxes; their right edge stays inside the card arc
    ey = [cy + (i - (len(g["elements"]) - 1) / 2) * 68 for i in range(len(g["elements"]))]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-labelledby="g-title g-desc" class="graph">',
           f'<title id="g-title">{e(title)}</title>',
           f'<desc id="g-desc">Case card {e(g["center"]["id"])} at the centre; {len(g["txns"])} affected transaction(s) on the left; '
           f'{len(g["elements"])} linking element(s); {len(g["cards"])} connected card(s) on the right.</desc>']
    # edges first so nodes sit on top
    for a in t_ang:
        x, y = cx + R1 * math.cos(a), cy + R1 * math.sin(a)
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" class="e-txn"/>')
    for i, el in enumerate(g["elements"]):
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{ex}" y2="{ey[i]:.1f}" class="{"e-el" if el.get("link") else "e-attr"}"/>')
    for a, cd in zip(c_ang, g["cards"]):
        x, y = cx + R2 * math.cos(a), cy + R2 * math.sin(a)
        if cd["element"] is not None:
            out.append(f'<line x1="{ex + BW}" y1="{ey[cd["element"]]:.1f}" x2="{x:.1f}" y2="{y:.1f}" class="e-card"/>')
        else:
            out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" class="e-card dashed"/>')
    # transactions
    for a, t in zip(t_ang, g["txns"]):
        x, y = cx + R1 * math.cos(a), cy + R1 * math.sin(a)
        cls = "n-txn mark" if t["mark"] else "n-txn"
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{7 if t["flagged"] else 5.5}" class="{cls}"/>')
        out.append(f'<text x="{x - 12:.1f}" y="{y - 1:.1f}" text-anchor="end" class="t-id">{e(t["id"])}{" (flagged)" if t["flagged"] else ""}</text>')
        out.append(f'<text x="{x - 12:.1f}" y="{y + 11:.1f}" text-anchor="end" class="t-sub">{e(t["sub"])}</text>')
    if g["more_txns"]:
        out.append(f'<text x="{cx - R1}" y="{cy + R1 * 0.75 + 30:.1f}" text-anchor="middle" class="t-sub">'
                   f'+{g["more_txns"]} more affected transaction(s), see the timeline</text>')
    # elements
    for i, el in enumerate(g["elements"]):
        val = el["value"] if len(el["value"]) <= 26 else el["value"][:24] + "..."
        label = el["label"].split(" (")[0] + (", linking element" if el.get("link") else "")
        out.append(f'<rect x="{ex}" y="{ey[i] - 27:.1f}" width="{BW}" height="54" rx="6" class="{"n-el" if el.get("link") else "n-attr"}"/>')
        out.append(f'<text x="{ex + BW / 2}" y="{ey[i] - 12:.1f}" text-anchor="middle" class="t-sub">{e(label)}</text>')
        out.append(f'<text x="{ex + BW / 2}" y="{ey[i] + 3:.1f}" text-anchor="middle" class="t-id">{e(val)}</text>')
        out.append(f'<text x="{ex + BW / 2}" y="{ey[i] + 18:.1f}" text-anchor="middle" class="t-edge">{e(el["edge"])}</text>')
    # connected cards
    for a, cd in zip(c_ang, g["cards"]):
        x, y = cx + R2 * math.cos(a), cy + R2 * math.sin(a)
        out.append(f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" rx="2" class="n-card"/>')
        out.append(f'<text x="{x + 12:.1f}" y="{y - 1:.1f}" class="t-id">{e(cd["id"])}</text>')
        out.append(f'<text x="{x + 12:.1f}" y="{y + 11:.1f}" class="t-sub">{e(cd["sub"])}</text>')
    if g["more_cards"]:
        out.append(f'<text x="{cx + R2}" y="{H - 14}" class="t-sub">+{g["more_cards"]} more connected card(s), see the table</text>')
    # the case card on top
    out.append(f'<rect x="{cx - 70}" y="{cy - 26}" width="140" height="52" rx="8" class="n-case"/>')
    out.append(f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" class="t-case">{e(g["center"]["id"])}</text>')
    out.append(f'<text x="{cx}" y="{cy + 13}" text-anchor="middle" class="t-case-sub">{e(g["center"]["sub"])}</text>')
    out.append(f'<g class="legend" transform="translate(16,{H - 40})">'
               '<circle cx="6" cy="0" r="5.5" class="n-txn mark"/><text x="16" y="4" class="t-sub">affected transaction carrying the linking element</text>'
               '<circle cx="6" cy="18" r="5.5" class="n-txn"/><text x="16" y="22" class="t-sub">affected transaction</text>'
               '<rect x="330" y="-6" width="12" height="12" rx="2" class="n-card"/><text x="348" y="4" class="t-sub">connected card</text>'
               '<line x1="330" y1="18" x2="352" y2="18" class="e-card"/><text x="358" y="22" class="t-sub">shares the linking element in the data</text>'
               '<line x1="610" y1="18" x2="632" y2="18" class="e-card dashed"/><text x="638" y="22" class="t-sub">named by attested claims</text></g>')
    out.append("</svg>")
    return "\n".join(out)


def graph_edges_md(g: dict) -> list[str]:
    c = g["center"]
    lines = [f"- `{c['id']}` ({c['sub']}) has the affected transaction(s) " + ", ".join(f"`{t['id']}`" for t in g["txns"])
             + (f" and {g['more_txns']} more" if g["more_txns"] else "")]
    for el in g["elements"]:
        lines.append(f"- `{c['id']}` links to {el['label']} `{el['value']}` through {el['edge']}")
    for cd in g["cards"]:
        via = g["elements"][cd["element"]] if cd["element"] is not None else None
        lines.append("- " + (f"{via['label']} `{via['value']}` links to card `{cd['id']}` ({cd['sub']})" if via else
                             f"`{c['id']}` is connected to card `{cd['id']}` ({cd['sub']})"))
    if g["more_cards"]:
        lines.append(f"- and {g['more_cards']} more connected card(s), listed in section 6")
    return lines


# ---------------------------------------------------------------------------------------------------------------
# rendering
def _fn_text_md(s: dict) -> str:
    parts = []
    if s["rewritten"]:
        parts.append(f"Rewritten from the template: the recorded sentence **{s['claim']}** cited no ids "
                     f"(it read: \"{_norm(s['original'])}\").")
        if s["supported_by"]:
            parts.append("Ids attested in " + ", ".join(f"**{x}**" for x in s["supported_by"]) + ".")
    else:
        parts.append(f"Claim **{s['claim']}**" + (", supported by " + ", ".join(f"**{x}**" for x in s["supported_by"])
                                                  if s["supported_by"] else "") + ".")
    for k, label in ID_KINDS:
        if s["ids"].get(k):
            parts.append(f"{label}: " + ", ".join(f"`{x}`" for x in s["ids"][k]) + ".")
    if s["episode_txns"]:
        parts.append(f"Episode transactions (attested in **{s['episode_from']}**): " + ", ".join(f"`{x}`" for x in s["episode_txns"]) + ".")
    return " ".join(parts)


def _how(cl: dict) -> str:
    how = cl.get("how") or {}
    if not how.get("tool"):
        return "no query recorded"
    return f"{how['tool']}({json.dumps(how.get('params') or {}, sort_keys=True, default=str)})"


def _cited_claims(f: dict) -> list[dict]:
    ids = list(dict.fromkeys(x for s in f["sentences"] for x in footnote_claims(s)))
    return [f["claims"][x] for x in sorted(ids, key=lambda i: ("SED".index(i[0]) if i[0] in "SED" else 3, _claim_num(i)))
            if x in f["claims"]]


def _mdcell(v) -> str:
    return "" if v is None else str(v).replace("|", "\\|")


def _link_note(f: dict) -> str:
    link, ring = f["link"], f["ring"] or {}
    if link:
        return f"Linked by {link['label']} {link['value']}, from {link['source']}."
    why = ring.get("link_error") or (ring.get("reason") if ring and not ring.get("available") else "")
    return ("No single shared value links these cards in the data" + (f" ({why})" if why else "")
            + "; each is listed because the attested claims named in the table name it.")


def render_md(f: dict) -> str:
    s3a, sar = f["s3a"], f["sar"]
    L = [f"# Suspicious activity report, draft for analyst review: {f['case_id']}", "",
         f"> **{NO_TIPOFF}**", "",
         f"Prepared by the Kavach agent at {f['generated_at']} from the answer for case {f['case_id']} and its attestation "
         f"(run {f['attested_at']}, backend {f['backend']}, dataset version {f['dataset_version']}). "
         f"Approval: {f['approval']['text']}.", "",
         "## 1. Filing institution", "",
         "| Field | Value |", "|---|---|",
         "| Name | [Filing institution name] |", "| Identifier | [Institution identifier] |",
         "| Address | [Branch or office address] |", "| Contact | [Analyst name, phone, email] |",
         f"| Internal case | {f['case_id']} (case created, CREATE_CASE in the final actions) |", "",
         "## 2. Customer and cards concerned", "",
         "| Field | Value |", "|---|---|",
         f"| Customer | `{f['customer']}` (cardholder; the transactions were made on the card by an unauthorized party) |",
         f"| Card | `{f['card']}` ({_mdcell(f['card_desc']) or 'card'}) |",
         f"| Connected cards | {len(f['connected'])}" + (f": see section 6 | " if f["connected"] else " |"),
         f"| Device profiles | " + ("; ".join(f"`{_mdcell(d)}`" for d in f["devices"]) or "none recorded") + " |",
         f"| Subject of the activity | Unidentified party" + (f"; the activity links through {f['shared_label']}" if f["shared_label"] else "") + " |", "",
         "## 3. Suspicious activity", "",
         "| Field | Value |", "|---|---|",
         f"| Activity dates | {f['dates'][0]} to {f['dates'][-1]} |",
         f"| Total amount | {_money(sar.get('total_amount_usd') or f['exposure'])} over {len(f['timeline'])} transaction(s) |",
         f"| Category | {f['category']} |",
         f"| Policy basis | {_mdcell(sar.get('reason'))} |",
         f"| Assessment | verdict {f['verdict']}, fraud probability {float(f['p'] or 0):.2f} |", "",
         "Section 3a test (a report exists only when it holds):", "",
         f"- Fraud confirmed or strongly suspected: {'yes' if s3a['suspected'] else 'no'} (verdict {s3a['verdict']}, p {s3a['p']:.2f})",
         f"- Exposure over $1,000: {'yes' if s3a['conditions']['exposure_over_1000'] else 'no'} ({_money(s3a['exposure'])})",
         f"- Connected to a shared element or another card's fraud: {'yes' if s3a['conditions']['connected'] else 'no'} "
         f"({s3a['n_connected']} card(s), {s3a['n_devices']} device profile(s))",
         f"- Coordinated or undocumented pattern (R9): {'yes' if s3a['conditions']['undocumented_pattern'] else 'no'} ({s3a['pattern']})",
         f"- Result: {s3a['reason']}", ""]
    if f["pattern_description"]:
        L += [f"Pattern, in the agent's words: {f['pattern_description']}", ""]
    L += ["## 4. Narrative", "",
          " ".join(f"{s['text']}[^{s['n']}]" for s in f["sentences"]), ""]
    if f["dropped"]:
        L += [f"Dropped as uncited: " + "; ".join(f"{d['claim']} ({d['reason']})" for d in f["dropped"]), ""]
    L += ["## 5. Transaction timeline", "",
          "| Transaction | Time | Amount | Product | Channel | Region | Device profile | Cited in |", "|---|---|---:|---|---|---|---|---|"]
    for r in f["timeline"]:
        tag = " (flagged)" if r["flagged"] else ""
        tag += " (first)" if r["first"] else ""
        L.append(f"| `{r['id']}`{tag} | {r['ts']} | {_money(r['amt'])} | {r['product']} | {r['channel']} | {r['region'] or ''} | "
                 f"{_mdcell(r['device']) or 'no identity record'} | {', '.join(r['claims'])} |")
    L += [f"| Total | | {_money(f['total'])} | | | | | |", ""]
    L += ["## 6. Connected cards", ""]
    if f["connected"]:
        L += ["| Card | Customer | Linked by | Transactions via the link | Amount via the link | First linked | Named in claims |",
              "|---|---|---|---:|---:|---|---|"]
        for x in f["connected"]:
            L.append(f"| `{x['card']}` | `{x['customer']}` | {('`' + _mdcell(x['via']) + '`') if x['via'] else _mdcell(x['link'])} | {x['n_link'] if x['n_link'] is not None else ''} | "
                     f"{_money(x['amt_link']) if x['amt_link'] is not None else ''} | "
                     f"{(x['first_ts'] or '') + (' (`' + x['first_txn'] + '`)' if x['first_txn'] else '')} | {', '.join(x['claims'])} |")
        L += ["", _link_note(f)]
    else:
        L += ["No other card is listed as connected; the report rests on " + s3a["reason"].split(": ", 2)[-1] + "."]
    g = graph(f)
    L += ["", "## 7. Evidence graph", "",
          f"Drawn as an inline SVG in `{f['case_id']}.html`, built from the timeline and the ring query above. Edges:", ""]
    L += graph_edges_md(g)
    L += ["", "## 8. Attested claims cited", "",
          "Re-run every claim with `python -m kavach attest " + f["case_id"] + "` and every footnote with "
          "`python -m kavach.filing verify " + f["case_id"] + "`.", ""]
    for cl in _cited_claims(f):
        ids = _ids(cl.get("evidence"))
        L.append(f"- **{cl['id']}** ({cl.get('kind')}): {_norm(cl.get('what'))} How: `{_how(cl)}`. When: "
                 f"{(cl.get('when') or {}).get('ts', '')}, dataset {(cl.get('when') or {}).get('dataset_version', '')}. "
                 f"Ids: {len(_flat(ids))}.")
    L += ["", "## 9. Checks before filing", ""]
    for name, ok, detail in f["checks"]:
        L.append(f"- [{'x' if ok else ' '}] {name}: {detail}")
    L.append(f"- [{'x' if f['approval']['status'] == 'approved' else ' '}] L2 approval of FILE_REPORT: {f['approval']['text']}")
    L += ["", "Analyst edits before sending: fill in section 1, confirm the subject description, attach the case file "
              f"{f['case_id']}, and keep the no tip-off instruction.", "", "## Citations", ""]
    for s in f["sentences"]:
        L.append(f"[^{s['n']}]: {_fn_text_md(s)}")
    return _undash("\n".join(L) + "\n")


CSS = """
:root{--ink:#16181d;--muted:#5b616e;--line:#d5d8de;--soft:#f4f5f7;--accent:#b4531a;--accent-soft:#fbeee4;--ok:#1f6f43;--bad:#a1261b}
*{box-sizing:border-box}
html{background:#e9eaee}
body{margin:0 auto;max-width:900px;padding:40px 48px 56px;background:#fff;color:var(--ink);
 font:15px/1.6 Charter,"Bitstream Charter","Sitka Text",Cambria,Georgia,serif}
h1,h2,h3,.label,th,.meta,.banner,figcaption,.checks{font-family:"Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
h1{font-size:22px;line-height:1.3;margin:0 0 6px}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);
 padding-bottom:4px;margin:30px 0 12px}
.meta{font-size:12.5px;color:var(--muted);margin:0 0 14px}
.banner{border:1.5px solid var(--bad);color:var(--bad);background:#fff;padding:10px 14px;font-size:13px;font-weight:600;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
th,td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{background:var(--soft);font-weight:600;font-size:12px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
table.kv th{width:200px}
td.nw,td.nw code{white-space:nowrap}
code,.id{font-family:Consolas,"SFMono-Regular",Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
tr.total td{font-weight:600;background:var(--soft)}
.tag{display:inline-block;font:600 10.5px/1.5 "Segoe UI",system-ui,sans-serif;padding:0 6px;border-radius:3px;
 background:var(--accent-soft);color:var(--accent);margin-left:4px}
.narrative{font-size:15.5px;line-height:1.75}
.narrative sup a{color:var(--accent);text-decoration:none;font:600 10.5px "Segoe UI",system-ui,sans-serif;padding:0 1px}
ol.fn{font-size:12.5px;padding-left:22px}
ol.fn li{margin:0 0 6px}
ol.fn b{color:var(--accent)}
.s3a li{margin:2px 0}
.yes{color:var(--ok);font-weight:600}.no{color:var(--muted)}
figure{margin:8px 0 0}
figcaption{font-size:12px;color:var(--muted)}
svg.graph{width:100%;height:auto;border:1px solid var(--line);background:#fff}
svg .e-txn{stroke:#9aa0ab;stroke-width:1}
svg .e-el{stroke:var(--accent);stroke-width:2.5}
svg .e-attr{stroke:#9aa0ab;stroke-width:1.5}
svg .n-attr{fill:var(--soft);stroke:#9aa0ab;stroke-width:1}
svg .e-card{stroke:var(--accent);stroke-width:1;opacity:.7}
svg .e-card.dashed{stroke:#9aa0ab;stroke-dasharray:4 3;opacity:1}
svg .n-txn{fill:#fff;stroke:#3c4250;stroke-width:1.5}
svg .n-txn.mark{fill:var(--accent);stroke:var(--accent)}
svg .n-el{fill:var(--accent-soft);stroke:var(--accent);stroke-width:1.5}
svg .n-card{fill:#fff;stroke:var(--accent);stroke-width:1.5}
svg .n-case{fill:var(--ink);stroke:var(--ink)}
svg text{font-family:"Segoe UI",system-ui,sans-serif}
svg .t-id{font-size:11.5px;fill:var(--ink);font-family:Consolas,Menlo,monospace}
svg .t-sub,svg .t-edge{font-size:10.5px;fill:var(--muted)}
svg .t-edge{fill:var(--accent);font-weight:600}
svg .t-case{font-size:13px;fill:#fff;font-weight:700;font-family:Consolas,Menlo,monospace}
svg .t-case-sub{font-size:10.5px;fill:#d8dbe2}
ul.claims{font-size:12.5px;padding-left:18px}
ul.claims li{margin:0 0 6px}
.checks li{font-size:13px;list-style:none;margin:2px 0}
.checks{padding-left:0}
.edit{border-left:3px solid var(--accent);padding:6px 12px;background:var(--accent-soft);font-size:13.5px}
@media (max-width:640px){body{padding:20px 16px}table.kv th{width:auto}}
@page{size:A4;margin:16mm 14mm}
@media print{html{background:#fff}body{max-width:none;padding:0;font-size:11pt}.banner{break-inside:avoid}
 tr,li,figure{break-inside:avoid}h2{break-after:avoid}a{color:inherit}}
"""


def _fn_html(s: dict) -> str:
    e = html.escape

    def codes(xs):
        return ", ".join(f"<code>{e(x)}</code>" for x in xs)

    parts = []
    if s["rewritten"]:
        parts.append(f"Rewritten from the template: the recorded sentence <b>{e(s['claim'])}</b> cited no ids "
                     f"(it read: &ldquo;{e(_norm(s['original']))}&rdquo;).")
        if s["supported_by"]:
            parts.append("Ids attested in " + ", ".join(f"<b>{e(x)}</b>" for x in s["supported_by"]) + ".")
    else:
        parts.append(f"Claim <b>{e(s['claim'])}</b>" + (", supported by " + ", ".join(f"<b>{e(x)}</b>" for x in s["supported_by"])
                                                       if s["supported_by"] else "") + ".")
    for k, label in ID_KINDS:
        if s["ids"].get(k):
            parts.append(f"{label}: {codes(s['ids'][k])}.")
    if s["episode_txns"]:
        parts.append(f"Episode transactions (attested in <b>{e(s['episode_from'])}</b>): {codes(s['episode_txns'])}.")
    data = (f' data-claims="{e(" ".join(footnote_claims(s)))}" '
            f'data-ids="{e(json.dumps(footnote_ids(s), ensure_ascii=True))}"')
    return f'<li id="fn-{s["n"]}"{data}>{" ".join(parts)} <a href="#ref-{s["n"]}" aria-label="back to sentence {s["n"]}">&#8617;</a></li>'


def render_html(f: dict) -> str:
    e = html.escape
    s3a, sar = f["s3a"], f["sar"]

    def yn(b):
        return '<span class="yes">yes</span>' if b else '<span class="no">no</span>'

    rows_t = []
    for r in f["timeline"]:
        tags = ('<span class="tag">flagged</span>' if r["flagged"] else "") + ('<span class="tag">first</span>' if r["first"] else "")
        dev = f'<td><code>{e(r["device"])}</code></td>' if r["device"] else '<td class="no">no identity record</td>'
        rows_t.append(f'<tr><td class="nw"><code>{e(r["id"])}</code>{tags}</td><td class="nw">{e(r["ts"])}</td><td class="num nw">{_money(r["amt"])}</td>'
                      f'<td>{e(r["product"])}</td><td>{e(r["channel"])}</td><td>{e(r["region"] or "")}</td>{dev}'
                      f'<td>{e(", ".join(r["claims"]))}</td></tr>')
    conn_html = ""
    if f["connected"]:
        trs = "".join(
            f'<tr><td class="nw"><code>{e(x["card"])}</code></td><td class="nw"><code>{e(x["customer"])}</code></td>'
            f'<td>{("<code>" + e(x["via"]) + "</code>") if x["via"] else e(x["link"])}</td>'
            f'<td class="num">{"" if x["n_link"] is None else x["n_link"]}</td>'
            f'<td class="num">{"" if x["amt_link"] is None else _money(x["amt_link"])}</td>'
            f'<td>{e(x["first_ts"] or "")}{(" <code>" + e(x["first_txn"]) + "</code>") if x["first_txn"] else ""}</td>'
            f'<td>{e(", ".join(x["claims"]))}</td></tr>' for x in f["connected"])
        note = f'<p class="meta">{e(_link_note(f))}</p>'
        conn_html = ('<table><thead><tr><th>Card</th><th>Customer</th><th>Linked by</th><th class="num">Transactions via the link</th>'
                     '<th class="num">Amount via the link</th><th>First linked</th><th>Named in claims</th></tr></thead>'
                     f'<tbody>{trs}</tbody></table>{note}')
    else:
        conn_html = f'<p>No other card is listed as connected; the report rests on {e(s3a["reason"].split(": ", 2)[-1])}.</p>'
    g = graph(f)
    narrative = " ".join(f'<span id="ref-{s["n"]}">{e(s["text"])}</span><sup><a href="#fn-{s["n"]}">{s["n"]}</a></sup>'
                         for s in f["sentences"])
    dropped = (f'<p class="meta">Dropped as uncited: {e("; ".join(d["claim"] + " (" + d["reason"] + ")" for d in f["dropped"]))}</p>'
               if f["dropped"] else "")
    claims = "".join(
        f'<li><b>{e(cl["id"])}</b> ({e(str(cl.get("kind")))}): {e(_norm(cl.get("what")))} '
        f'<br><span class="meta">How: <code>{e(_how(cl))}</code>. When: {e(str((cl.get("when") or {}).get("ts", "")))}, '
        f'dataset {e(str((cl.get("when") or {}).get("dataset_version", "")))}. Ids: {len(_flat(_ids(cl.get("evidence"))))}.</span></li>'
        for cl in _cited_claims(f))
    checks = "".join(f'<li>{"&#9745;" if ok else "&#9744;"} {e(name)}: {e(detail)}</li>' for name, ok, detail in f["checks"])
    checks += (f'<li>{"&#9745;" if f["approval"]["status"] == "approved" else "&#9744;"} L2 approval of FILE_REPORT: '
               f'{e(f["approval"]["text"])}</li>')
    pd_html = (f'<p>Pattern, in the agent\'s words: {e(f["pattern_description"])}</p>' if f["pattern_description"] else "")
    body = f"""<header>
<h1>Suspicious activity report, draft for analyst review: {e(f["case_id"])}</h1>
<p class="meta">Prepared by the Kavach agent at {e(f["generated_at"])} from the answer for case {e(f["case_id"])} and its attestation
(run {e(f["attested_at"])}, backend {e(f["backend"])}, dataset version {e(str(f["dataset_version"]))}).</p>
<div class="banner" role="note">{e(NO_TIPOFF)}</div>
<p class="meta">Approval: {e(f["approval"]["text"])}.</p>
</header>
<section><h2>1. Filing institution</h2>
<table class="kv"><tbody>
<tr><th>Name</th><td>[Filing institution name]</td></tr><tr><th>Identifier</th><td>[Institution identifier]</td></tr>
<tr><th>Address</th><td>[Branch or office address]</td></tr><tr><th>Contact</th><td>[Analyst name, phone, email]</td></tr>
<tr><th>Internal case</th><td>{e(f["case_id"])} (case created, CREATE_CASE in the final actions)</td></tr>
</tbody></table></section>
<section><h2>2. Customer and cards concerned</h2>
<table class="kv"><tbody>
<tr><th>Customer</th><td><code>{e(f["customer"])}</code> (cardholder; the transactions were made on the card by an unauthorized party)</td></tr>
<tr><th>Card</th><td><code>{e(f["card"])}</code> ({e(f["card_desc"] or "card")})</td></tr>
<tr><th>Connected cards</th><td>{len(f["connected"])}{": see section 6" if f["connected"] else ""}</td></tr>
<tr><th>Device profiles</th><td>{"; ".join("<code>" + e(d) + "</code>" for d in f["devices"]) or "none recorded"}</td></tr>
<tr><th>Subject of the activity</th><td>Unidentified party{("; the activity links through " + e(f["shared_label"])) if f["shared_label"] else ""}</td></tr>
</tbody></table></section>
<section><h2>3. Suspicious activity</h2>
<table class="kv"><tbody>
<tr><th>Activity dates</th><td>{e(f["dates"][0])} to {e(f["dates"][-1])}</td></tr>
<tr><th>Total amount</th><td>{_money(sar.get("total_amount_usd") or f["exposure"])} over {len(f["timeline"])} transaction(s)</td></tr>
<tr><th>Category</th><td>{e(f["category"])}</td></tr>
<tr><th>Policy basis</th><td>{e(str(sar.get("reason") or ""))}</td></tr>
<tr><th>Assessment</th><td>verdict {e(str(f["verdict"]))}, fraud probability {float(f["p"] or 0):.2f}</td></tr>
</tbody></table>
<p>Section 3a test (a report exists only when it holds):</p>
<ul class="s3a">
<li>Fraud confirmed or strongly suspected: {yn(s3a["suspected"])} (verdict {e(s3a["verdict"])}, p {s3a["p"]:.2f})</li>
<li>Exposure over $1,000: {yn(s3a["conditions"]["exposure_over_1000"])} ({_money(s3a["exposure"])})</li>
<li>Connected to a shared element or another card's fraud: {yn(s3a["conditions"]["connected"])} ({s3a["n_connected"]} card(s), {s3a["n_devices"]} device profile(s))</li>
<li>Coordinated or undocumented pattern (R9): {yn(s3a["conditions"]["undocumented_pattern"])} ({e(s3a["pattern"])})</li>
<li>Result: {e(s3a["reason"])}</li>
</ul>{pd_html}</section>
<section><h2>4. Narrative</h2>
<p class="narrative">{narrative}</p>{dropped}</section>
<section><h2>5. Transaction timeline</h2>
<table><thead><tr><th>Transaction</th><th>Time</th><th class="num">Amount</th><th>Product</th><th>Channel</th><th>Region</th><th>Device profile</th><th>Cited in</th></tr></thead>
<tbody>{"".join(rows_t)}<tr class="total"><td>Total</td><td></td><td class="num">{_money(f["total"])}</td><td colspan="5"></td></tr></tbody></table></section>
<section><h2>6. Connected cards</h2>
{conn_html}</section>
<section><h2>7. Evidence graph</h2>
<figure>{svg(g, "Evidence graph for " + f["case_id"])}
<figcaption>Built from the affected transactions and the ring query in the index at {e(f["generated_at"])}; not a stored picture.</figcaption></figure></section>
<section><h2>8. Citations</h2>
<ol class="fn">{"".join(_fn_html(s) for s in f["sentences"])}</ol></section>
<section><h2>9. Attested claims cited</h2>
<p class="meta">Re-run every claim with <code>python -m kavach attest {e(f["case_id"])}</code> and every footnote with
<code>python -m kavach.filing verify {e(f["case_id"])}</code>.</p>
<ul class="claims">{claims}</ul></section>
<section><h2>10. Checks before filing</h2>
<ul class="checks">{checks}</ul>
<p class="edit">Analyst edits before sending: fill in section 1, confirm the subject description, attach the case file {e(f["case_id"])},
and keep the no tip-off instruction.</p></section>"""
    doc = (f'<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
           f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
           f'<title>SAR draft {e(f["case_id"])}</title>\n<style>{CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n')
    return _undash(doc)


# ---------------------------------------------------------------------------------------------------------------
# build
def build(case_id: str, cases_dir=None, attest_dir_=None, out_dir=None) -> dict:
    """Decide and, when section 3a and the answer agree, write sar/<case>.md and sar/<case>.html.
    Returns one manifest row: {case_id, status, reason, ...}."""
    cases_dir = Path(cases_dir or CASES_DIR)
    out = Path(out_dir or OUT_DIR)
    row = {"case_id": case_id, "status": ERROR, "reason": "", "at": _now()}
    p = cases_dir / f"{case_id}.json"
    if not p.exists():
        row["reason"] = f"no answer file {_rel(p)}"
        return row
    try:
        answer = _read_json(p)
    except (OSError, json.JSONDecodeError) as e:
        row["reason"] = f"answer file unreadable: {type(e).__name__}"
        return row
    status, reason, s3a = decide(answer)
    row.update({"status": status, "reason": reason, "sar_file": (answer.get("sar") or {}).get("file"), "s3a_required": s3a["required"]})
    if status != FILED:
        _remove(out, case_id)          # a stale filing for a case that no longer files must not survive
        return row
    sp = _attest_dir(attest_dir_) / f"{case_id}.json"
    try:
        if not sp.exists():
            raise FilingError(f"no attestation sidecar {_rel(sp)}; a sentence nobody can check counts as wrong")
        sidecar = _read_json(sp)
        f = model(case_id, answer, sidecar, s3a)
        md, page = render_md(f), render_html(f)
    except FilingError as e:
        _remove(out, case_id)
        row.update({"status": ERROR, "reason": f"fail closed: {e}"})
        return row
    except Exception as e:  # noqa: BLE001  (any other failure: no filing, a clear reason)
        _remove(out, case_id)
        row.update({"status": ERROR, "reason": f"fail closed: {type(e).__name__}: {str(e)[:200]}"})
        return row
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{case_id}.md").write_text(md, encoding="utf-8")
    (out / f"{case_id}.html").write_text(page, encoding="utf-8")
    row.update({"md": _rel(out / f"{case_id}.md"), "html": _rel(out / f"{case_id}.html"), "sentences": len(f["sentences"]),
                "rewritten": sum(1 for s in f["sentences"] if s["rewritten"]), "dropped": len(f["dropped"]),
                "transactions": len(f["timeline"]), "connected_cards": len(f["connected"]),
                "checks_failed": [n for n, ok, _ in f["checks"] if not ok], "approval": f["approval"]["status"]})
    return row


def _remove(out: Path, case_id: str) -> None:
    for ext in ("md", "html"):
        q = out / f"{case_id}.{ext}"
        if q.exists():
            q.unlink()


def write_manifest(rows: list[dict], out_dir=None, replace: bool = False) -> Path:
    out = Path(out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    p = out / "manifest.json"
    old = {}
    if p.exists() and not replace:
        try:
            old = {r["case_id"]: r for r in _read_json(p).get("cases", [])}
        except (OSError, json.JSONDecodeError, KeyError, AttributeError):
            old = {}
    for r in rows:
        old[r["case_id"]] = r
    data = {"generated_at": _now(), "rule": "policy section 3a decides whether a filing exists; see kavach/filing.py",
            "counts": {s: sum(1 for r in old.values() if r["status"] == s) for s in (FILED, REFUSED, MISSING, NOT_REQUIRED, ERROR)},
            "cases": [old[k] for k in sorted(old)]}
    p.write_text(_undash(json.dumps(data, indent=2, ensure_ascii=False)), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------------------------------------------
# verify
FN_MD = re.compile(r"^\[\^(\d+)\]:\s*(.*)$", re.M)
MARK = re.compile(r"\[\^(\d+)\]")
CLAIM_MD = re.compile(r"\*\*([SED]\d+)\*\*")
TICK = re.compile(r"`([^`]+)`")
FN_HTML = re.compile(r'<li id="fn-(\d+)" data-claims="([^"]*)" data-ids="([^"]*)">')


def _section(md: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\s*$(.*?)(?=^## |\Z)", md, re.M | re.S)
    return m.group(1) if m else ""


def verify(case_id: str, out_dir=None, attest_dir_=None, cases_dir=None, rerun: bool = True) -> list[dict]:
    """Check one filing as written: every narrative sentence ends in a footnote; every footnote names claims that
    exist in the sidecar and ids that exist in the index and in the sidecar; the HTML carries the same footnotes;
    the timeline lists exactly the affected transactions; no en or em dash; optionally re-run every cited claim."""
    out = Path(out_dir or OUT_DIR)
    res: list[dict] = []

    def add(check, ok, detail=""):
        res.append({"case_id": case_id, "check": check, "status": ok if isinstance(ok, str) else "pass" if ok else "fail",
                    "detail": detail})

    mdp, hp = out / f"{case_id}.md", out / f"{case_id}.html"
    if not mdp.exists() or not hp.exists():
        add("files", False, f"missing {_rel(mdp) if not mdp.exists() else _rel(hp)}")
        return res
    md, page = mdp.read_text(encoding="utf-8"), hp.read_text(encoding="utf-8")
    sp = _attest_dir(attest_dir_) / f"{case_id}.json"
    if not sp.exists():
        add("sidecar", False, f"no attestation sidecar {_rel(sp)}")
        return res
    side = _read_json(sp)
    claims = {c["id"]: c for c in side.get("claims") or [] if c.get("id")}
    attested = {x for c in claims.values() for x in _flat(_ids(c.get("evidence")))}

    # 1. every narrative sentence carries a footnote marker, and nothing trails the last one
    narr = _section(md, "4. Narrative").strip().split("\n\n")[0].strip()
    marks = [int(m) for m in MARK.findall(narr)]
    pieces = [x.strip() for x in MARK.split(narr)[::2]]
    trailing = pieces[-1] if pieces else narr
    empty = [i + 1 for i, x in enumerate(pieces[:-1]) if not x]
    add("every sentence cited", bool(marks) and not trailing and not empty,
        f"{len(marks)} sentence(s) with a footnote" + (f"; uncited text after the last footnote: {trailing[:60]!r}" if trailing else "")
        + (f"; empty sentence before footnote(s) {empty}" if empty else ""))

    # 2. footnotes: claims in the sidecar, ids in the sidecar and in the index
    fns = {int(n): body for n, body in FN_MD.findall(md)}
    no_def = [m for m in marks if m not in fns]
    agree = marks == sorted(fns) == list(range(1, len(fns) + 1))
    add("markers and footnotes agree", agree, f"{len(marks)} marker(s), {len(fns)} footnote(s)"
        + (f"; no footnote for {no_def}" if no_def else "") + ("" if agree else "; markers must run 1..n, one per footnote"))
    all_ids, md_sets = [], {}
    for n in sorted(fns):
        body = fns[n]
        cl = CLAIM_MD.findall(body)
        ids = TICK.findall(body)
        md_sets[n] = (set(cl), set(ids))
        unknown = [c for c in cl if c not in claims]
        not_att = [i for i in ids if i not in attested]
        add(f"footnote {n}", bool(cl) and bool(ids) and not unknown and not not_att,
            f"{len(cl)} claim(s), {len(ids)} id(s)" + ("; no claim" if not cl else "") + ("; no id" if not ids else "")
            + (f"; claims not in the sidecar {unknown}" if unknown else "")
            + (f"; ids not attested in the sidecar {not_att[:4]}" if not_att else ""))
        all_ids += ids
    ev = {k: v for k, v in split_ids(list(dict.fromkeys(all_ids))).items() if k in KEYS}
    try:
        found = _lookup(ev)
        absent = [i for i in _flat(ev) if i not in found]
        add("footnote ids in the index", not absent, f"{len(_flat(ev))} distinct id(s) looked up"
            + (f"; not found: {', '.join(absent[:4])}" if absent else ""))
    except Exception as e:  # noqa: BLE001
        add("footnote ids in the index", False, f"lookup failed: {type(e).__name__}: {str(e)[:120]}")

    # 3. the HTML carries the same footnotes
    h = {int(n): (set(html.unescape(c).split()), set(json.loads(html.unescape(i)))) for n, c, i in FN_HTML.findall(page)}
    diff = [n for n in set(h) | set(md_sets) if h.get(n) != md_sets.get(n)]
    add("html footnotes match markdown", not diff and len(h) == len(md_sets), f"{len(h)} in html, {len(md_sets)} in markdown"
        + (f"; differ at {sorted(diff)[:5]}" if diff else ""))
    n_sup = len(re.findall(r'<sup><a href="#fn-\d+">', page))
    add("html sentences cited", n_sup == len(h) and n_sup > 0, f"{n_sup} footnote link(s) in the narrative")

    # 4. the timeline is exactly the affected transactions
    ap = Path(cases_dir or CASES_DIR) / f"{case_id}.json"
    if ap.exists():
        ans = _read_json(ap)
        tl = [m for m in re.findall(r"^\| `(\d+)`", _section(md, "5. Transaction timeline"), re.M)]
        want = [str(x) for x in ans["case"].get("affected_txn_ids") or []]
        add("timeline equals affected_txn_ids", sorted(tl) == sorted(want), f"{len(tl)} row(s), {len(want)} affected")
        add("answer still files", decide(ans)[0] == FILED, decide(ans)[1][:160])
    else:
        add("answer file", False, f"no answer file {_rel(ap)}")

    # 5. house rules and self-containment
    add("no en or em dash", not DASH_RE.search(md) and not DASH_RE.search(page), "")
    ext = re.findall(r'(?:src|href)="(?!#)[^"]+"|<link\b|<script\b|url\(', page)
    add("html self-contained", not ext, f"external references: {ext[:3]}" if ext else "inline CSS and SVG only")
    add("no tip-off line present", NO_TIPOFF[:60] in md and html.escape(NO_TIPOFF)[:60] in page, "")

    # 6. re-run every cited claim (the same check `python -m kavach attest` applies)
    if rerun:
        from kavach.attest import _version_id, dataset_version, verify_claim
        from kavach.tools import ToolRegistry
        tools, cur = ToolRegistry(side.get("backend") or "duck"), _version_id(dataset_version())
        cited = sorted({c for cl, _ in md_sets.values() for c in cl}, key=lambda i: ("SED".index(i[0]), _claim_num(i)))
        for cid in cited:
            if cid not in claims:
                continue
            r = verify_claim(claims[cid], tools, cur)
            if r["status"] == "fail" and r.get("missing") and all(" | " in m for m in r["missing"]):
                # only whitespace-trimmed device keys are "missing": the trim-tolerant index check above decides
                ok = not (set(r["missing"]) - _lookup({"device_keys": r["missing"]}))
                add(f"re-run {cid}", "warn" if ok else "fail",
                    f"{r['status']}: {r['reason']}" + ("; the key exists in the index with surrounding whitespace, which the "
                                                       "sidecar strips (kavach.attest fix pending)" if ok else ""))
            else:
                add(f"re-run {cid}", r["status"] != "fail", f"{r['status']}: {r['reason']}")
    return res


# ---------------------------------------------------------------------------------------------------------------
# CLI
def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _all_ids(cases_dir: Path) -> list[str]:
    return sorted(p.stem for p in Path(cases_dir).glob("*.json"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.filing",
                                 description="Draft SAR filings (Markdown and self-contained HTML) for answers that section 3a "
                                             "requires a report for; `verify` checks every footnote.")
    ap.add_argument("args", nargs="*", help="[verify] <case id> ...")
    ap.add_argument("--all", action="store_true", help="every answer in the cases folder")
    ap.add_argument("--cases-dir", default=None, help="answers folder (default cases/)")
    ap.add_argument("--attest-dir", default=None, help="attestation sidecars (default attestations/)")
    ap.add_argument("--out", default=None, help="output folder (default sar/)")
    ap.add_argument("--no-rerun", action="store_true", help="verify: skip re-running the cited claims")
    a = ap.parse_args(argv)
    _utf8()
    mode = "build"
    args = list(a.args)
    if args and args[0] == "verify":
        mode, args = "verify", args[1:]
    cases_dir = Path(a.cases_dir) if a.cases_dir else CASES_DIR
    out = Path(a.out) if a.out else OUT_DIR
    if a.all:
        # build: every answer; verify: every filing on disk
        ids = sorted(p.stem for p in out.glob("*.md")) if mode == "verify" else _all_ids(cases_dir)
    elif args:
        ids = args
    else:
        ap.error("give a case id or --all")
    if mode == "verify":
        if not ids:
            print(f"no filings in {_rel(out)}")
            return 1
        bad = 0
        for cid in ids:
            rows = verify(cid, out, a.attest_dir, cases_dir, rerun=not a.no_rerun)
            for r in rows:
                print(f"{cid} {r['status']:4s} {r['check']}: {r['detail']}")
            nf = sum(1 for r in rows if r["status"] == "fail")
            nw = sum(1 for r in rows if r["status"] == "warn")
            print(f"{cid}: {len(rows)} checks, {nf} failed, {nw} warning(s)")
            bad += nf
        print(f"verify: {len(ids)} filing(s), {bad} failed check(s)")
        return 1 if bad else 0
    rows = []
    for cid in ids:
        r = build(cid, cases_dir, a.attest_dir, out)
        rows.append(r)
        extra = f" -> {r['md']}, {r['html']} ({r['sentences']} sentences, {r['transactions']} txns, {r['connected_cards']} connected)" if r["status"] == FILED else ""
        print(f"{cid}: {r['status']}{extra}. {r['reason']}")
    p = write_manifest(rows, out, replace=a.all)
    c = {s: sum(1 for r in rows if r["status"] == s) for s in (FILED, REFUSED, MISSING, NOT_REQUIRED, ERROR)}
    print(f"filing: {len(rows)} answer(s): " + ", ".join(f"{k} {v}" for k, v in c.items()) + f"; manifest {_rel(p)}")
    return 1 if c[ERROR] or c[MISSING] else 0


if __name__ == "__main__":
    sys.exit(main())
