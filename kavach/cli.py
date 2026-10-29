import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main():
    """Kavach fraud investigation agent."""


@app.command()
def download(force: bool = False, round: int = typer.Option(2, "--round", help="1: data/raw/, 2: data/raw/round2/")):
    """Download organizer Drive files into data/raw/ (round 1) or data/raw/round2/ (round 2)."""
    from kavach.data.download import download as dl

    dl(force, round)


@app.command()
def index(out: str = typer.Option("", "--out", help="target DuckDB file (default: kavach.config.DUCK_PATH)")):
    """Build the DuckDB index (lane A) over July 2016 to January 2017.

    Replacing an existing index with changed organizer files is a schema-drift event: it needs a recorded approval
    (`kavach drift detect`, then `kavach drift approve --token <role token>`). A first build on a clean clone has nothing
    to drift from and proceeds."""
    from pathlib import Path

    from kavach.config import DUCK_PATH
    from kavach.data.duck import build
    target = Path(out) if out else DUCK_PATH
    if target.exists():
        from kavach import drift as drift_mod
        st = drift_mod.status()
        if not st["approved"]:
            print(f"refusing to rebuild {target.name} over changed inputs: {st['reason']}")
            print("run `python -m kavach drift detect`, review docs/round2/drift-report.md, then "
                  "`python -m kavach drift approve --token <KAVACH_TOKEN_...>`")
            raise typer.Exit(2)
    build(out or None)


@app.command()
def check(cases_dir: str = "cases", no_ids: bool = False,
          round: int = typer.Option(2, "--round", help="which case pack the files answer (1: HHG, 2: HHF)"),
          grader: bool = typer.Option(False, "--grader", help="also run the organizer grader (grader/grade.py) exactly as the judges do")):
    """Validate answer files (schema, enums, ids, policy, grader scope) and optionally run the organizer grader."""
    from pathlib import Path

    from kavach.config import ROOT
    from kavach.validate import check as run_check

    d = Path(cases_dir)
    if not d.is_absolute():
        d = ROOT / d
    raise typer.Exit(0 if run_check(d, strict_ids=not no_ids, round=round, grader=grader) else 1)


@app.command()
def graph(step: str = typer.Argument("all", help="schema|load|queries|counts|all")):
    """Set up TigerGraph: schema, loading job + load, installed queries, count check."""
    from kavach.graph.setup import run

    run(step)


@app.command()
def weights():
    """Fit per-signal likelihood ratios from the closed cases."""
    from kavach.features import build
    from kavach.weights import fit

    build()
    for prod, sig in fit().items():
        for k, v in sig.items():
            print(f"{prod} {k:24s} LR_pop {v['LR_pop']:7.2f}  LR_hi {v['LR_hi']:7.2f}")


def _cases(ids, round: int | None = None):
    from kavach import duckq
    rows = duckq.case_pack(round).to_dict("records")
    rows = [r for r in rows if not ids or r["case_id"] in ids]
    if ids and not rows:
        # not in the default pack: look in the other round's pack before giving up
        rows = [r for r in duckq.case_pack(1).to_dict("records") if r["case_id"] in ids]
    if ids and not rows:
        print(f"unknown case id(s): {', '.join(ids)}")
        raise typer.Exit(1)
    return rows


def _graph_conn(backend: str, persist: bool):
    """Fail closed: when the run needs the graph (tg/mcp lane or --persist) and it cannot be reached, stop with a clear
    error instead of carrying on and reporting written_to_graph=false as if nothing happened."""
    from kavach.harness.faults import GraphDown
    from kavach.harness.runner import connect_graph
    try:
        return connect_graph(backend, persist)
    except GraphDown as e:
        print(f"INCIDENT graph unavailable: {e}")
        raise typer.Exit(2)


