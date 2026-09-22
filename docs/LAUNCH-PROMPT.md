# LAUNCH PROMPT — paste everything below the line into a new Claude Code session opened in `D:\Projects\hhgoa-tigergraph`

---

You are the build agent for **Kavach**, our submission to **TigerGraph × Hacker House Goa 2026, Task 4: Agentic Fraud Investigation**. This is our team's last chance to reach the HH Goa finals. The research and planning are done. Your job is to build, verify, and ship.

**Hard deadline: Thursday 24 Sep 2026, 11:59 PM IST. No resubmissions. Our internal target is the form submitted by 21:30 IST.** Run `date` now and keep checking it at every milestone.

## Step 0 — Set up the repo (first 10 minutes)
The repo `D:\Projects\hhgoa-tigergraph` is an empty git repo (branch `main`, remote `origin = https://github.com/MONSTERBOY110/hhgoa-tigergraph.git`, no commits yet).
1. Copy the handoff pack from `D:\Downloads\hhgoa-tigergraph\` into the repo:
   - `CLAUDE.md` → repo root
   - `PRD.md`, `TRD.md`, `TASK-README.md`, `LAUNCH-PROMPT.md` → `docs/`
2. Read `CLAUDE.md`, then `docs/TASK-README.md` top to bottom (it is the organizer spec and wins every conflict), then `docs/PRD.md` and `docs/TRD.md`.
3. Create `.gitignore` (`data/`, `.env`, `*.duckdb`, `.venv/`, `__pycache__/`, `logs/`), `.env.example` (TRD §2), `requirements.txt`, the `kavach/` package skeleton, and a stub `README.md`. First commit and push to `origin main`.

## Step 1 — Ask the user for these right away, then keep working while you wait
Put this checklist in one message and continue with Step 2 immediately:
- [ ] Sign up at https://savanna.tgcloud.io, create a free workspace (TigerGraph ≥ 4.2), and share the **host URL** and a **GSQL secret** (or username/password). Also note the **Savanna org ID** (organization settings) for the form.
- [ ] A **Groq API key** (https://console.groq.com). If Groq is unavailable, give Azure OpenAI or Gemini credentials instead.
- [ ] **Team name exactly as on Devfolio**, team lead's **Devfolio ID**, team size, and each member's name/email/phone for the form.
- [ ] Put the secrets in `.env` yourself (never paste them into committed files).

## Step 2 — Execute the phases (details in docs/TRD.md)
Work phase by phase. At each **Gate**, run the check, show its output, commit, and push.

**Phase A — Data + validator (target done by 23 Sep 03:00 IST)**
- `kavach download` (Drive ids in TRD §3; never the Kaggle originals), `kavach index` (DuckDB lane, TRD §4).
- First, establish the **card_id mapping** (how `C01234-K1` maps to transaction rows) and the **device_key** format. Assert ≥99% agreement with closed-case card ids.
- Build `answer.py` (pydantic schema) and `validate.py`. **Gate A:** the README example validates structurally; row counts match the README (590,742 txns / 144,432 identity / 5,565 closed / 20 cases).

**Phase B — Hand investigation of all 20 cases (target 23 Sep 10:00)**
- For each case, investigate with SQL like a human analyst (README step 5): card history, burst window, device, region, email, other cards on the same device/region, holder links, closed cases touching the entities, recurring charges. Write `docs/dossiers/HHG-xxx.md` with: facts found (with IDs), provisional verdict, pattern, episode txns, connected cards/devices, exposure, applicable rules, what evidence request you would make and the assumed reply, confidence.
- Parallelize with subagents if helpful (e.g., 4 agents × 5 cases), each writing dossiers only. Review all dossiers yourself for consistency.
- Keep a running tally: roughly half should come out legitimate (README hint). If far off, re-examine.
- In parallel (background), once Savanna credentials arrive: schema + slim loader (TRD §5.2–5.3).
- **Gate B:** 20 dossiers committed; a summary table in `docs/dossiers/README.md`.

**Phase C — Agent on the DuckDB lane (target 23 Sep 20:00)**
- Detectors (TRD §7, general, never keyed on case ids), `weights.py` from closed cases (TRD §8), `assess.py`, `episode.py`, `policy.py` (TRD §10, with unit tests for every rule and route), `simulate.py` (TRD §11), `recall.py` (fingerprint similarity for now), template narratives, `agent.py` state machine, `run --all`.
- Compare agent output with each dossier. Where they disagree, decide which is right from the data, then fix the general logic (not the case).
- **Gate C — SAFETY NET:** `pytest -q` green, `python -m kavach run --all --backend duck` and `python -m kavach check` pass on all 20 files. Commit `cases/` and push by **23 Sep 20:00 IST**. From here on we always have a valid submission on GitHub.

**Phase D — TigerGraph as system of record (target 24 Sep 10:00)**
- Finish load; verify counts against DuckDB. Install the GSQL queries (TRD §5.4). Connect TigerGraph MCP (timebox 1 h; fall back to pyTigerGraph behind the same interface).
- Vectors: embed closed-case narratives, policy/pattern text, and a few FinCEN SAR-guidance excerpts into TigerVector; `similar_cases` via `vectorSearch` (fallback: local vectors + graph edges).
- `persist.py` write-back of `InvestigationCase` + edges + embedding, then re-retrieve it → `written_to_graph: true`.
- LLM narratives via Groq (`openai/gpt-oss-120b`) with the id post-check and template fallback (TRD §12).
- Duck↔TG parity test on 3 cases. Rerun all 20 on `--backend tg`.
- **Gate D:** all 20 files `written_to_graph: true`, validator passes, parity test passes. Push.

**Phase E — Review and freeze (24 Sep 10:00–12:00)**
- Read every answer file next to its dossier: verdict, pattern, first txn, episode, exposure, connected cards/devices, actions and order, routes, case-vs-report, SAR narrative (standalone, 6–12 sentences, who/what/when/where/how/why), stop_reason. Fix logic, rerun, re-check. Freeze answers v1 and tag `v1-answers`.

**Phase F — Package (24 Sep 12:00–20:00)**
- Stretch in this order, only if time allows: `monitor/` (TRD §14), static case viewer in `site/` on GitHub Pages (Live UI URL).
- `README.md` per TRD §16, with the results table and exact run steps. Test the steps from a fresh clone.
- `docs/blog.md` draft (architecture, how we used TigerGraph, results, lessons) for the user to publish on dev.to/Hashnode.
- Demo script (TRD §17) plus a shot list for the user to record 3–5 min.
- Three social post drafts (X / LinkedIn / Instagram caption) tagging **@TigerGraphDB** and **@247pmstudio**, one per team member.
- `docs/tigergraph-feedback.md` finalized for the form's feedback field.

**Phase G — Submit (24 Sep 20:00–21:30)**
- Final: `pytest -q`, `python -m kavach check`, `git status` clean, push, confirm on GitHub that `cases/HHG-001.json … HHG-020.json` are at the repo root.
- Ask the user to make the repo **public**, then hand them the filled form answers (PRD §8) in one block ready to paste: team name, Devfolio ID, team size, contacts, repo URL, video URL, live UI URL, LLM model used, agent framework used, social post URLs, blog URL, TigerGraph feedback, Savanna + org ID.
- Form: https://docs.google.com/forms/d/e/1FAIpQLSeUF0lkkro3XmcCMFn8nGNRv6SLwfPjyjWG0d6wbgrpe2gNeQ/viewform

## Rules of engagement
- Follow `CLAUDE.md` non-negotiables at all times: no Kaggle originals, only real IDs, exact enums, no per-case hardcoding, no copying other teams' answers.
- Work autonomously. Don't stop to ask permission for reversible steps. Only the human-only items in Step 1 and Phase F/G need the user.
- If you fall behind a gate by more than 2 hours, apply the cut order (TRD §13) and tell the user what was cut.
- Report progress to the user at every gate in 3–5 lines: what's done, the check output, what's next, anything you need from them.
- Evidence before claims: never say something works without showing the command output.

Start now with Step 0.
