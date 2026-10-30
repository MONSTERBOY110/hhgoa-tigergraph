"""Drill: approval by the wrong role, against a running `python -m kavach serve` (default http://127.0.0.1:8799).

Usage: python logs/drills/approvals_drill.py <scratch_dir> [base_url]
Tokens are read from the environment after `import kavach.config` (.env) and never printed; output shows a short
sha256 fingerprint of each token instead. The server must run with KAVACH_APPROVALS_FILE=<scratch_dir>/approvals.jsonl.
Step 6 tampers a COPY of that log and runs approvals.verify() on the copy."""
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import httpx  # noqa: E402

import kavach.config  # noqa: E402,F401  (loads .env)
from kavach.harness import approvals  # noqa: E402

scratch = Path(sys.argv[1]).resolve()
base = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8799"
tok = {r: os.environ.get(approvals.TOKEN_ENV[r], "") for r in approvals.ROLES}
assert all(len(t) >= approvals.MIN_TOKEN_LEN for t in tok.values()), "role tokens missing in .env"
fp = {r: hashlib.sha256(t.encode()).hexdigest()[:12] for r, t in tok.items()}
c = httpx.Client(base_url=base, timeout=30)
results = []


def show(label, r, expect):
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:120]
    ok = r.status_code == expect
    rec = body.get("record") if isinstance(body, dict) else None
    detail = (body.get("detail") if isinstance(body, dict) else body) or ""
    line = f"[{'PASS' if ok else 'FAIL'}] {label}: HTTP {r.status_code} (expected {expect})"
    if rec:
        line += f" record status={rec['status']} role={rec['role']} route={rec['route']} approver={rec['approver']}"
    elif detail:
        line += f" | {str(detail)[:200]}"
    print(line, flush=True)
    results.append({"step": label, "status": r.status_code, "expected": expect, "ok": ok})
    return body


def post(case, body, role=None, raw_token=None):
    h = {}
    if role:
        h["Authorization"] = f"Bearer {tok[role]}"
    elif raw_token:
        h["Authorization"] = f"Bearer {raw_token}"
    return c.post(f"/cases/{case}/approvals", json=body, headers=h)


print("health:", json.dumps({k: v for k, v in c.get("/health").json().items() if k != "time"}))
print("token fingerprints (sha256[:12], the approver id stored in each record):", fp)
print("-- 1. analyst token on L2 and L1 actions")
show("analyst approves FILE_REPORT on HHF-003 (L2)", post("HHF-003", {"action": "FILE_REPORT", "note": "drill"}, "analyst"), 403)
show("analyst approves BLOCK_CARD on HHF-007 (L1, exposure 246.38)", post("HHF-007", {"action": "BLOCK_CARD", "note": "drill"}, "analyst"), 403)
print("-- 2. a role claimed in the body is not enforcement")
b = show("analyst token, body {'role': 'L2'} approves FILE_REPORT on HHF-003",
         post("HHF-003", {"action": "FILE_REPORT", "role": "L2", "note": "I am L2, trust me"}, "analyst"), 403)
print("   ignored_fields:", b.get("ignored_fields"))
show("L1 token, body {'exposure': 10} approves BLOCK_CARD on HHF-003 (server exposure 8551.47 -> L2 route)",
     post("HHF-003", {"action": "BLOCK_CARD", "exposure": 10, "note": "small, honest"}, "L1"), 403)
print("-- 3. no token, unknown token")
show("no Authorization header", post("HHF-003", {"action": "FILE_REPORT"}), 401)
show("unknown bearer token", post("HHF-003", {"action": "FILE_REPORT"}, raw_token="not-a-real-token-0123456789"), 403)
show("unknown action", post("HHF-003", {"action": "APPROVE_EVERYTHING"}, "L2"), 422)
print("-- 4. the right roles")
show("L2 approves FILE_REPORT on HHF-003", post("HHF-003", {"action": "FILE_REPORT", "note": "drill: reviewed SAR"}, "L2"), 200)
show("L1 approves BLOCK_CARD on HHF-007 (L1 route)", post("HHF-007", {"action": "BLOCK_CARD", "note": "drill"}, "L1"), 200)
show("L2 approves BLOCK_CARD on HHF-003 (L2 route)", post("HHF-003", {"action": "BLOCK_CARD", "note": "drill"}, "L2"), 200)
v = c.get("/approvals/verify").json()
print("GET /approvals/verify:", v)
print("-- 5. no API can change or remove an approval")
paths = sorted({p for p in c.get("/openapi.json").json()["paths"]})
methods = {p: sorted(m.upper() for m in ops) for p, ops in c.get("/openapi.json").json()["paths"].items()}
print("   routes:", json.dumps(methods))
for m in ("PUT", "PATCH", "DELETE"):
    for p in ("/cases/HHF-003/approvals", "/approvals", f"/approvals/{approvals.records()[-1]['id']}"):
        r = c.request(m, p, json={"status": "approved"}, headers={"Authorization": f"Bearer {tok['L2']}"})
        print(f"   {m} {p} -> {r.status_code}")
        results.append({"step": f"{m} {p}", "status": r.status_code, "expected": "404/405", "ok": r.status_code in (404, 405)})
print("   public functions in kavach.harness.approvals:",
      sorted(n for n in dir(approvals) if not n.startswith("_") and callable(getattr(approvals, n)) and getattr(getattr(approvals, n), "__module__", "") == approvals.__name__))
print("-- 6. tamper with a copy of the log")
log = approvals.path()
copy = scratch / "approvals_tampered.jsonl"
shutil.copy2(log, copy)
lines = copy.read_text(encoding="utf-8").splitlines()
i = next(k for k, ln in enumerate(lines) if '"status": "denied"' in ln)
lines[i] = lines[i].replace('"status": "denied"', '"status": "approved"', 1)
copy.write_text("\n".join(lines) + "\n", encoding="utf-8")
os.environ["KAVACH_APPROVALS_FILE"] = str(copy)
print(f"   edited line {i + 1} of the copy: denied -> approved")
tampered = approvals.verify_detail()
print("   verify() on the tampered copy:", tampered)
os.environ["KAVACH_APPROVALS_FILE"] = str(log)
original = approvals.verify_detail()
print("   verify() on the original:", original)
results.append({"step": "tampered copy fails verify, original passes", "ok": (not tampered["ok"]) and original["ok"]})
print(f"{sum(1 for r in results if r['ok'])}/{len(results)} checks as expected")
(scratch / "approvals_drill.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
