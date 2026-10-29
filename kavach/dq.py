"""Data-quality detective (brief challenge 11): the gaps that matter, with record ids, and the investigations they taint.

    python -m kavach.dq [--duck data/kavach_r2.duckdb] [--out docs/round2/data-quality.md]

`report()` scans the DuckDB index (read-only) for transactions without an identity record, empty billing regions,
placeholder device values, cards too thin to judge, duplicates, non-positive amounts and the risk_score distribution per
month, and runs `taint()` on every case of the latest pack. `taint(case_row)` returns the flags that affect one
investigation, each with record ids and a one-line consequence. `probability_cap(flags, fraud_groups)` is the TRD2 section 9
rule: no identity record plus thin history caps p at 0.60 unless a network or history group supports fraud.
"""
import argparse
import sys
import time
from pathlib import Path

import duckdb

from kavach.config import DUCK_PATH, ROOT

REPORT_MD = ROOT / "docs" / "round2" / "data-quality.md"
PLACEHOLDER_DEVICEINFO = ("Windows", "iOS Device", "MacOS", "Trident/7.0")
PLACEHOLDER_ID31 = ("other",)
THIN = 5                       # fewer than this many prior transactions is too thin to judge
JANUARY = "2017-01-01"         # the bank's model was not retrained for activity from here on
CAP = 0.60
CAP_FLAGS = {"no_identity_record", "thin_history"}
CAP_EXEMPT_GROUPS = {"network", "history"}
SAMPLE = 5

CONSEQUENCE = {
    "flagged_txn_missing": "The flagged transaction is not in the index; the case cannot be investigated and must fail closed.",
    "alert_card_mismatch": "The alert names a card the flagged transaction is not on; the investigation follows the transaction's own card, "
                           "and anything said about the stated card would describe another account.",
    "no_identity_record": "No device or browser record for the flagged transaction: device, new-device and proxy signals cannot fire either way, "
                          "and the absence of device evidence is not evidence of innocence.",
    "empty_billing_region": "No billing region on the flagged transaction: region checks (out-of-region use, region clusters, holder_key) cannot run.",
    "thin_history": "Fewer than 5 prior transactions on the card and on its holder key: amount, product, region and device novelty "
                    "have no baseline, so profile-fit signals are unreliable.",
    "placeholder_device": "The device profile is a generic value shared by many customers; it can neither link cards nor separate them.",
    "january_score": "January risk_score comes from a model that was not retrained: it is clamped, a low score is not evidence of innocence "
                     "and a high score alone cannot carry a verdict.",
}


def connect(duck_path=None) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(duck_path or DUCK_PATH), read_only=True)


def _has(con, table: str, col: str | None = None) -> bool:
    try:
        cols = [r[0] for r in con.execute(f"describe {table}").fetchall()]
    except duckdb.Error:
        return False
    return col is None or col in cols


def _valid_holder(k) -> bool:
    return isinstance(k, str) and "||" not in k and not k.endswith("|") and k.count("|") == 2


# ---------------------------------------------------------------------------------------------------------------------
# per investigation

def taint(case_row: dict, duck_path=None, con=None) -> list[dict]:
    """Data-quality flags that affect one investigation: [{flag, record_ids, detail, consequence, caps_probability}]."""
    own = con is None
    con = con or connect(duck_path)
    try:
        return _taint(con, case_row)
    finally:
        if own:
            con.close()


def _flag(flag: str, record_ids, detail: str) -> dict:
    return {"flag": flag, "record_ids": [str(x) for x in record_ids if x is not None], "detail": detail,
            "consequence": CONSEQUENCE[flag], "caps_probability": flag in CAP_FLAGS}


