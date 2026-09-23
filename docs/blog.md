---
title: "Kavach: building a fraud investigator that knows when a risk score is lying"
tags: tigergraph, graphdatabase, ai, fraud
---

# Kavach: building a fraud investigator that knows when a risk score is lying

*TigerGraph x Hacker House Goa 2026, Task 4: Agentic Fraud Investigation. Team PixelPaws.*

A bank's fraud model gives every card transaction a risk score. The task handed us 590,742 transactions, 5,565 closed investigations and 20 open alerts, and one warning in capitals: **a risk score is a reason to look, never a verdict.** Half of the 20 alerts are legitimate. The job is to decide which half, how far each fraud goes, and what the bank should do next under a written fraud policy, and to write each case back into the graph so the next investigation can find it.

We built **Kavach** ("shield"). This post covers what we built, how TigerGraph fits in, and the handful of things the data taught us that no amount of prompt engineering would have.

## The shape of the problem

Every answer has three parts: an internal **case** (verdict, pattern, affected transactions, connected cards, exposure, evidence), a **suspicious activity report** when the policy calls for one, and the **next best action** before and after the evidence the agent asks for. Everything is scored against a hidden key, and every ID has to exist in the data.

That rules out letting an LLM decide anything. In Kavach, **tools and Python decide, and the LLM writes.** Verdicts, amounts, IDs, actions and approval routes come from detectors, a calibrated evidence model and a policy engine. The LLM (Groq `openai/gpt-oss-120b`) writes the summary, the SAR narrative and the description of new fraud patterns from a facts JSON. Every ID it writes is checked against those facts, and a template takes over if it slips.

## Architecture

```
case -> GATHER -> DETECT -> ASSESS -> initial actions -> ask the customer? -> simulated reply
     -> ASSESS -> final actions -> RECALL similar cases -> NARRATE -> write back to TigerGraph -> answer file
```

- **GATHER** pulls the card's history, the customer's cards, the device profile and every other card on it, the billing region, closed cases touching any of these, and the history of the underlying account.
- **DETECT** runs general detectors in three families: single-card (sequence, device, region, match flags, score, the customer's words), cross-card (device rings, fixed-amount templates, repeated bursts) and history (repeat compromise, device precedents).
- **ASSESS** combines the strongest signal from each independent evidence group into a log-odds, so ten correlated device signals count once.
- **The policy engine** is Fraud Policy v1.0 as code: rules R1 to R10, the approval table, and "a case is not a report" (section 3a). It is unit-tested rule by rule.

## How we used TigerGraph

<!-- fill in once the Savanna load is done: schema screenshot, load counts, query timings, MCP -->

The graph holds customers, cards, transactions, device profiles, email domains, billing regions, the bank's closed cases, and the agent's own `InvestigationCase` vertices. Every agent tool is an installed GSQL query: `card_history`, `device_neighbors`, `region_activity`, `holder_fraud_history`, `closed_cases_for`, `amount_peers`. The agent runs the same code against TigerGraph (through pyTigerGraph or the TigerGraph MCP server) and against a DuckDB copy used for analysis. A parity test checks that both lanes give the same answers.

The question "what else happened on this device?" is a two-hop traversal, Transaction to DeviceProfile to Transaction to Card. It is the question that cracks the hardest cases.

## What the data taught us

**1. A card id is not a person.** `customer_id` is derived from the card issuer field, so a single card id can pool thousands of real accounts; one of ours had 10,332 transactions across dozens of regions. "This region is familiar to the card" means nothing on such a card. We rebuilt the underlying account as `card1 | billing region | account start day` (the start day comes from D1, days since the account opened) and moved every baseline to that account.

**2. The closed cases are a nearly complete label set.** Confirmed-fraud transactions are 3.4% of July to October traffic. An account with an earlier confirmed-fraud case turned out to be fraud in 1,059 later cases and cleared in none.

**3. The same score means different things.** Calibrated on the closed months, a score above 0.8 is 97% fraud for product C without an identity record, and 9% for product R. Kavach reads the score through that calibration instead of at face value. That single change turned several "high score, looks scary" alerts into correct legitimate closes.

**4. Selection bias hides in the labels.** Every cleared closed case was a high-score false alarm (0.82 to 0.94). Compare fraud with cleared naively and a new device looks *exculpatory*. We fit every signal's likelihood ratio within each product, against both the whole population and the cleared cases.

**5. Other cards solve cases.** A device profile seen on only 5 cards in six months paid about $100 on five customers' cards in six days. Nothing on any single card looks wrong. In the closed months that shape was 7 to 11 times more common in fraud than in normal traffic, and Kavach links the cards and recommends monitoring them under R6.

**6. Some fraud has no name.** Two patterns fit none of the five documented typologies: four online purchases in 30 minutes, each priced just under $500, repeated across a dozen cards; and a scripted phone profile, always new to the account and always behind an anonymous proxy, buying small amounts on 28 cards. Kavach labels both `undocumented`, describes them in its own words, and files a report under R9.

## Results

<!-- results table from README -->

## Knowing when to ask

When the evidence is short of a decision, Kavach does what the policy says: it verifies before blocking (R1), opens a case (section 3a), and simulates the customer's reply from the evidence it gathered *without* the customer. Fraud-leaning evidence leads to a denial, legitimate-leaning evidence to a confirmation, and balanced evidence to no reply within 24 hours (R4, then escalation under R8). Every answer records the initial actions, the assumed reply, the final actions, and what changed.

## Lessons

- Hand-investigate before you automate. We wrote 20 SQL dossiers before any agent code, and almost every detector traces back to one of them.
- Measure every weight. Three of our intuitions were wrong: new devices, high scores, and region familiarity.
- Keep the LLM on prose. It is very good at writing a FinCEN-style narrative from facts, and it should never be the thing deciding.

*Code: https://github.com/MONSTERBOY110/hhgoa-tigergraph. Built for TigerGraph x Hacker House Goa 2026. Thanks to @TigerGraphDB and @247pmstudio.*
