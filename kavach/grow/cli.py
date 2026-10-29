"""`python -m kavach.grow mine | propose | approve <name> --token X | reject <name> --token X | list | ablate | parity <name>`.

The lead mounts this app as `kavach grow` in kavach/cli.py (app.add_typer(grow_app, name="grow")).
"""
import json
from pathlib import Path

import typer

from kavach.config import LOGS

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Self-growing detective: mine, propose, approve, ablate.")
MINED = LOGS / "grow_mine.json"


@app.command()
def mine(duck: str = typer.Option("", help="index file (default: kavach.config.DUCK_PATH)"),
         features: str = typer.Option("", help="features parquet (default: kavach.config.FEATURES)"),
         min_support: int = 15, min_lift: float = 3.0, max_cleared: int = 3, sample: int = 100000, seed: int = 42,
         rare_support: int = 5, alpha: float = 0.01, out: str = str(MINED)):
    """Search every family's grid over the closed cases; print candidates, the analyst-note check and the forward test."""
    from kavach.grow import miner
    cands = miner.mine(duck or None, features or None, min_support, min_lift, max_cleared, sample, seed,
                       rare_support=rare_support, alpha=alpha)
    print(miner.table(cands))
    seeds = miner.seed_coverage(miner.seed_from_notes(), cands)
    print("\nanalyst notes (undocumented) -> families, and what the blind miner found on those cases:")
    for s in seeds:
        fams = ", ".join(f"{f['family']} ({'; '.join(f['keywords'])})" for f in s["families"]) or "no keyword"
        print(f"  cluster {s['cluster']}: {s['n']} notes {s['case_ids']}\n    '{s['exemplar'][:150]}'\n    -> {fams}")
        for b in s["best_grid_points"]:
            print(f"    best grid point {b['grid_point']}: covers {b['covers']}/{b['of']} (support {b['support']}, cleared {b['cleared_hits']}, lift {b['lift']})")
        for a in s["accepted_candidates"]:
            print(f"    accepted candidate {a['candidate']} covers {a['covers']}")
    path = miner.save(cands, out, seeds)
    print(f"\n{len(cands)} candidates saved to {path} ({miner.LAST['seconds']} s)")


@app.command()
def propose(src: str = str(MINED), include_rediscoveries: bool = False):
    """Write detectors/proposed/<name>.json for each mined candidate (novel ones unless --include-rediscoveries)."""
    from kavach.grow import miner, registry
    doc = json.loads(Path(src).read_text(encoding="utf-8"))
    cands = miner.load(src)
    for p in registry.propose(cands, include_rediscoveries=include_rediscoveries, duck_path=doc["settings"].get("duck_path") or None):
        s = json.loads(p.read_text(encoding="utf-8"))
        print(f"proposed {s['name']} ({s['description_source']}): {s['description']}")


def _token(token: str, token_env: str) -> str:
    import os

    from kavach import config  # noqa: F401  (loads .env)
    t = token or (os.environ.get(token_env, "") if token_env else "")
    if not t:
        print("no token given: pass --token or --token-env <VAR>")
        raise typer.Exit(3)
    return t


@app.command()
def approve(name: str, token: str = typer.Option("", help="approver token; only an L2 token passes"),
            token_env: str = typer.Option("", "--token-env", help="name of an environment variable holding the token (keeps it out of shell history)"),
            note: str = ""):
    """Approve a proposed detector (L2 only, recorded in the approvals hash chain)."""
    from kavach.grow import registry
    token = _token(token, token_env)
    try:
        rec = registry.approve(name, token, note)
    except Exception as e:
        print(f"refused: {type(e).__name__}: {e}")
        raise typer.Exit(3)
    print(json.dumps(rec, indent=1))


@app.command()
def reject(name: str, token: str = typer.Option(""), reason: str = typer.Option(...),
           token_env: str = typer.Option("", "--token-env")):
    """Reject a proposed detector (analyst or above; recorded)."""
    from kavach.grow import registry
    token = _token(token, token_env)
    try:
        rec = registry.reject(name, token, reason)
    except Exception as e:
        print(f"refused: {type(e).__name__}: {e}")
        raise typer.Exit(3)
    print(json.dumps(rec, indent=1))


@app.command("list")
def list_():
    """Proposed, approved and rejected detectors; approved ones that fail the load gate are named."""
    from kavach.grow import registry
    for kind, items in registry.listing().items():
        print(f"{kind}: {len(items)}")
        for i in items:
            print(f"  {i['name']:45s} {i['family']:16s} support {i['support']} cleared {i['cleared_hits']} lift {i['lift']} approval {i['approval']}")
    print(f"loadable approved: {[s['name'] for s in registry.load_approved()]}")


@app.command()
def ablate(cases: list[str] = typer.Argument(None), backend: str = "duck", round: int = typer.Option(None, "--round"),
           out: str = ""):
    """Run the pack with KAVACH_GROWN=1 and =0 (no LLM, no answer files) and write docs/round2/ablation.md."""
    from kavach.grow.ablation import ablate as run
    rep = run(cases or None, backend, round, out or None)
    for r in rep["cases"]:
        print(f"{r['case_id']}: grown {[g['name'] for g in r['with'].get('grown', [])]} changed {r['changed'] or 'nothing'}")
    print(f"wired={rep['wired']} approved={rep['approved']}")


@app.command()
def parity(name: str, kind: str = "proposed", n: int = 25):
    """Check the runtime detector against the SQL backtest on its fraud hits plus a random closed-case sample."""
    import duckdb
    from kavach.grow import miner, registry, templates
    spec = registry.read(name, kind)
    con = duckdb.connect()
    templates.prepare(con)
    lab = miner._labels(con)
    con.close()
    ids = lab[lab.case_id.isin(spec["backtest"]["fraud_case_ids"])].TransactionID.tolist() + \
        lab.sample(min(n, len(lab)), random_state=1).TransactionID.tolist()
    print(miner.parity_check(spec, ids))


@app.command()
def preview(name: str, kind: str = "proposed", round: int = typer.Option(None, "--round"), backend: str = "duck"):
    """Show which open cases a proposed detector would fire on (for the L2 reviewer, before approval)."""
    from kavach.grow.ablation import preview as run
    for r in run(name, kind, round, backend):
        if r.get("error"):
            print(f"{r['case_id']}: error {r['error']}")
        elif r["fired"]:
            print(f"{r['case_id']}: FIRES ({r['group']}, llr {r['llr']}) {r['claim']}\n    ids {r['entity_ids'][:8]} how {r['how']}")
        else:
            print(f"{r['case_id']}: no")
