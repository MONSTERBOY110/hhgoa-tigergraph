# Social post drafts

Every team member must post one, tagging **@TigerGraphDB** and **@247pmstudio**. Replace `<VIDEO>` and `<BLOG>` with the real links before posting, then paste each post URL into the form.

## X / Twitter (Subhojyoti)
Built Kavach for @TigerGraphDB x @247pmstudio Hacker House Goa 2026 🛡️

An agent that investigates card-fraud alerts on a TigerGraph graph:
• rebuilds the real account behind pooled card ids
• calibrates the risk score per product (a 0.9 is 97% fraud on one product, 9% on another)
• cracks device rings through other cards
• files SARs only when the policy says so

<VIDEO>
#TigerGraph #HackerHouseGoa #GraphAI

## LinkedIn (Puskar)
We just shipped Kavach, our submission for the TigerGraph x Hacker House Goa 2026 agentic fraud investigation challenge. @TigerGraphDB @247pmstudio

The brief: 590,742 card transactions, 5,565 closed bank investigations, 20 open alerts, and a warning that the bank's risk score is often wrong in both directions. Half the alerts are legitimate.

What we learned by investigating every case by hand before writing agent code:
1. A card id is not a person. One id pooled more than 10,000 transactions, so we rebuilt the real account from the issuer, billing region and account start day.
2. The same score means different things. Calibrated on the closed cases, a score above 0.8 is 97% fraud for one product and 9% for another.
3. Some cases can only be solved on other cards. A device seen on 5 cards in six months paid about $100 on five customers' cards in six days.

Kavach keeps decisions deterministic: detectors, calibrated evidence and a policy engine that encodes every rule. TigerGraph is the system of record and the agent's case memory. The LLM only writes the report narratives.

Demo: <VIDEO>
Write-up: <BLOG>

#TigerGraph #GraphDatabase #FraudDetection #AIagents #HackerHouseGoa

## Instagram caption (either member, with a screenshot of the device-ring graph)
One phone profile, always new to the account, always behind an anonymous proxy, buying small amounts on 28 strangers' cards 🕸️
Our agent Kavach found it by asking the graph what else that device touched.
Built for @tigergraphdb x @247pmstudio Hacker House Goa 2026 🛡️
#TigerGraph #HackerHouseGoa #FraudDetection #GraphAI #BuildInPublic
