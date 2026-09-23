import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
RAW = DATA / "raw"
DUCK_PATH = DATA / "kavach.duckdb"
CASES_DIR = ROOT / "cases"
LOGS = ROOT / "logs"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
