"""Drill: data protection. Plant an email address and real secret values (from .env, never printed) in an alert's free
text, POST it to a running server that persists into <scratch_dir> (started through logs/drills/kv_scratch.py), then
scan every file the run wrote (answer, trace, attestation sidecar, casefile, incidents, approvals log, server log) for
the raw secret values and full email addresses. Prints variable names and counts only.

Usage: python logs/drills/protect_drill.py <scratch_dir> [base_url]"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx  # noqa: E402

import kavach.config  # noqa: E402,F401  (loads .env)
from kavach import duckq  # noqa: E402
from kavach.harness.redact import SECRET_NAME  # noqa: E402

scratch = Path(sys.argv[1]).resolve()
base_url = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8799"
secrets = {k: v.strip() for k, v in os.environ.items() if SECRET_NAME.search(k) and v and len(v.strip()) >= 8}
print(f"secret-named variables in the environment: {len(secrets)} ({', '.join(sorted(secrets))})")
EMAIL_FULL = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
planted_email = "jane.customer@example.com"
row = {k: (None if isinstance(v, float) and v != v else v) for k, v in
       next(r for r in duckq.case_pack().to_dict("records") if r["case_id"] == "HHF-003").items()}
plant = [f"Reach me at {planted_email}."]
for name in ("LLM_API_KEY", "KAVACH_TOKEN_L2", "TG_PASSWORD", "TG_SECRET"):
    if name in secrets:
        plant.append(f"{name.lower()} is {secrets[name]}")
row.update({"case_id": "DRILL-PROTECT", "trigger_text": row["trigger_text"] + " " + " ".join(plant)})
c = httpx.Client(base_url=base_url, timeout=60)
r = c.post("/alerts", json=row)
print("POST /alerts:", r.status_code, r.json())
for _ in range(300):
    j = c.get("/alerts/DRILL-PROTECT").json()
    if j["status"] != "running":
        break
    time.sleep(0.2)
print("job:", {k: j.get(k) for k in ("status", "verdict", "persisted")})
# a second alert that fails closed with secrets in its structured fields (they land in tool parameters, the partial
# trace and the incident): card_id = a role token, customer_id = an email address, a transaction that does not exist
row2 = {**row, "case_id": "DRILL-PROTECT2", "card_id": secrets.get("KAVACH_TOKEN_L1", "x" * 20)[:64],
        "customer_id": planted_email, "flagged_txn_id": "9999999"}
r2 = c.post("/alerts", json=row2)
print("POST /alerts (secret in card_id, email in customer_id, unknown txn):", r2.status_code)
for _ in range(300):
    j2 = c.get("/alerts/DRILL-PROTECT2").json()
    if j2["status"] != "running":
        break
    time.sleep(0.2)
print("job:", {k: j2.get(k) for k in ("status", "incident_kind", "answer_written")})
# the API responses themselves
api = (json.dumps(c.get("/cases/DRILL-PROTECT").json()) + json.dumps(c.get("/cases/DRILL-PROTECT/replay").json()) + json.dumps(j)
       + json.dumps(c.get("/cases/DRILL-PROTECT2").json()) + json.dumps(j2))
files = [p for p in scratch.rglob("*") if p.is_file() and p.suffix in (".json", ".jsonl", ".log")]
bad = 0
for p in sorted(files) + [None]:
    text = api if p is None else p.read_text(encoding="utf-8", errors="replace")
    label = "API responses (GET /cases, /replay, /alerts)" if p is None else str(p.relative_to(scratch))
    leaked = [k for k, v in secrets.items() if v in text]
    emails = sorted(set(EMAIL_FULL.findall(text)))
    masked = text.count("[REDACTED")
    flag = "LEAK" if leaked or emails else "ok"
    bad += flag != "ok"
    if "DRILL-PROTECT" in label or p is None or leaked or emails or label.endswith(".log") or "incidents" in label:
        print(f"[{flag}] {label}: secret values found {leaked}, full emails {emails}, [REDACTED markers {masked}, "
              f"masked email j***@example.com present: {'j***@example.com' in text}")
print(f"scanned {len(files)} files + API responses: {'no secret value and no full email address anywhere' if not bad else f'{bad} LEAK(S)'}")
