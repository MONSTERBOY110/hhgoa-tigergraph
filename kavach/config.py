import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATA = ROOT / "data"
RAW = DATA / "raw"                 # Round 1 organizer files (July to December 2016)
RAW2 = RAW / "round2"              # Round 2 organizer files (January 2017, replaced closed cases, case pack, policy, grader)
# Default index: the Round 2 build (July 2016 to January 2017, both case packs). KAVACH_DUCK_FILE=kavach.duckdb selects
# the Round 1 build (July to December 2016) for regression runs on the HHG cases.
DUCK_PATH = DATA / env("KAVACH_DUCK_FILE", "kavach_r2.duckdb")
FEATURES = DATA / env("KAVACH_FEATURES_FILE", "features_r2.parquet")
CASES_DIR = ROOT / "cases"
LOGS = ROOT / "logs"
