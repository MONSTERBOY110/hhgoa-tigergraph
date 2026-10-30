"""Round 1 regression: our current answers for HHG-001..020 versus the bank's real outcomes (closed cases CC-5566..CC-5585)."""
import pathlib, sys; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # run from anywhere
import os
os.environ["KAVACH_NO_LLM"] = "1"; os.environ["KAVACH_RECORD"] = "0"
from kavach import duckq
from kavach.harness.runner import run_case
truth = duckq.q("select * from closed where case_id between 'CC-5566' and 'CC-5585' order by case_id")
pack = duckq.case_pack(1)
rows, ok_v, harm = [], 0, 0
for (_, t), c in zip(truth.iterrows(), pack.to_dict("records")):
    o = run_case(c, quiet=True); a = o.answer; k = a["case"]
    tv = "fraud" if t.outcome == "confirmed_fraud" else "legitimate"
    acts = {x["action"] for x in a["next_best_actions"]["final"]}
    bad = tv == "legitimate" and bool(acts & {"BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"})
    harm += bad; ok_v += k["verdict"] == tv
    ours = set(k["affected_txn_ids"]); theirs = set(str(t.txn_ids).split("|")) if tv == "fraud" else set()
    print(f"{c['case_id']} truth={tv:10s} ours={k['verdict']:10s} p={k['fraud_probability']:.2f} pat={k['pattern'][:14]:14s}/{str(t.pattern)[:14]:14s} "
          f"ep {len(ours & theirs)}/{len(theirs)} (+{len(ours - theirs)}) sar {a['sar']['file']}/{t.report_filed} {'HARM' if bad else ''}", flush=True)
print(f"verdict correct {ok_v}/20, legitimate customers harmed {harm}")
