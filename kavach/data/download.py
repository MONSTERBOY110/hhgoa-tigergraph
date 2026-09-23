"""Download the organizer's Drive files (never the Kaggle originals)."""
import gdown

from kavach.config import RAW

FILES = {
    "README.md": "1-a1N26_O_wmvf2gtAuTP00jf124vbqhC",
    "case_pack.csv": "11GAxXOPWCxrB1EfePMHB9IquGJ9rDIod",
    "closed_cases_history.csv": "1S05ULujpOwSlv_YSrcDbVJcyS3JpTOZT",
    "identity.csv": "1zsMMY7lnnjZWsubsO25D9n2ZiZHSU8J_",
    "transactions.csv": "1svn7YqgPlJ-Iv3A8ar1Lh91eVWp6sukR",
}


def download(force: bool = False) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for name, fid in FILES.items():
        out = RAW / name
        if out.exists() and out.stat().st_size > 0 and not force:
            print(f"skip {name} ({out.stat().st_size:,} bytes)")
            continue
        gdown.download(id=fid, output=str(out), quiet=False)
        size = out.stat().st_size if out.exists() else 0
        if size == 0:
            raise SystemExit(f"download failed or empty: {name}")
        head = out.read_bytes()[:200].lower()
        if b"<html" in head:
            raise SystemExit(f"{name}: got an HTML page (Drive quota?), retry or download in browser")
        print(f"ok {name} {size:,} bytes")
