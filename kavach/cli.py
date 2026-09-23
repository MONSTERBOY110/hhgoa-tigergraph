import typer

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main():
    """Kavach fraud investigation agent."""


@app.command()
def download(force: bool = False):
    """Download organizer Drive files into data/raw/."""
    from kavach.data.download import download as dl

    dl(force)


@app.command()
def index():
    """Build the DuckDB index (lane A)."""
    from kavach.data.duck import build

    build()


@app.command()
def check(cases_dir: str = "cases", no_ids: bool = False):
    """Validate answer files (schema, enums, ids, policy)."""
    from pathlib import Path

    from kavach.config import ROOT
    from kavach.validate import check as run_check

    d = Path(cases_dir)
    if not d.is_absolute():
        d = ROOT / d
    raise typer.Exit(0 if run_check(d, strict_ids=not no_ids) else 1)


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


def _cases(ids):
    from kavach import duckq
    rows = duckq.case_pack().to_dict("records")
    return [r for r in rows if not ids or r["case_id"] in ids]


def _graph_conn(backend: str, persist: bool):
    if backend != "tg" and not persist:
        return None
    try:
        from kavach.graph.client import connect
        return connect()
    except Exception as e:
        print(f"TigerGraph not reachable ({e}); answers will say written_to_graph=false")
        return None


def _write(ans: dict, trace: dict) -> None:
    import json
    from kavach.config import CASES_DIR, LOGS
    CASES_DIR.mkdir(exist_ok=True)
    (LOGS / "traces").mkdir(parents=True, exist_ok=True)
    (CASES_DIR / f"{ans['case_id']}.json").write_text(json.dumps(ans, indent=2, ensure_ascii=False), encoding="utf-8")
    (LOGS / "traces" / f"{ans['case_id']}.json").write_text(json.dumps(trace, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


@app.command()
def investigate(case_id: str, backend: str = "duck", verbose: bool = False, persist: bool = False, no_llm: bool = False,
                no_write: bool = typer.Option(False, "--no-write", help="print only; leave cases/<id>.json untouched")):
    """Investigate one case and write cases/<case_id>.json."""
    import json, os
    if no_llm:
        os.environ["KAVACH_NO_LLM"] = "1"
    from kavach.agent import investigate as inv
    conn = _graph_conn(backend, persist)
    for c in _cases([case_id]):
        ans, trace = inv(c, backend=backend, graph_conn=conn, verbose=verbose)
        if not no_write:
            _write(ans, trace)
        if verbose:
            print(json.dumps(ans, indent=2, ensure_ascii=False))
        print(f"{ans['case_id']}: {ans['case']['verdict']} p={ans['case']['fraud_probability']} {ans['case']['pattern']} "
              f"exposure={ans['case']['exposure_usd']} sar={ans['sar']['file']} calls={ans['tool_calls']} tokens={ans['tokens']} {ans['latency_s']}s")


@app.command()
def run(all: bool = typer.Option(True, "--all"), backend: str = "duck", persist: bool = False, no_llm: bool = False):
    """Investigate every case in the case pack and write cases/*.json."""
    import os
    if no_llm:
        os.environ["KAVACH_NO_LLM"] = "1"
    from kavach.agent import investigate as inv
    from kavach.llm import Llm
    conn = _graph_conn(backend, persist)
    llm = Llm()
    for c in _cases([]):
        ans, trace = inv(c, backend=backend, llm=llm, graph_conn=conn)
        _write(ans, trace)
        k = ans["case"]
        print(f"{ans['case_id']}: {k['verdict']:10s} p={k['fraud_probability']:.2f} {k['pattern']:28s} exp={k['exposure_usd']:>9.2f} "
              f"sar={str(ans['sar']['file']):5s} graph={k['written_to_graph']} calls={ans['tool_calls']} tok={ans['tokens']} {ans['latency_s']}s",
              flush=True)


@app.command()
def monitor(limit: int = 6, backend: str = "duck", no_llm: bool = False):
    """Autonomous sweep of Nov-Dec for activity no alert pointed at; writes monitor/MON-xxx.json."""
    import os
    if no_llm:
        os.environ["KAVACH_NO_LLM"] = "1"
    from kavach.monitor import run as mon

    mon(limit, backend)


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


if __name__ == "__main__":
    app()
