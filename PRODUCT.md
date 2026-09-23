# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Static HTML, CSS and vanilla JS, no build step, served from `site/` on GitHub Pages (the page must also work from file://). Data is read from `site/cases.js` (a script assignment, so file:// works), generated from `cases/*.json` by `python -m kavach site`. Fonts are self-hosted in `site/fonts/`.

## Users

TigerGraph judges and the Hacker House Goa 2026 reviewers evaluating Task 4 (Agentic Fraud Investigation). They open the repo and the optional "Live UI URL" from the submission form, usually on a laptop, and want to see quickly what the agent concluded for each of the 20 exam cases and why.

## Product Purpose

Kavach is an agent that investigates card-fraud alerts on a TigerGraph graph and produces, for each case, an internal case record, a suspicious activity report when policy requires one, and the next best actions before and after requested evidence. The viewer makes those 20 answer files browsable: results at a glance, then the full reasoning for any case. Success: a judge understands the verdict, the evidence behind it, and the policy-routed actions for any case within a minute.

## Positioning

Decisions are deterministic and evidence-cited (detectors, calibrated weights fitted on the bank's closed cases, a policy engine encoding rules R1 to R10); TigerGraph is the system of record and case memory; the LLM only writes prose. The viewer shows that chain: evidence with sources, rule-cited actions with approval routes, initial versus final recommendations.

## Operating Context

Read-only browsing of the 20 case files and the 5 monitor findings. Terms the judges know from the task README: verdict (fraud, legitimate, uncertain), pattern enum, exposure, SAR, approval routes auto, L1, L2, evidence requests with assumed replies.

## Capabilities and Constraints

- Shows only what is in the answer files; nothing is invented or recomputed in the page.
- No em dashes or en dashes in any copy (team rule).
- Must work offline from file:// (inline or relative assets, no API calls).

## Brand Commitments

The user asked for a TigerGraph-branded look: TigerGraph's orange and charcoal, graph-network motifs. Product name Kavach ("shield"), team PixelPaws.

## Evidence on Hand

- `cases/HHG-001.json` to `HHG-020.json` (answer files), `monitor/MON-*.json`, `docs/dossiers/README.md`.
- No screenshots, logos or testimonials are provided; do not fabricate TigerGraph logos or endorsements.

## Product Principles

1. Every number and ID on screen comes from an answer file.
2. The reasoning chain (evidence, rule, action, route) is the product; show it, do not summarize it away.
3. Legitimate closes deserve the same clarity as fraud: half the cases are legitimate.