def _write(ans: dict, trace: dict) -> None:
    """Write the answer, its trace, its attestation sidecar and the casefile events. Called only for complete answers."""
    import json
    from kavach import attest, casefile
    from kavach.config import CASES_DIR, LOGS
    CASES_DIR.mkdir(exist_ok=True)
    (LOGS / "traces").mkdir(parents=True, exist_ok=True)
    side = trace.pop("attestation", None)
    (CASES_DIR / f"{ans['case_id']}.json").write_text(json.dumps(ans, indent=2, ensure_ascii=False), encoding="utf-8")
    (LOGS / "traces" / f"{ans['case_id']}.json").write_text(json.dumps(trace, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    if side:
        attest.write(ans["case_id"], side)
    casefile.record_answer(ans, trace)
    if ans["case"]["written_to_graph"]:
        casefile.append(ans["case_id"], "written_to_graph", ans["case"]["graph_case_id"],
                        extra={"graph_case_id": ans["case"]["graph_case_id"]})


def _line(ans: dict, rerun=None) -> str:
    k = ans["case"]
    tail = ""
    if rerun is not None:
        tail = " rerun:identical" if rerun.get("identical") else f" rerun:changed {rerun.get('changed')}"
    return (f"{ans['case_id']}: {k['verdict']:10s} p={k['fraud_probability']:.2f} {k['pattern']:28s} exp={k['exposure_usd']:>9.2f} "
            f"sar={str(ans['sar']['file']):5s} graph={k['written_to_graph']} calls={ans['tool_calls']} tok={ans['tokens']} "
            f"{ans['latency_s']}s{tail}")


@app.command()
def investigate(case_id: str, backend: str = "duck", verbose: bool = False, persist: bool = False, no_llm: bool = False,
                no_write: bool = typer.Option(False, "--no-write", help="print only; leave cases/<id>.json untouched")):
    """Investigate one case inside the harness and write cases/<case_id>.json (exit 2 and an incident if it fails closed)."""
    import json, os
    if no_llm:
        os.environ["KAVACH_NO_LLM"] = "1"
    os.environ["KAVACH_RECORD"] = "0" if no_write else "1"
    from kavach.harness.runner import run_case
    conn = _graph_conn(backend, persist)
    for c in _cases([case_id]):
        o = run_case(c, backend=backend, graph_conn=conn, verbose=verbose, persist=persist)
        if o.answer is None:
            raise typer.Exit(2)
        if not no_write:
            _write(o.answer, o.trace)
        if verbose:
            print(json.dumps(o.answer, indent=2, ensure_ascii=False))
        print(_line(o.answer, o.rerun))


@app.command()
def run(all: bool = typer.Option(True, "--all"), backend: str = "duck", persist: bool = False, no_llm: bool = False,
        round: int = typer.Option(2, "--round", help="case pack: 2 (HHF, default) or 1 (HHG)")):
    """Investigate every case in the case pack inside the harness and write cases/*.json; exit 2 if any case failed closed."""
    import os
    if no_llm:
        os.environ["KAVACH_NO_LLM"] = "1"
    os.environ["KAVACH_RECORD"] = "1"
    from kavach.harness.runner import run_case
    from kavach.llm import Llm
    conn = _graph_conn(backend, persist)
    llm = Llm()
    failed = []
    for c in _cases([], round):
        o = run_case(c, backend=backend, llm=llm, graph_conn=conn, persist=persist)
        if o.answer is None:
            failed.append(c["case_id"])
            continue
        _write(o.answer, o.trace)
        print(_line(o.answer, o.rerun), flush=True)
    if failed:
        print(f"{len(failed)} case(s) failed closed (see incidents/): {', '.join(failed)}")
        raise typer.Exit(2)


@app.command()
def serve(port: int = 8787, host: str = "127.0.0.1"):
    """Approvals and case API (role tokens from .env; the role comes from the token, never from the request body)."""
    from kavach.server import main as serve_main
    serve_main(port=port, host=host)


@app.command()
def drift(action: str = typer.Argument("detect", help="detect | status | approve"), token: str = typer.Option("", "--token"),
          token_env: str = typer.Option("", "--token-env", help="name of an environment variable holding the approver token")):
    """Schema drift: detect and explain what changed in the organizer files, propose the re-mapping, approve it."""
    import os
    import sys
    from kavach.drift import main as drift_main
    token = token or (os.environ.get(token_env, "") if token_env else "")
    sys.exit(drift_main([action] + (["--token", token] if token else [])) or 0)


PUBLISHED = ("RESILIENCE.md", "INNOVATION.md", "ablation.md", "backtests.md", "attacks.md", "drift-report.md", "data-quality.md",
             "january-data-profile.md", "round1-postmortem.md")


@app.command()
def publish():
    """Copy the judge-facing Round 2 documents from docs/round2/ (internal, not in git) into round2/ (tracked)."""
    import shutil

    from kavach.config import ROOT
    src, dst = ROOT / "docs" / "round2", ROOT / "round2"
    dst.mkdir(exist_ok=True)
    for name in PUBLISHED:
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
            print(f"published round2/{name}")
        else:
            print(f"missing docs/round2/{name}")


@app.command()
def dq():
    """Data-quality report (docs/round2/data-quality.md) over the current index."""
    import sys
    from kavach.dq import main as dq_main
    sys.exit(dq_main([]) or 0)


@app.command()
def monitor(limit: int = 6, backend: str = "duck", no_llm: bool = False):
    """Autonomous sweep of Nov-Dec for activity no alert pointed at; writes monitor/MON-xxx.json."""
    import os
    if no_llm:
        os.environ["KAVACH_NO_LLM"] = "1"
    from kavach.monitor import run as mon

    mon(limit, backend)


@app.command()
def attest(case_id: str = typer.Argument("", help="case id, or empty with --all"), all_: bool = typer.Option(False, "--all"),
           backend: str = "duck"):
    """Re-run every attested claim of a case (what, evidence ids, how, when) and verify the ids come back."""
    import sys

    from kavach.attest import main as attest_main
    argv = (["--all"] if all_ or not case_id else [case_id]) + ["--backend", backend]
    sys.exit(attest_main(argv) or 0)


@app.command()
def replay(case_id: str, json_out: bool = typer.Option(False, "--json")):
    """Audit replay: what the agent saw at each step, which tool it called, what came back, and why p moved."""
    import sys

    from kavach.audit import main as audit_main
    sys.exit(audit_main([case_id] + (["--json"] if json_out else [])) or 0)


@app.command()
def case(case_id: str, json_out: bool = typer.Option(False, "--json")):
    """Case handover view: state, timeline, what changed, what it waits on, who decided what."""
    import sys

    from kavach.casefile import main as casefile_main
    sys.exit(casefile_main([case_id] + (["--json"] if json_out else [])) or 0)


@app.command()
def vectors():
    """Embed closed-case notes and policy/pattern sections (384-d) into data/vectors.npz."""
    from kavach.vectors import build

    build()


@app.command()
def report():
    """Rewrite the README results table from cases/*.json."""
    from kavach.report import update_readme

    update_readme()


@app.command()
def site():
    """Build site/cases.js for the static case viewer from cases/*.json and monitor/*.json."""
    from kavach.site import build

    build()


from kavach.grow.cli import app as grow_app  # noqa: E402

app.add_typer(grow_app, name="grow", help="Self-growing detective: mine, propose, approve (L2), ablate.")

if __name__ == "__main__":
    app()
