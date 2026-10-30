"""The real kavach CLI (kavach.cli.app), with every file it writes redirected to a scratch folder.

Usage: python logs/drills/kv_scratch.py <scratch_dir> <kavach args...>
   e.g. python logs/drills/kv_scratch.py C:/tmp/drill investigate HHF-003 --no-llm

Redirected: cases/ (answers and the rerun comparison), logs/traces/, attestations/, data/casefiles/, incidents/,
data/approvals.jsonl. Reads (the DuckDB index, the TigerGraph workspace, .env) are the real ones. A --persist run
still writes the real graph vertex (that is the write-back being drilled)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # the repo root, so `kavach` imports
scratch = Path(sys.argv[1]).resolve()
for sub in ("cases", "logs/traces", "attestations", "casefiles", "incidents"):
    (scratch / sub).mkdir(parents=True, exist_ok=True)
os.environ["KAVACH_CASEFILE_DIR"] = str(scratch / "casefiles")
os.environ["KAVACH_ATTEST_DIR"] = str(scratch / "attestations")
os.environ["KAVACH_INCIDENTS_DIR"] = str(scratch / "incidents")
os.environ["KAVACH_APPROVALS_FILE"] = str(scratch / "approvals.jsonl")

import kavach.config as cfg  # noqa: E402

cfg.CASES_DIR = scratch / "cases"
cfg.LOGS = scratch / "logs"
import kavach.harness.runner as runner  # noqa: E402

runner.CASES_DIR = cfg.CASES_DIR
from kavach.cli import app  # noqa: E402

sys.argv = ["kavach"] + sys.argv[2:]
app(prog_name="kavach")
