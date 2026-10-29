"""Kavach HTTP API (FastAPI). Start with `python -m kavach serve` (or kavach.server.main()); binds 127.0.0.1:8787.

    POST /alerts                    body = one case-pack row; runs the investigation in a worker thread (harness.runner),
                                    returns {case_id, status: "running"}; poll GET /alerts/{case_id}
    GET  /alerts/{case_id}          job status: running | ok | incident (with the incident file) | error
    GET  /cases/{case_id}           handover view: the answer file if present, latest incident, casefile, approvals, pending
    GET  /cases/{case_id}/replay    the recorded trace (logs/traces/<id>.json), redacted
    POST /cases/{case_id}/approvals Authorization: Bearer <token>; body {action, note, exposure}. The role comes from the
                                    token only; a "role" field in the body is ignored, logged and written into the denied or
                                    approved record's note. 401 without a token, 403 when the role may not approve.
    GET  /approvals/verify          re-walks the approval hash chain
    GET  /health

What persists is the caller's choice: by default the server keeps results in memory and never writes cases/; pass
on_outcome to create_app (the CLI does) to persist an Outcome whose answer is not None. Everything returned or logged
goes through harness.redact.
"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from kavach.config import CASES_DIR, DATA, LOGS, env
from kavach.harness import approvals, runner
from kavach.harness.faults import Forbidden
from kavach.harness.redact import install_logging_filter, redact

log = logging.getLogger("kavach.server")
APPROVAL_FIELDS = ("action", "note", "exposure")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _casefile_view(case_id: str, data_dir: Path) -> dict:
    """Handover from kavach.casefile when that module exists (imported lazily), else a stub from the jsonl file."""
    try:
        from kavach import casefile
        for fn_name in ("handover", "view", "state"):
            fn = getattr(casefile, fn_name, None)
            if callable(fn):
                return {"exists": True, "source": f"kavach.casefile.{fn_name}", "view": fn(case_id)}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        return {"exists": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    p = data_dir / "casefiles" / f"{case_id}.jsonl"
    if not p.exists():
        return {"exists": False}
    events = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return {"exists": True, "source": "stub", "n_events": len(events), "last_events": events[-5:]}


def create_app(backend: str | None = None, on_outcome=None, cases_dir: Path | None = None, traces_dir: Path | None = None,
               data_dir: Path | None = None):
    from fastapi import Body, FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse

    install_logging_filter()
    backend = backend or env("TG_BACKEND", "duck") or "duck"
    cases_dir = Path(cases_dir or CASES_DIR)
    traces_dir = Path(traces_dir or (LOGS / "traces"))
    data_dir = Path(data_dir or DATA)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kavach-case")   # one investigation at a time (one DuckDB connection)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Kavach", version="2", description="Fraud investigation agent: alerts, handover, replay, approvals",
                  lifespan=lifespan)
    jobs: dict[str, dict] = {}
    lock = threading.Lock()
    app.state.jobs, app.state.pool, app.state.backend = jobs, pool, backend

    def check_id(case_id: str) -> str:
        if not runner.CASE_ID_RE.match(case_id or ""):
            raise HTTPException(422, "invalid case id")
        return case_id

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        resp = await call_next(request)
        log.info("%s %s -> %s", request.method, request.url.path, resp.status_code)   # never headers or bodies
        return resp

    def work(row: dict) -> None:
        cid = row["case_id"]
        try:
            o = runner.run_case(row, backend=backend)
            info = {"status": o.status, "finished_at": _now(), "incident": o.incident_path,
                    "incident_kind": (o.incident or {}).get("kind"), "rerun": o.rerun}
            if o.answer is not None:
                c = o.answer["case"]
                info.update({"verdict": c["verdict"], "fraud_probability": c["fraud_probability"], "pattern": c["pattern"],
                             "final_actions": [x["action"] for x in o.answer["next_best_actions"]["final"]]})
            if on_outcome is not None:
                on_outcome(o)
                info["persisted"] = o.answer is not None
        except Exception as e:  # noqa: BLE001  (run_case already fails closed; this guards the hook)
            info = {"status": "error", "finished_at": _now(), "error": f"{type(e).__name__}: {str(e)[:300]}"}
            log.error("alert %s failed outside the harness: %s", cid, info["error"])
        with lock:
            jobs[cid] = {**jobs.get(cid, {}), **info}

    @app.get("/health")
    def health():
        ok, n = approvals.verify()
        return {"status": "ok", "time": _now(), "backend": backend,
                "roles_configured": [r for r in approvals.ROLES if len(env(approvals.TOKEN_ENV[r])) >= approvals.MIN_TOKEN_LEN],
                "approvals_chain": {"ok": ok, "records": n},
                "jobs_running": sum(1 for j in jobs.values() if j.get("status") == "running")}

    @app.post("/alerts", status_code=202)
    def post_alert(row: dict = Body(...)):
        try:
            runner.validate_case_row(row)
        except runner.InvalidInput as e:
            raise HTTPException(422, str(e)) from None
        cid = str(row["case_id"])
        with lock:
            if jobs.get(cid, {}).get("status") == "running":
                return {"case_id": cid, "status": "running", "note": "already running"}
            jobs[cid] = {"status": "running", "submitted_at": _now()}
        pool.submit(work, dict(row))
        return {"case_id": cid, "status": "running"}

    @app.get("/alerts/{case_id}")
    def get_alert(case_id: str):
        check_id(case_id)
        if case_id not in jobs:
            raise HTTPException(404, "no alert submitted for this case in this server session")
        return redact({"case_id": case_id, **jobs[case_id]})

    def answer_for(case_id: str):
        return _read_json(cases_dir / f"{case_id}.json")

    @app.get("/cases/{case_id}")
    def get_case(case_id: str):
        check_id(case_id)
        ans = answer_for(case_id)
        incs = sorted(runner.incidents_dir().glob(f"{case_id}-*.json"))
        last_inc = _read_json(incs[-1]) if incs else None
        cf = _casefile_view(case_id, data_dir)
        recs = approvals.records(case_id)
        if ans is None and not incs and not cf.get("exists") and not recs and case_id not in jobs:
            raise HTTPException(404, "unknown case")
        final = (ans or {}).get("next_best_actions", {}).get("final", [])
        return redact({
            "case_id": case_id,
            "answer": ans,
            "state": ("incident" if ans is None and incs else (ans or {}).get("case", {}).get("status")),
            "incident": ({"path": str(incs[-1]), "kind": last_inc.get("kind"), "error": last_inc.get("error"), "ts": last_inc.get("ts")}
                         if last_inc else None),
            "n_incidents": len(incs),
            "casefile": cf,
            "approvals": recs,
            "pending_approvals": approvals.pending(case_id, final),
            "job": jobs.get(case_id),
        })

    @app.get("/cases/{case_id}/replay")
    def get_replay(case_id: str):
        check_id(case_id)
        tr = _read_json(traces_dir / f"{case_id}.json")
        if tr is None:
            raise HTTPException(404, "no trace recorded for this case")
        return redact({"case_id": case_id, "trace": tr})

    @app.post("/cases/{case_id}/approvals")
    def post_approval(case_id: str, body: dict = Body(...), authorization: str | None = Header(default=None)):
        check_id(case_id)
        token = None
        if authorization and authorization[:7].lower() == "bearer ":
            token = authorization[7:].strip() or None
        action = body.get("action")
        if not isinstance(action, str):
            raise HTTPException(422, "body.action is required")
        note = str(body.get("note") or "")[:400]
        ignored = sorted(k for k in body if k not in APPROVAL_FIELDS)
        if "role" in body:
            claimed = str(body.get("role"))[:20]
            log.warning("approval attempt on %s %s carried role=%r in the request body; ignored, the role comes from the token",
                        case_id, action, claimed)
            note = (note + " " if note else "") + f"[request body claimed role {claimed!r}; ignored, role taken from the token]"
        try:
            exposure = float(body.get("exposure") or 0.0)
        except (TypeError, ValueError):
            raise HTTPException(422, "body.exposure must be a number") from None
        ans = answer_for(case_id)
        if ans is not None:   # the server's own exposure wins if larger: a caller cannot lower a route by understating it
            exposure = max(exposure, float(ans.get("case", {}).get("exposure_usd") or 0.0))
        try:
            rec = approvals.approve(case_id, action, token, exposure=exposure, note=note)
        except Forbidden as e:
            log.warning("approval denied: %s", e)
            code = 401 if token is None else 403
            return JSONResponse(status_code=code, content=redact({"status": "denied", "detail": str(e), "ignored_fields": ignored}))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        final = (ans or {}).get("next_best_actions", {}).get("final", [])
        return redact({"status": "approved", "record": rec, "ignored_fields": ignored,
                       "pending_approvals": approvals.pending(case_id, final)})

    @app.get("/approvals/verify")
    def get_verify():
        return approvals.verify_detail()

    return app


def main(port: int = 8787, host: str = "127.0.0.1") -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    install_logging_filter()
    missing = [r for r in approvals.ROLES if len(env(approvals.TOKEN_ENV[r])) < approvals.MIN_TOKEN_LEN]
    if missing:
        log.warning("no token configured for roles %s (set KAVACH_TOKEN_ANALYST / _L1 / _L2 in .env); those roles cannot approve", missing)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