def _taint(con, case_row: dict) -> list[dict]:
    tid = int(case_row["flagged_txn_id"])
    r = con.execute("""select t.TransactionID, t.card_id, t.customer_id, t.card6, t.ts, t.TransactionAmt, t.addr1, t.holder_key,
                              t.risk_score, t.DeviceInfo, t.id_30, t.id_31, t.device_key,
                              exists (select 1 from ident i where i.TransactionID = t.TransactionID) has_id
                       from txn t where t.TransactionID = ?""", [tid]).fetchone()
    if r is None:
        return [_flag("flagged_txn_missing", [tid], f"Transaction {tid} named by the alert does not exist in the index.")]
    _, card, cust, card6, ts, amt, addr1, hk, score, dinfo, id30, id31, dkey, has_id = r
    out = []
    stated_card, stated_cust = case_row.get("card_id"), case_row.get("customer_id")
    if (stated_card and stated_card != card) or (stated_cust and stated_cust != cust):
        out.append(_flag("alert_card_mismatch", [tid, stated_card, card],
                         f"The alert names card {stated_card} (customer {stated_cust}), but transaction {tid} is on card {card} "
                         f"(customer {cust}, card type {card6})."))
    if not has_id:
        out.append(_flag("no_identity_record", [tid], f"Transaction {tid} has no identity record (no DeviceInfo, id_30, id_31)."))
    if addr1 is None:
        out.append(_flag("empty_billing_region", [tid], f"Transaction {tid} has an empty addr1 (billing region)."))
    prior = con.execute("""select TransactionID from txn where card_id = ? and (ts < ? or (ts = ? and TransactionID < ?))
                           order by ts desc, TransactionID desc""", [card, ts, ts, tid]).fetchall()
    n_card = len(prior)
    if _valid_holder(hk):
        n_holder = con.execute("""select count(*) from txn where holder_key = ? and (ts < ? or (ts = ? and TransactionID < ?))""",
                               [hk, ts, ts, tid]).fetchone()[0]
    else:
        n_holder = n_card            # no usable holder key: the card's own history is all there is
    if n_card < THIN and n_holder < THIN:
        out.append(_flag("thin_history", [card] + [p[0] for p in prior[:10]],
                         f"Card {card} has {n_card} prior transaction(s) and holder key {hk if _valid_holder(hk) else '(unusable)'} "
                         f"has {n_holder} before transaction {tid}."))
    ph = []
    if has_id and dinfo in PLACEHOLDER_DEVICEINFO:
        ph.append(f"DeviceInfo '{dinfo}'")
    if has_id and id31 in PLACEHOLDER_ID31:
        ph.append(f"id_31 '{id31}'")
    if has_id and (id30 is None or str(id30).strip() == ""):
        ph.append("empty id_30")
    if ph:
        out.append(_flag("placeholder_device", [tid, dkey], f"Device profile of transaction {tid} uses placeholder values: {', '.join(ph)}."))
    if ts is not None and str(ts) >= JANUARY:
        out.append(_flag("january_score", [tid], f"Transaction {tid} is from {str(ts)[:10]}; its risk_score {score} comes from the "
                                                 "model that was not retrained for January."))
    return out


def probability_cap(flags: list[dict], fraud_groups=()) -> float | None:
    """TRD2 section 9: no identity record and thin history cap p at 0.60 unless a network or history group supports fraud."""
    names = {f["flag"] for f in flags}
    if CAP_FLAGS <= names and not (CAP_EXEMPT_GROUPS & set(fraud_groups)):
        return CAP
    return None


# ---------------------------------------------------------------------------------------------------------------------
# whole-index report

