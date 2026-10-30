"""Duck lane vs TigerGraph lane on every case of a pack: identical decisions? (no writes, no LLM)"""
import pathlib, sys; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # run from anywhere
import os, sys
os.environ["KAVACH_NO_LLM"] = "1"; os.environ["KAVACH_RECORD"] = "0"
from kavach import duckq
from kavach.harness.runner import run_case
rnd = int(sys.argv[1]) if len(sys.argv) > 1 else 2
only = sys.argv[2:]
def dec(a):
    k = a["case"]
    return (k["verdict"], k["fraud_probability"], k["pattern"], tuple(k["affected_txn_ids"]), k["first_suspicious_txn_id"], k["exposure_usd"],
            tuple(k["connected_card_ids"]), tuple(k["connected_device_profiles"]),
            tuple((x["action"], x["route"]) for x in a["next_best_actions"]["initial"]),
            tuple((x["action"], x["route"]) for x in a["next_best_actions"]["final"]), a["sar"]["file"])
ok = 0; rows = [r for r in duckq.case_pack(rnd).to_dict("records") if not only or r["case_id"] in only]
for c in rows:
    out = {}
    for b in ("duck", "tg"):
        o = run_case(c, backend=b, quiet=True)
        out[b] = ("INCIDENT " + str((o.incident or {}).get("error"))[:160]) if o.answer is None else dec(o.answer)
        if o.answer is not None: out[b + "_t"] = (o.answer["tool_calls"], o.answer["latency_s"])
    same = out["duck"] == out["tg"]; ok += same
    print(c["case_id"], "SAME" if same else "DIFF", out.get("duck_t"), out.get("tg_t"), "" if same else f"\n  duck={out['duck']}\n  tg  ={out['tg']}", flush=True)
print(f"parity {ok}/{len(rows)}")
