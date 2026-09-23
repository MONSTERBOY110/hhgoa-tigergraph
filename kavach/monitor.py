"""Autonomous monitor: sweep November and December for activity no alert pointed at, then investigate it.

Three general sweeps, none keyed on the exam cases:
  1. sub-threshold bursts: 3+ online purchases just under a round limit within 40 minutes on one card
  2. scripted device rings: a rare device profile, almost always New and behind an anonymous proxy, on many cards
  3. rare-device rings: a device seen on few cards ever, paying similar amounts on 3+ cards within 7 days
Each candidate becomes a synthetic alert (trigger "analyst_request", opened one hour after its latest transaction)
investigated by the same agent; results go to monitor/MON-xxx.json with the answer-file schema.
"""
import json

import pandas as pd

from kavach import duckq as d
from kavach.config import ROOT

OUT = ROOT / "monitor"
START, END = "2016-11-01", "2016-12-31 23:59:59"


def _exam_cards() -> set:
    return set(d.case_pack().card_id)


def sweep_sub_threshold(limit: int = 3) -> list[dict]:
    b = d.band_bursts(440, 500, "2016-12-01", days=31, minutes=40, min_n=3)
    b = b[(b.first_ts >= START) & ~b.card_id.isin(_exam_cards())].copy()
    b["span"] = (b.last_ts - b.first_ts).dt.total_seconds()
    b = b.sort_values(["n", "span"], ascending=[False, True])
    out = []
    for r in b.head(limit).itertuples():
        t = d.q("""select TransactionID, ts from txn where card_id = ? and channel = 'online' and TransactionAmt >= 440
                   and TransactionAmt < 500 and ts >= ? order by ts""", r.card_id, START)
        last = t.iloc[min(len(t) - 1, 3)] if len(t) else None
        if last is None:
            continue
        out.append({"kind": "sub_threshold_burst", "card_id": r.card_id, "txn": int(last.TransactionID), "ts": last.ts,
                    "text": f"Monitor sweep: {int(r.n)} online purchases just under $500 within 40 minutes on card {r.card_id}."})
    return out


def sweep_device_rings(limit: int = 2) -> list[dict]:
    rings = d.q(f"""select device_key, count(distinct card_id) cards, count(*) n,
                     avg(case when id_15 = 'New' then 1.0 else 0 end) new_share,
                     avg(case when id_23 like '%ANONYMOUS%' then 1.0 else 0 end) anon_share
                   from txn where ts between '{START}' and '{END}' and device_key is not null
                     and replace(replace(device_key, '|', ''), ' ', '') <> ''
                   group by 1 having count(distinct card_id) >= 5 and avg(case when id_15 = 'New' then 1.0 else 0 end) >= 0.8
                     and avg(case when id_23 like '%ANONYMOUS%' then 1.0 else 0 end) >= 0.5
                   order by cards desc""")
    out, exam = [], _exam_cards()
    for r in rings.head(limit).itertuples():
        t = d.q(f"""select TransactionID, card_id, ts from txn where device_key = ? and ts between '{START}' and '{END}'
                   order by ts desc""", r.device_key)
        t = t[~t.card_id.isin(exam)]
        if t.empty:
            continue
        x = t.iloc[0]
        out.append({"kind": "scripted_device_ring", "card_id": x.card_id, "txn": int(x.TransactionID), "ts": x.ts,
                    "text": f"Monitor sweep: device profile {r.device_key} used on {r.cards} cards in November and December, "
                            f"{r.new_share:.0%} New and {r.anon_share:.0%} behind an anonymous proxy."})
    return out


