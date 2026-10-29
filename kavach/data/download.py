"""Download the organizer's Drive files (never the Kaggle originals).

Round 1 files go to data/raw/, Round 2 files to data/raw/round2/ (with the cases/, policy/ and grader/
subfolders as delivered). gdown 6.x: no --fuzzy; large files may need a retry (see STATUS.md).
"""
import gdown

from kavach.config import RAW, RAW2

FILES = {
    "README.md": "1-a1N26_O_wmvf2gtAuTP00jf124vbqhC",
    "case_pack.csv": "11GAxXOPWCxrB1EfePMHB9IquGJ9rDIod",
    "closed_cases_history.csv": "1S05ULujpOwSlv_YSrcDbVJcyS3JpTOZT",
    "identity.csv": "1zsMMY7lnnjZWsubsO25D9n2ZiZHSU8J_",
    "transactions.csv": "1svn7YqgPlJ-Iv3A8ar1Lh91eVWp6sukR",
}

# Final-round folder: https://drive.google.com/drive/folders/1qmpJfwnAV9LJh_LXxNG4PTj_INcQRKCA
FILES2 = {
    "transactions_2017q1.csv": "1QKMqg3WYLIILD45JPRlZKKGFuPXP46bl",
    "identity_2017q1.csv": "1-oJcq1Yrg8VrCKQDpUN73hH8W2HgEi4P",
    "closed_cases_history.csv": "10opaHcOFOUgvFIBxi0SXNFpC49D4lnjS",
    "cases/case_pack_final.csv": "1V79jW-g9Qp-EgXX00bRTM_fml5qhmCSi",
    "cases/answer_format.md": "1H9mEM9yR-taKC94f6_sOGa1juIVIPd3H",
    "policy/fraud_policy.md": "1aXXicV6oCfZFy7vTnelM5SYHi5rjCAN9",
    "grader/grade.py": "1DzDYXmgBAvOr0_bH11EZGfuNHfY6MnXC",
}


def download(force: bool = False, round: int = 2) -> None:
    root, files = (RAW, FILES) if round == 1 else (RAW2, FILES2)
    root.mkdir(parents=True, exist_ok=True)
    for name, fid in files.items():
        out = root / name
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.stat().st_size > 0 and not force:
            print(f"skip {name} ({out.stat().st_size:,} bytes)")
            continue
        for attempt in range(3):
            gdown.download(id=fid, output=str(out), quiet=False, resume=True)
            size = out.stat().st_size if out.exists() else 0
            head = out.read_bytes()[:200].lower() if size else b""
            if size and b"<html" not in head:
                break
            print(f"  {name}: {'empty' if not size else 'got an HTML page'} on attempt {attempt + 1}, retrying")
            out.unlink(missing_ok=True)
        else:
            raise SystemExit(f"download failed: {name} (Drive quota or a changed id); download it in the browser into {out}")
        print(f"ok {name} {out.stat().st_size:,} bytes")
