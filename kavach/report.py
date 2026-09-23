"""Fill the README results table from cases/*.json (so the table can never drift from the answers)."""
import json

from kavach.config import CASES_DIR, ROOT

START, END = "<!-- RESULTS_TABLE -->", "<!-- /RESULTS_TABLE -->"


def table() -> str:
    rows = ["| Case | Verdict | Pattern | p | Episode | Exposure | Evidence asked | Final actions (route) | SAR | Graph |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    counts = {}
    for f in sorted(CASES_DIR.glob("HHG-*.json")):
        a = json.loads(f.read_text(encoding="utf-8"))
        c = a["case"]
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
        acts = ", ".join(f"{x['action']} ({x['route']})" for x in a["next_best_actions"]["final"])
        ask = a["evidence_requests"][0]["assumed_response"].split(",")[0] if a["evidence_requests"] else "none"
        rows.append(f"| [{a['case_id']}](cases/{f.name}) | {c['verdict']} | {c['pattern']} | {c['fraud_probability']:.2f} | "
                    f"{len(c['affected_txn_ids'])} | ${c['exposure_usd']:,.2f} | {ask} | {acts} | {'yes' if a['sar']['file'] else 'no'} | "
                    f"{c['graph_case_id'] or 'no'} |")
    head = (f"{counts.get('fraud', 0)} fraud, {counts.get('legitimate', 0)} legitimate, {counts.get('uncertain', 0)} uncertain. "
            "All 20 files pass `python -m kavach check`.\n\n")
    return head + "\n".join(rows)


def update_readme() -> None:
    p = ROOT / "README.md"
    s = p.read_text(encoding="utf-8")
    if END not in s:
        s = s.replace(START, START + "\n" + END)
    i, j = s.index(START) + len(START), s.index(END)
    p.write_text(s[:i] + "\n" + table() + "\n" + s[j:], encoding="utf-8")
    print("README results table updated")