def sweep_rare_device_rings(limit: int = 2) -> list[dict]:
    c = d.q(f"""with dv as (select device_key, count(distinct card_id) pop from txn
                          where device_key is not null and replace(replace(device_key, '|', ''), ' ', '') <> '' group by 1),
                w as (select t.TransactionID, t.card_id, t.ts, t.TransactionAmt amt, t.device_key from txn t join dv using (device_key)
                      where dv.pop <= 10 and t.ts between '{START}' and '{END}')
                select a.device_key, a.TransactionID, a.card_id, a.ts, a.amt, count(distinct b.card_id) k
                from w a join w b on a.device_key = b.device_key and b.card_id <> a.card_id
                  and b.ts between a.ts - interval 7 day and a.ts + interval 7 day and abs(b.amt - a.amt) <= 0.02 * a.amt + 0.5
                group by all having count(distinct b.card_id) >= 3 order by k desc, a.ts""")
    c = c[~c.card_id.isin(_exam_cards())]
    out, seen = [], set()
    for r in c.itertuples():
        if r.device_key in seen:
            continue
        seen.add(r.device_key)
        out.append({"kind": "rare_device_ring", "card_id": r.card_id, "txn": int(r.TransactionID), "ts": r.ts,
                    "text": f"Monitor sweep: rare device profile {r.device_key} paid about ${r.amt:,.0f} on {r.k + 1} cards within 7 days."})
        if len(out) >= limit:
            break
    return out


def run(limit_total: int = 6, backend: str = "duck") -> list[dict]:
    from kavach.agent import investigate
    from kavach.llm import Llm

    cands = sweep_sub_threshold(2) + sweep_device_rings(2) + sweep_rare_device_rings(2)
    OUT.mkdir(exist_ok=True)
    llm, rows = Llm(), []
    for i, c in enumerate(cands[:limit_total], 1):
        cid = f"MON-{i:03d}"
        case = {"case_id": cid, "opened_at": f"{pd.Timestamp(c['ts']) + pd.Timedelta(hours=1):%Y-%m-%d %H:%M:%S}",
                "trigger_type": "analyst_request", "trigger_text": c["text"], "flagged_txn_id": str(c["txn"]),
                "card_id": c["card_id"], "customer_id": c["card_id"].split("-")[0], "risk_score": ""}
        ans, trace = investigate(case, backend=backend, llm=llm)
        ans["monitor"] = {"sweep": c["kind"], "trigger_text": c["text"], "flagged_txn_id": str(c["txn"]), "card_id": c["card_id"]}
        (OUT / f"{cid}.json").write_text(json.dumps(ans, indent=2, ensure_ascii=False), encoding="utf-8")
        k = ans["case"]
        rows.append({"id": cid, "sweep": c["kind"], "card": c["card_id"], "verdict": k["verdict"], "p": k["fraud_probability"],
                     "pattern": k["pattern"], "exposure": k["exposure_usd"], "sar": ans["sar"]["file"], "text": c["text"]})
        print(f"{cid} {c['kind']:22s} {c['card_id']} -> {k['verdict']} p={k['fraud_probability']} {k['pattern']} "
              f"${k['exposure_usd']:,.2f} sar={ans['sar']['file']}", flush=True)
    _readme(rows)
    return rows


def _readme(rows: list[dict]) -> None:
    lines = ["# Monitor findings", "",
             "Kavach's autonomous monitor sweeps November and December for activity that no alert pointed at, using three general "
             "sweeps (sub-threshold bursts, scripted device rings, rare-device rings), and investigates each candidate with the same "
             "agent and policy engine as the exam cases. The files use the answer schema plus a `monitor` block. These are extra "
             "findings for the Innovation score and are not part of the 20 answers in `cases/`.", "",
             "Run: `python -m kavach monitor`", "",
             "| File | Sweep | Card | Verdict | p | Pattern | Exposure | SAR | Why it was picked |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| [{r['id']}]({r['id']}.json) | {r['sweep']} | {r['card']} | {r['verdict']} | {r['p']:.2f} | {r['pattern']} | "
                     f"${r['exposure']:,.2f} | {'yes' if r['sar'] else 'no'} | {r['text'].replace('|', chr(92) + '|')} |")
    (OUT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
