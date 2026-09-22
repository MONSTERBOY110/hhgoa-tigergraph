# PRD — TigerGraph × HH Goa 2026, Task 4: Agentic Fraud Investigation

**Project codename:** `hhgoa-tigergraph` (agent name for the README/blog: **Kavach** — "shield"; rename freely)
**Repo:** `D:\Projects\hhgoa-tigergraph` → `https://github.com/MONSTERBOY110/hhgoa-tigergraph` (must be **public** before submitting)
**Deadline:** **Thursday 24 Sep 2026, 11:59 PM IST.** One submission per team, by the team lead. **No resubmissions.**
**Internal target:** form submitted by **24 Sep 21:30 IST** (2.5 h buffer).
**Source of truth for the task:** `TASK-README.md` (verbatim copy of the organizer's dataset README). If this PRD and that file disagree, that file wins.

---

## 1. Why this matters

Hacker House Goa 2026 (28–31 Oct, Goa, fully sponsored residency, ~247 builders) selects builders through online trials. Tasks 1–3 are closed and we skipped them. Task 4, set by TigerGraph, is our last route to the finals. The goal is not "a working demo". The goal is **a top score against a hidden answer key plus a submission package that makes the TigerGraph team want us in Goa.**

## 2. What the organizers asked for (verified)

Load the provided IEEE-CIS–based dataset into TigerGraph. Build an agent that takes each of the 20 exam cases, investigates it using the graph and the 5,565 closed cases, decides **what kind of fraud it is (if any), how far it goes, and what to do next** under Fraud Policy v1.0, and **knows when it needs more evidence**. For every case it writes one JSON answer file with three parts:

1. **Case** — internal investigation record (status, verdict, calibrated probability, pattern, evidence, affected transactions, first suspicious txn, connected cards and device profiles, exposure, retrieved prior cases, summary) **and written into the graph** as case memory.
2. **SAR** — suspicious activity report, only when policy §3a calls for it; must stand on its own (who/what/when/where/how/why, 6–12 sentences).
3. **Next best actions** — `initial` (before requested evidence), `final` (after simulated replies), `what_changed`; exact action ids and approval routes; cite rule numbers.

Plus `evidence_requests`, `stop_reason`, `tool_calls`, `tokens`, `latency_s`.

**Optional:** an autonomous monitor that finds and investigates alerts beyond the 20 → separate `monitor/` folder → counts toward **Innovation**, not accuracy.

## 3. How we will be scored (inferred from the README; treat as the working model)

| Area | What they check (evidence in README) | Our lever |
|---|---|---|
| **Verdict accuracy** | fraud / legitimate / uncertain vs key. "Half the cases are legitimate… an agent that blocks everything scores badly." `uncertain` earns full credit on designed-ambiguous cases if R1/R8 followed | Hand-investigated dossiers → general detectors; honest `uncertain` |
| **Pattern** | 7-value enum; undocumented patterns "scored" | Detectors for all 5 + explicit undocumented detectors (rings, structuring, etc.) |
| **Episode scope** | `affected_txn_ids`, `first_suspicious_txn_id`, `connected_card_ids`, `connected_device_profiles`, `exposure_usd` = sum of abs amounts | Episode builder that walks back/forward from the flagged txn and across shared devices/regions |
| **Calibration** | "`fraud_probability`… scored for calibration" | Log-odds evidence model fitted on closed cases; clamp and sanity-check |
| **Next best action** | exact actions, routes, ordering, case-vs-report decision ("part of the NBA score"), initial→final change | Deterministic policy engine encoding R1–R10, §2 routes, §3a thresholds |
| **SAR quality** | FinCEN narrative standard; `file` must match FILE_REPORT; subjects/amount/dates | Template-grounded LLM narrative from structured facts only |
| **Evidence & explanation** | every claim sourced, entity_ids real, rules cited (§7) | Evidence ledger produced by tools, never by the LLM |
| **Stopping** | §6: stop at p≥0.85 / ≤0.15 with ≥2 independent evidence, or settled by reply, or no further value | Stop rule in the state machine + honest `stop_reason` |
| **Case memory** | `written_to_graph`, `graph_case_id`, retrievable by the next investigation | Upsert `InvestigationCase` vertex + edges + embedding, then re-retrieve it |
| **Efficiency** | `tool_calls`, `tokens`, `latency_s` reported | Real counters; lean tool plan |
| **Innovation** | monitor/, undocumented patterns, engineering quality | monitor/ stretch, static case viewer, blog |
| **Hard fails** | made-up IDs score 0; missing fields score 0 for that part; using Kaggle originals = **disqualification** | Validator gate on every run; data only from the provided files |

## 4. Users

- **Primary: the organizers' scoring script + TigerGraph judges.** They read `cases/*.json` straight from the repo, then README, video, blog.
- **Secondary (the story we tell): a bank fraud analyst.** Gets a defensible case, a ready SAR, and approval-routed actions instead of a raw risk score.

## 5. Requirements

### Must (non-negotiable for submission)
- M1. Data downloaded from the organizer's Drive; **no** original Kaggle/IEEE files anywhere in the pipeline.
- M2. Data loaded into **TigerGraph** (Savanna free tier preferred; Community Edition acceptable) with a schema that extends the suggested one.
- M3. Agent investigates via graph queries (installed GSQL queries; via **TigerGraph MCP** where it works) and retrieves similar **closed cases** as memory.
- M4. Agent runs on all 20 cases end to end with one command; writes `cases/HHG-001.json` … `cases/HHG-020.json` at repo root, exact schema, exact enums.
- M5. Every case written back to TigerGraph (`written_to_graph: true`, real `graph_case_id`) and retrievable by the next run.
- M6. Deterministic policy engine: exact action ids, routes (BLOCK_CARD L1 ≤ $2,500 / L2 > $2,500; FILE_REPORT and BLOCK_ALL_CARDS always L2; DECLINE_TRANSACTION L1; rest auto), R1–R10, §3a, §6.
- M7. `sar.file` ⇔ `FILE_REPORT` in `final`; legitimate ⇒ empty episode, exposure 0, no SAR.
- M8. Validator (`python -m kavach check`) passes on all 20 files: schema, enums, IDs exist in dataset, exposure = sum, routes legal, SAR consistency, `final == initial` when no evidence requested.
- M9. README with end-to-end run instructions (download → load → run → check), architecture, results table.
- M10. **3–5 min demo video**, link-viewable (YouTube unlisted or Loom/Drive).
- M11. **Technical blog** (dev.to / Hashnode / Medium): what we built, architecture, how we used TigerGraph, results.
- M12. **Social post from every team member** tagging **@TigerGraphDB** and **@247pmstudio**.
- M13. Submission form filled by the team lead before the deadline (checklist in §8).

### Should
- S1. Vector retrieval (TigerVector) over closed-case narratives + policy/pattern text + a few FinCEN docs (GraphRAG).
- S2. Evidence weights calibrated from closed cases (likelihood ratios per signal), not hand-picked.
- S3. Undocumented-pattern detection with plain-language `pattern_description`.
- S4. Per-case dossier markdown in `docs/dossiers/` (shows the human-in-the-loop investigation that shaped the agent).

### Could (only after Must+Should are green)
- C1. `monitor/` autonomous sweep over Nov–Dec risk scores and device/region rings → `MON-xxx.json`.
- C2. Static case viewer (GitHub Pages) → "Live UI URL (optional)" field.
- C3. Louvain/WCC community detection from the TigerGraph algorithm library.

### Won't (this round)
- Custom ML model training to replace the risk score, real customer messaging, multi-tenant UI, auth.

## 6. Success metrics

| Metric | Target |
|---|---|
| Valid answer files | 20/20 pass validator, 0 fabricated IDs |
| Verdict mix | Roughly half legitimate (README hint); every fraud verdict backed by ≥2 independent evidence groups or an explicit uncertain/escalate path |
| Policy compliance | 0 illegal routes; 0 BLOCK on single weak signal below p 0.70 (R1); 0 BLOCK_ALL_CARDS without R10 condition |
| Graph writeback | 20/20 `written_to_graph: true`, retrievable by `similar_cases` query |
| Run time | full 20-case run < 20 min; per-case `latency_s` reported truthfully |
| Package | repo public, README, video 3–5 min, blog, posts, form — all by 24 Sep 21:30 IST |

## 7. Timeline (IST)

| When | Milestone | Gate |
|---|---|---|
| 23 Sep 00:30–03:00 | Repo scaffold, data download, DuckDB index, validator; **user** creates Savanna workspace + Groq key | `check` runs on the README example |
| 03:00–10:00 | Hand-investigate all 20 cases into dossiers; Savanna schema + loader in parallel | 20 dossiers with a provisional verdict each |
| 10:00–20:00 | Detectors, weights, policy engine, answer builder; first full run (offline lane) | **Safety-net push: 20 valid files on GitHub by 23 Sep 20:00** |
| 23 Sep 20:00 → 24 Sep 10:00 | TigerGraph queries, MCP, writeback, vector recall, LLM narratives; rerun on TigerGraph | 20 files with `written_to_graph: true` |
| 24 Sep 10:00–12:00 | Review every answer against its dossier; freeze answers v1 | Answers frozen |
| 12:00–20:00 | monitor/ (stretch), README, blog, video, posts, optional viewer | All links live |
| 20:00–21:30 | Final validator, push, make repo public, submit form | Form confirmation screenshot |

## 8. Submission form checklist (exact fields)

Form: https://docs.google.com/forms/d/e/1FAIpQLSeUF0lkkro3XmcCMFn8nGNRv6SLwfPjyjWG0d6wbgrpe2gNeQ/viewform

- [ ] Team name — exactly as on Devfolio
- [ ] Devfolio ID of team lead
- [ ] Team size (1 / 2 / 3)
- [ ] Lead: Name, Email, Phone (one line, comma separated); Members 2–3 same format if any
- [ ] Public GitHub repo URL — `cases/HHG-001.json` … `HHG-020.json` at root (they score directly from the repo)
- [ ] Tick "My repo has all 20 answer files in cases/…"
- [ ] Demo video URL (3–5 min)
- [ ] Live UI URL (optional)
- [ ] LLM model used — e.g. "Groq openai/gpt-oss-120b (narratives only; decisions are deterministic)"
- [ ] Agent framework used — e.g. "Custom Python state-machine agent + TigerGraph MCP / pyTigerGraph"
- [ ] Social post URLs — every member, tagging @TigerGraphDB and @247pmstudio
- [ ] Technical blog URL
- [ ] TigerGraph experience feedback (what worked, what was slow/confusing, what you wish existed) — keep notes in `docs/tigergraph-feedback.md` while building
- [ ] Deployment: Savanna → Savanna org ID (Savanna → organization settings)

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| 47 h total, one human | Safety-net push by 23 Sep 20:00; strict cut order (TRD §13) |
| Savanna signup/provisioning slow or free-tier limits | Start signup at minute 0; slim loader (subset of columns, V-features stay in DuckDB); Community Edition in Docker as fallback |
| TigerGraph MCP flaky | 1-hour timebox; same tool interface backed by pyTigerGraph REST; mention MCP honestly in README |
| Groq rate limits | LLM used only for ~3 short generations per case; OpenAI-compatible client swaps to Azure gpt-5-mini / Gemini via env; deterministic template fallback so a run never fails on the LLM |
| Over-flagging (blocking legit customers) | Evidence model requires ≥2 independent groups for fraud; R1 enforced in code; dossier review before freeze |
| Fabricated IDs from LLM | LLM never emits IDs into structured fields; validator checks every ID against the dataset |
| Accidental disqualification | No Kaggle files, no downloading public IEEE-CIS data, no copying competitor answer files |
| Submitting a broken repo | Final checklist + fresh-clone test of README steps before submitting |