def _rows(con, sql: str, params=()) -> list[dict]:
    cur = con.execute(sql, list(params))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def report(duck_path=None, out_md=None, pack_round=None) -> dict:
    t0 = time.perf_counter()
    path = Path(duck_path or DUCK_PATH)
    con = connect(path)
    base = f"""(select t.TransactionID, strftime(t.ts, '%Y-%m') m, t.ProductCD, t.TransactionAmt amt, t.addr1, t.card_id, t.ts,
                       t.DeviceInfo, t.id_30, t.id_31, t.risk_score, i.TransactionID is not null has_id
                from txn t left join (select distinct TransactionID from ident) i on i.TransactionID = t.TransactionID)"""
    rep: dict = {"duck_path": path.as_posix(), "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    rep["totals"] = _rows(con, f"""select count(*) txns, count(*) filter (where not has_id) no_identity,
                                    count(*) filter (where addr1 is null) empty_addr1, min(ts) ts_min, max(ts) ts_max from {base}""")[0]
    rep["no_identity"] = _rows(con, f"""select m as "month", ProductCD product, count(*) txns, count(*) filter (where not has_id) no_identity,
                                         round(count(*) filter (where not has_id) / count(*), 4) as share,
                                         (list(TransactionID order by TransactionID) filter (where not has_id))[1:{SAMPLE}] sample_ids
                                  from {base} group by all order by 1, 2""")
    rep["empty_addr1"] = _rows(con, f"""select m as "month", ProductCD product, count(*) txns, count(*) filter (where addr1 is null) empty_addr1,
                                         round(count(*) filter (where addr1 is null) / count(*), 4) as share,
                                         (list(TransactionID order by TransactionID) filter (where addr1 is null))[1:{SAMPLE}] sample_ids
                                  from {base} group by all order by 1, 2""")
    di = ", ".join(f"'{x}'" for x in PLACEHOLDER_DEVICEINFO)
    i31 = ", ".join(f"'{x}'" for x in PLACEHOLDER_ID31)
    rep["placeholder_device"] = _rows(con, f"""
        with p as (select *, case when DeviceInfo in ({di}) then 'DeviceInfo = ' || DeviceInfo end a,
                              case when id_31 in ({i31}) then 'id_31 = ' || id_31 end b,
                              case when has_id and (id_30 is null or trim(id_30) = '') then 'id_30 empty (identity present)' end c
                   from {base} where has_id),
             u as (select TransactionID, card_id, ts, a v from p where a is not null
                   union all select TransactionID, card_id, ts, b from p where b is not null
                   union all select TransactionID, card_id, ts, c from p where c is not null)
        select v as "value", count(*) txns, count(distinct card_id) cards, count(*) filter (where ts >= '{JANUARY}') january,
               (list(TransactionID order by TransactionID))[1:{SAMPLE}] sample_ids
        from u group by 1 order by 2 desc""")
    rep["identity_rows"] = con.execute("select count(*) from ident").fetchone()[0]
    rep["thin_cards"] = _rows(con, f"""
        with c as (select card_id, count(*) n, count(*) filter (where ts >= '{JANUARY}') n_jan from txn group by 1)
        select count(*) cards, count(*) filter (where n < {THIN} + 1) thin_cards,
               coalesce(sum(n) filter (where n < {THIN} + 1), 0) thin_txns,
               count(*) filter (where n < {THIN} + 1 and n_jan > 0) thin_cards_active_january,
               (list(card_id order by card_id) filter (where n < {THIN} + 1))[1:{SAMPLE}] sample_cards,
               (list(card_id order by card_id) filter (where n < {THIN} + 1 and n_jan > 0))[1:{SAMPLE}] sample_cards_january
        from c""")[0]
    rep["duplicates"] = {
        "same_transaction_id": _rows(con, """select count(*) ids, coalesce(sum(k), 0) as "rows", (list(TransactionID order by TransactionID))[1:5] sample_ids
                                             from (select TransactionID, count(*) k from txn group by 1 having count(*) > 1)""")[0],
        "same_card_ts_amount_product": _rows(con, """
            select count(*) as "groups", coalesce(sum(k), 0) as "rows", (list(ids order by ids[1]))[1:5] sample_groups
            from (select card_id, ts, TransactionAmt, ProductCD, count(*) k, list(TransactionID order by TransactionID) ids
                  from txn group by all having count(*) > 1)""")[0],
    }
    rep["non_positive_amounts"] = _rows(con, f"""select count(*) filter (where TransactionAmt <= 0) non_positive,
                                                  count(*) filter (where TransactionAmt is null) null_amount,
                                                  (list(TransactionID order by TransactionID) filter (where TransactionAmt <= 0 or TransactionAmt is null))[1:{SAMPLE}] sample_ids
                                           from txn""")[0]
    lab = ("(select distinct TransactionID from closed_txn where outcome = 'confirmed_fraud')" if _has(con, "closed_txn", "outcome")
           else "(select cast(null as bigint) TransactionID where false)")
    rep["risk_by_month"] = _rows(con, f"""
        with b as (select strftime(t.ts, '%Y-%m') m, t.risk_score s, l.TransactionID is not null fraud
                   from txn t left join {lab} l on l.TransactionID = t.TransactionID where t.risk_score is not null),
             r as (select *, rank() over (partition by m order by s) rk from b)
        select m as "month", count(*) txns, round(avg(s), 4) as mean, round(quantile_cont(s, 0.5), 4) p50, round(quantile_cont(s, 0.9), 4) p90,
               round(count(*) filter (where s >= 0.5) / count(*), 4) share_ge_05, count(*) filter (where fraud) labelled_fraud,
               round(avg(s) filter (where fraud), 4) mean_fraud, round(avg(s) filter (where not fraud), 4) mean_other,
               case when count(*) filter (where fraud) > 0 and count(*) filter (where not fraud) > 0 then
                    round((sum(rk) filter (where fraud) - count(*) filter (where fraud) * (count(*) filter (where fraud) + 1) / 2.0)
                          / (count(*) filter (where fraud) * count(*) filter (where not fraud)), 3) end auc
        from r group by 1 order by 1""")
    # the investigations these gaps taint: every case of the latest pack
    cols = "case_id, trigger_type, flagged_txn_id, card_id, customer_id"
    if _has(con, "cases", "round"):
        rnd = pack_round or con.execute("select max(round) from cases").fetchone()[0]
        cases = _rows(con, f"select {cols} from cases where round = ? order by case_id", [rnd])
    else:
        rnd, cases = pack_round, _rows(con, f"select {cols} from cases order by case_id")
    rep["pack_round"] = rnd
    rep["taint"] = []
    for c in cases:
        fl = _taint(con, c)
        rep["taint"].append({"case_id": c["case_id"], "trigger_type": c["trigger_type"], "flagged_txn_id": c["flagged_txn_id"],
                             "flags": fl, "cap_rule_applies": probability_cap(fl) is not None})
    con.close()
    rep["elapsed_s"] = round(time.perf_counter() - t0, 1)
    md = Path(out_md or REPORT_MD)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(render(rep), encoding="utf-8")
    return rep


def _tbl(head, rows) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def _ids(v) -> str:
    return ", ".join(str(x) for x in (v or []))


def render(rep: dict) -> str:
    tot, thin, dup, npos = rep["totals"], rep["thin_cards"], rep["duplicates"], rep["non_positive_amounts"]
    tainted = [t for t in rep["taint"] if any(f["flag"] != "january_score" for f in t["flags"])]
    jan = [r for r in rep["no_identity"] if r["month"] >= JANUARY[:7]]
    jan_n, jan_k = sum(r["txns"] for r in jan), sum(r["no_identity"] for r in jan)
    L = ["# Data-quality report (challenge 11)", "",
         f"Generated by `python -m kavach.dq` at {rep['generated_at']} over `{rep['duck_path']}` (read-only) in {rep['elapsed_s']} s. "
         f"{tot['txns']:,} transactions from {str(tot['ts_min'])[:10]} to {str(tot['ts_max'])[:10]}. Every problem lists sample record ids; "
         "`kavach.dq.taint(case_row)` returns the full list for one investigation and is stored in its attestation sidecar.", "",
         "## Headline findings", "",
         f"- **Identity gap:** {tot['no_identity']:,} of {tot['txns']:,} transactions ({tot['no_identity'] / max(1, tot['txns']):.1%}) have no identity "
         f"record ({jan_k:,} of {jan_n:,} in January, {jan_k / max(1, jan_n):.1%}). The brief says a quarter have none; in this data a quarter "
         "*have* one. Device evidence is absent for most activity, so its absence never exonerates.",
         f"- **Empty billing region:** {tot['empty_addr1']:,} transactions ({tot['empty_addr1'] / max(1, tot['txns']):.1%}) have no addr1; region "
         "checks and the holder key cannot run on them.",
         f"- **Thin cards:** {thin['thin_cards']:,} of {thin['cards']:,} cards have fewer than {THIN} prior transactions at their last transaction "
         f"({thin['thin_cards_active_january']:,} of them active in January).",
         "- **Placeholder devices:** " + ", ".join(f"{p['value']} ({p['txns']:,} txns on {p['cards']:,} cards)"
                                                  for p in rep["placeholder_device"][:6]) + ".",
         f"- **Duplicates:** {dup['same_transaction_id']['ids']} duplicated TransactionIDs; {dup['same_card_ts_amount_product']['groups']} groups "
         "of distinct ids with the same card, second, amount and product.",
         f"- **Amounts:** {npos['non_positive']} non-positive, {npos['null_amount']} null.",
         "- **Score drift:** the bank's model was not retrained for January; see the per-month table (no January labels exist, so its "
         "accuracy there cannot be measured at all).",
         f"- **Investigations tainted:** {len(tainted)} of {len(rep['taint'])} cases in pack round {rep['pack_round']} carry a flag beyond the "
         f"January score; the p cap rule (no identity record and thin history) applies to "
         f"{', '.join(t['case_id'] for t in rep['taint'] if t['cap_rule_applies']) or 'none'}.", "",
         "## Investigations it taints", "",
         _tbl(["case", "trigger", "flagged txn", "flags (record ids)", "p cap rule"],
              [[t["case_id"], t["trigger_type"], t["flagged_txn_id"],
                "; ".join(f"{f['flag']} ({_ids(f['record_ids'][:4])})" for f in t["flags"]) or "none", "yes" if t["cap_rule_applies"] else "no"]
               for t in rep["taint"]]), "",
         "Consequence of each flag:", ""] + [f"- `{k}`: {v}" for k, v in CONSEQUENCE.items()] + ["",
         f"Rule for the agent (TRD2 section 9): when the flagged transaction has no identity record and the account has fewer than {THIN} "
         f"prior transactions, p is capped at {CAP:.2f} unless a network or history group supports fraud, and the summary says why.", "",
         "## Transactions without an identity record, by month and product", "",
         _tbl(["month", "product", "txns", "no identity", "share", "sample ids"],
              [[r["month"], r["product"], f"{r['txns']:,}", f"{r['no_identity']:,}", f"{r['share']:.1%}", _ids(r["sample_ids"])] for r in rep["no_identity"]]), "",
         "## Empty billing region (addr1), by month and product", "",
         _tbl(["month", "product", "txns", "empty addr1", "share", "sample ids"],
              [[r["month"], r["product"], f"{r['txns']:,}", f"{r['empty_addr1']:,}", f"{r['share']:.1%}", _ids(r["sample_ids"])]
               for r in rep["empty_addr1"] if r["empty_addr1"]]), "",
         "## Placeholder device values (identity present)", "",
         _tbl(["value", "txns", "cards", "January txns", "sample ids"],
              [[p["value"], f"{p['txns']:,}", f"{p['cards']:,}", f"{p['january']:,}", _ids(p["sample_ids"])] for p in rep["placeholder_device"]]), "",
         "## Cards too thin to judge", "",
         f"{thin['thin_cards']:,} of {thin['cards']:,} cards have at most {THIN} transactions in total ({thin['thin_txns']:,} transactions). "
         f"Sample cards: {_ids(thin['sample_cards'])}. Thin cards active in January: {thin['thin_cards_active_january']:,} "
         f"(sample: {_ids(thin['sample_cards_january'])}).", "",
         "## Duplicates and amounts", "",
         f"- Same TransactionID more than once: {dup['same_transaction_id']['ids']} ids ({_ids(dup['same_transaction_id']['sample_ids'])}).",
         f"- Same card, second, amount and product under different ids: {dup['same_card_ts_amount_product']['groups']} groups, "
         f"{dup['same_card_ts_amount_product']['rows']} rows (sample groups: {'; '.join(_ids(g) for g in dup['same_card_ts_amount_product']['sample_groups'])}).",
         f"- Non-positive amounts: {npos['non_positive']}; null amounts: {npos['null_amount']} ({_ids(npos['sample_ids'])}).", "",
         "## risk_score distribution per month", "",
         "Labelled fraud = transactions listed in a confirmed-fraud closed case. AUC ranks ties low (approximate).", "",
         _tbl(["month", "txns", "mean", "p50", "p90", "share >= 0.5", "labelled fraud", "mean fraud", "mean other", "AUC"],
              [[r["month"], f"{r['txns']:,}", r["mean"], r["p50"], r["p90"], r["share_ge_05"], r["labelled_fraud"], r["mean_fraud"],
                r["mean_other"], r["auc"]] for r in rep["risk_by_month"]]), ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kavach.dq")
    ap.add_argument("--duck", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--round", type=int, default=None)
    a = ap.parse_args(argv)
    rep = report(a.duck, a.out, a.round)
    print(f"dq report: {rep['elapsed_s']} s over {rep['duck_path']} -> {a.out or REPORT_MD}")
    for t in rep["taint"]:
        print(f"  {t['case_id']}: {', '.join(f['flag'] for f in t['flags']) or 'none'}{'  [p cap]' if t['cap_rule_applies'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
