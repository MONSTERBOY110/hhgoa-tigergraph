"""Fill the README results tables from the answer files (so a table can never drift from the answers).

Round 2 (HHF, cases/) between <!-- RESULTS_TABLE --> markers; Round 1 (HHG, round1/cases/) between <!-- RESULTS_TABLE_R1 --> markers.
"""
import json

from kavach.config import CASES_DIR, ROOT

MARKERS = {2: ("<!-- RESULTS_TABLE -->", "<!-- /RESULTS_TABLE -->"), 1: ("<!-- RESULTS_TABLE_R1 -->", "<!-- /RESULTS_TABLE_R1 -->")}
DIRS = {2: (CASES_DIR, "HHF-*.json", "cases"), 1: (ROOT / "round1" / "cases", "HHG-*.json", "round1/cases")}


def table(round: int = 2) -> str:
    d, pattern, rel = DIRS[round]
    rows = ["| Case | Verdict | Pattern | p | Episode | Exposure | Evidence asked | Final actions (route) | SAR | Graph | Calls | Tokens | Seconds |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    counts, files = {}, sorted(d.glob(pattern))
    for f in files:
        a = json.loads(f.read_text(encoding="utf-8"))
        c = a["case"]
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
        acts = ", ".join(f"{x['action']} ({x['route']})" for x in a["next_best_actions"]["final"])
        ask = a["evidence_requests"][0]["assumed_response"].split(",")[0] if a["evidence_requests"] else "none"
        rows.append(f"| [{a['case_id']}]({rel}/{f.name}) | {c['verdict']} | {c['pattern']} | {c['fraud_probability']:.2f} | "
                    f"{len(c['affected_txn_ids'])} | ${c['exposure_usd']:,.2f} | {ask} | {acts} | {'yes' if a['sar']['file'] else 'no'} | "
                    f"{c['graph_case_id'] or 'no'} | {a['tool_calls']} | {a['tokens']} | {a['latency_s']} |")
    head = (f"{counts.get('fraud', 0)} fraud, {counts.get('legitimate', 0)} legitimate, {counts.get('uncertain', 0)} uncertain "
            f"({len(files)} files).\n\n")
    return head + "\n".join(rows)


def update_readme() -> None:
    p = ROOT / "README.md"
    s = p.read_text(encoding="utf-8")
    for rnd, (start, end) in MARKERS.items():
        if start not in s:
            continue
        if end not in s:
            s = s.replace(start, start + "\n" + end)
        i, j = s.index(start) + len(start), s.index(end)
        s = s[:i] + "\n" + table(rnd) + "\n" + s[j:]
    p.write_text(s, encoding="utf-8")
    print("README results tables updated")
