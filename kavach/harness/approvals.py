"""Approvals: server-side role checks and an append-only, hash-chained log (data/approvals.jsonl).

Roles come only from secret bearer tokens configured in the environment (.env):
    KAVACH_TOKEN_ANALYST, KAVACH_TOKEN_L1, KAVACH_TOKEN_L2
A role written in a request body is never trusted; approve() resolves the token to a role itself.

Route rule (the policy's approval routes, computed here from the action and the exposure, never taken from the caller):
    L2 route   needs role L2
    L1 route   needs role L1 or L2
    auto       needs role analyst or above
Anything else appends a "denied" record and raises Forbidden.

Every record (approved or denied) is {id, case_id, action, route, role, approver, status, ts, note, exposure, reason,
prev_hash, hash}: approver is the first 12 hex of sha256(token) (the token itself is never stored), hash is the sha256 of
the canonical JSON of the record without "hash" (prev_hash included), and the first record's prev_hash is "0" * 64.
verify() re-walks the chain, so editing, inserting or reordering any line is detected.

This module deliberately has NO update and NO delete function: the log is append-only. A correction is a new record.
(Truncating the tail is the one change a local chain cannot see; mirroring each record to TigerGraph as an Approval
vertex, insert only, is the lead's integration step and gives an independent copy.)
"""
import hashlib
import hmac
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from kavach.config import DATA
from kavach.harness.faults import Forbidden

PATH = DATA / "approvals.jsonl"          # tests point KAVACH_APPROVALS_FILE (or this attribute) at a temp file
GENESIS = "0" * 64
ROLES = ("analyst", "L1", "L2")
RANK = {"analyst": 1, "L1": 2, "L2": 3}
NEEDS = {"auto": 1, "L1": 2, "L2": 3}
TOKEN_ENV = {"analyst": "KAVACH_TOKEN_ANALYST", "L1": "KAVACH_TOKEN_L1", "L2": "KAVACH_TOKEN_L2"}
MIN_TOKEN_LEN = 16

_lock = threading.Lock()


def path() -> Path:
    p = os.environ.get("KAVACH_APPROVALS_FILE", "").strip()
    return Path(p) if p else Path(PATH)


# ---------------------------------------------------------------- roles
def role_for(token: str | None) -> str | None:
    """Resolve a bearer token to a role from the environment. Constant-time comparison; tokens shorter than 16
    characters are not accepted as configured roles."""
    if not token:
        return None
    tok = token.strip().encode()
    found = None
    for role in ROLES:
        want = os.environ.get(TOKEN_ENV[role], "").strip()
        if len(want) >= MIN_TOKEN_LEN and hmac.compare_digest(tok, want.encode()):
            found = role
    return found


def approver_id(token: str | None) -> str:
    return hashlib.sha256((token or "").strip().encode()).hexdigest()[:12] if token else "anonymous"


def required_role(route: str) -> str:
    return {1: "analyst", 2: "L1", 3: "L2"}[NEEDS[route]]


def allowed(role: str | None, route: str) -> bool:
    return role is not None and RANK.get(role, 0) >= NEEDS[route]


# ---------------------------------------------------------------- chain
def canonical(rec: dict) -> str:
    return json.dumps({k: v for k, v in rec.items() if k != "hash"}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(rec: dict) -> str:
    return hashlib.sha256(canonical(rec).encode()).hexdigest()


@contextmanager
def _locked(p: Path):
    """One writer at a time: a thread lock plus an OS file lock (several CLI processes and the server may append)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        lf = open(str(p) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                import time
                for _ in range(200):
                    try:
                        lf.seek(0)
                        msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise TimeoutError(f"could not lock {p}")
            else:
                import fcntl
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    lf.seek(0)
                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lf.close()


def _lines(p: Path) -> list[str]:
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _last_hash(p: Path) -> str:
    lines = _lines(p)
    if not lines:
        return GENESIS
    return json.loads(lines[-1])["hash"]


def _append(rec: dict) -> dict:
    p = path()
    with _locked(p):
        rec["prev_hash"] = _last_hash(p)
        rec["hash"] = digest(rec)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return rec


# ---------------------------------------------------------------- public API
def approve(case_id: str, action: str, token: str | None, exposure: float = 0.0, note: str = "") -> dict:
    """Approve one recommended action for a case. The role comes from the token; the route from the action and the
    exposure. Returns the appended record; raises Forbidden (after appending a denied record) when the role is too low
    or the token is unknown; raises ValueError for an action that is not in the policy's action list."""
    from kavach.answer import ACTIONS, route_for

    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if not case_id or len(case_id) > 64:
        raise ValueError("invalid case id")
    exposure = float(exposure or 0.0)
    route = route_for(action, exposure)
    role = role_for(token)
    ok = allowed(role, route)
    reason = ("" if ok else
              f"unknown or unconfigured token; an {route} action needs role {required_role(route)}" if role is None else
              f"role {role} cannot approve an {route} action; it needs role {required_role(route)} or above")
    rec = {"id": str(uuid.uuid4()), "case_id": case_id, "action": action, "route": route, "role": role or "none",
           "approver": approver_id(token), "status": "approved" if ok else "denied",
           "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "note": (note or "")[:500],
           "exposure": round(exposure, 2), "reason": reason}
    rec = _append(rec)
    if not ok:
        raise Forbidden(f"{case_id} {action}: {reason} (denied record {rec['id']})")
    return rec


def records(case_id: str | None = None) -> list[dict]:
    out = [json.loads(ln) for ln in _lines(path())]
    return [r for r in out if case_id is None or r.get("case_id") == case_id]


def verify_detail() -> dict:
    """Walk the chain. {ok, records, first_bad (1-based line or None), reason}."""
    prev, n = GENESIS, 0
    for i, ln in enumerate(_lines(path()), 1):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            return {"ok": False, "records": n, "first_bad": i, "reason": "line is not valid JSON"}
        if rec.get("prev_hash") != prev:
            return {"ok": False, "records": n, "first_bad": i, "reason": "prev_hash does not match the previous record"}
        if digest(rec) != rec.get("hash"):
            return {"ok": False, "records": n, "first_bad": i, "reason": "record content does not match its hash"}
        prev, n = rec["hash"], n + 1
    return {"ok": True, "records": n, "first_bad": None, "reason": ""}


def verify() -> tuple[bool, int]:
    d = verify_detail()
    return d["ok"], d["records"]


def pending(case_id: str, actions: list[dict]) -> list[dict]:
    """The L1/L2 actions of a case (NBA dicts with action and route) that have no approved record yet."""
    done = {r["action"] for r in records(case_id) if r.get("status") == "approved"}
    out = []
    for a in actions or []:
        route = a.get("route", "auto")
        if route in ("L1", "L2") and a.get("action") not in done:
            out.append({**a, "needs_role": required_role(route)})
    return out
