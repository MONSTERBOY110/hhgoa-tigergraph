# CLAUDE.md — hhgoa-tigergraph (Kavach)

TigerGraph × Hacker House Goa 2026, **Task 4: Agentic Fraud Investigation**. This repo is the team's last route into the HH Goa finals.

**DEADLINE: Thu 24 Sep 2026, 11:59 PM IST. No resubmissions. Internal target: form submitted by 21:30 IST.**
Check the clock (`date`) at the start of every session and at every milestone. If behind schedule, apply the cut order in `docs/TRD.md` §13 instead of polishing.

## Read first
1. `docs/TASK-README.md` — organizer spec. **Source of truth** for policy, answer format, enums. Wins every conflict.
2. `docs/PRD.md` — goals, scoring model, requirements, timeline, form checklist.
3. `docs/TRD.md` — architecture, schema, queries, detectors, policy engine, validator.

## Non-negotiables (breaking any of these can zero a case or disqualify us)
- **Never** download, open, or use the public Kaggle / IEEE-CIS files. Only the organizer's Drive files. (Disqualification.)
- **Every ID** in `cases/*.json` must exist in the dataset. The LLM never writes IDs into structured fields. The validator checks every ID.
- Action ids, routes, statuses, verdicts, patterns, evidence sources, evidence-request types: **exact enum strings** from `docs/TASK-README.md`.
- Routes: `DECLINE_TRANSACTION` L1; `BLOCK_CARD` L1 if exposure ≤ 2500 else L2; `BLOCK_ALL_CARDS` L2; `FILE_REPORT` L2; everything else `auto`.
- `sar.file == ("FILE_REPORT" in final actions)`. When false: narrative `""`, subjects `[]`, total 0, dates `[]`.
- `legitimate` ⇒ `affected_txn_ids == []`, `first_suspicious_txn_id == ""`, `exposure_usd == 0`, `sar.file == false`.
- No evidence requested ⇒ `final == initial`, `what_changed == "nothing"`.
- Every NBA `reason` cites a rule (R1–R10 or §3a/§6). Never block on a single weak signal with p < 0.70 (R1). Never `BLOCK_ALL_CARDS` without R10.
- **No per-case hardcoding.** Detectors and thresholds are general; dossiers inform them. No `if case_id == ...` anywhere.
- **No copying** answer files, dossiers, or code from other teams' public repos. Architecture ideas are fine; answers come from our own analysis of the data.
- `tool_calls`, `tokens`, `latency_s` are real measured values. `written_to_graph: true` only if the vertex was actually upserted and re-read.
- Never commit `data/`, `.env`, keys, or the DuckDB file.

## Commands
```
python -m venv .venv ; .venv\Scripts\activate ; pip install -r requirements.txt
python -m kavach download            # organizer Drive files → data/raw/
python -m kavach index               # DuckDB lane A
python -m kavach graph all           # TigerGraph schema + load + install queries
python -m kavach weights             # likelihood ratios from closed cases
python -m kavach investigate HHG-006 --verbose [--backend duck|tg]
python -m kavach run --all [--backend tg]
python -m kavach check               # validator — must pass before every push of cases/
pytest -q
```

## Working rules
- Environment: Windows 11, PowerShell primary, Git Bash available. Use `python`, not `python3`. Paths with backslashes in PowerShell.
- Decisions are deterministic (tools + `policy.py`); the LLM only writes summary, SAR narrative, pattern_description, and optionally picks the next tool from a whitelist.
- Keep a template fallback for every LLM output so a run never fails on the LLM.
- Log TigerGraph pain points as they happen in `docs/tigergraph-feedback.md` (needed for the form).
- Commit small and often with clear messages; push to `origin main` at every milestone. **Safety-net push of 20 validator-passing files by 23 Sep 20:00 IST**, even from the DuckDB lane.
- Before claiming anything works, run it and show the output (validator table, test results, row counts).
- Long jobs (data load, full runs) go in the background with logs in `logs/`.
- When evidence in a case is genuinely balanced, `uncertain` + R1/R8 is a correct answer. Do not force a verdict.
- The README hints matter: half the cases are legitimate; high risk scores are mostly legitimate; some cases are only solvable by looking at *other* cards sharing a device/region/email; undocumented patterns are scored.

## Human-only tasks (ask the user; don't block other work while waiting)
Savanna signup, workspace host + GSQL secret + org ID; Groq API key; Devfolio team name and lead ID; making the repo public; recording the demo video; publishing the blog and social posts (tag @TigerGraphDB and @247pmstudio); submitting the form.
