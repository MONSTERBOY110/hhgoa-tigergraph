"""Dry run of a case pack through the harness (no writes, no LLM): decisions + validator errors per case."""
import pathlib, sys; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # run from anywhere
import os, sys
os.environ.setdefault("KAVACH_NO_LLM", "1"); os.environ["KAVACH_RECORD"] = "0"
from kavach import duckq
from kavach.harness.runner import run_case
from kavach.validate import review, load_index
rnd = int(sys.argv[1]) if len(sys.argv) > 1 else 2
idx = load_index(rnd)
for c in duckq.case_pack(rnd).to_dict("records"):
    o = run_case(c, quiet=True)
    if o.answer is None:
        print(c["case_id"], "INCIDENT", str((o.incident or {}).get("error", ""))[:200]); continue
    a = o.answer; k = a["case"]
    errs, warns = review(a, idx, f"{a['case_id']}.json")
    fa = ",".join(x["action"] for x in a["next_best_actions"]["final"])
    rq = a["evidence_requests"][0]["assumed_response"][:28] if a["evidence_requests"] else "-"
    print(f"{a['case_id']} {c['trigger_type'][:8]:8s} {k['verdict']:10s} p={k['fraud_probability']:.2f} {k['pattern']:26s} n={len(k['affected_txn_ids']):2d} "
          f"exp={k['exposure_usd']:8.2f} conn={len(k['connected_card_ids'])} sar={a['sar']['file']!s:5s} [{rq}] {fa} | errs={len(errs)} {errs[:2]}", flush=True)
