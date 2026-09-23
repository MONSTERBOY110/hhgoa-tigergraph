# Demo video: script and shot list (target 4:00, hard limit 3 to 5 minutes)

Record at 1080p with OBS (or the Windows Game Bar, Win+G). Set the terminal font to 16 pt or larger, and use a dark theme for both the terminal and the browser. Upload to YouTube as **Unlisted** and paste the link into the form.

| Time | Shot | What is on screen | Voice-over (read naturally) |
|---|---|---|---|
| 0:00 | Title | README top of the GitHub repo | "This is Kavach, our agent for TigerGraph's fraud investigation task. A bank's model scores every card transaction, and the README warns that the score is often wrong both ways. Kavach decides what is actually going on, how far it goes, and what the bank should do." |
| 0:25 | The trap | `docs/dossiers/README.md`, the table | "Before writing agent code we investigated all 20 cases by hand. Three things we learned became the agent: card ids pool many real accounts, the closed cases are near-complete labels, and a score means different things per product." |
| 0:55 | Graph | Savanna GraphStudio: schema view | "Everything lives in TigerGraph Savanna: customers, cards, 590 thousand transactions, device profiles, email domains, billing regions, the bank's closed cases, and our own investigation cases." |
| 1:15 | Graph query | GraphStudio: run `device_neighbors` on the SM-G935F profile and show the star of 28 cards | "Every tool the agent uses is an installed GSQL query. Here is the device-ring query: one scripted phone profile, always new, always behind an anonymous proxy, touching 28 customers' cards." |
| 1:40 | Live run | Terminal: `python -m kavach investigate HHG-006 --backend tg --verbose --no-write` | "Here is one case live. A customer disputes a $482 purchase. Kavach finds four purchases in 30 minutes, each just under $500, and the same template on 15 other cards. That matches no documented pattern, so it is labelled undocumented, with a case plus a report under R9." |
| 2:20 | Evidence request | Case viewer (`site/index.html#HHG-019`): the readout strip, then the ego-graph, then Next best actions with Initial, the assumed reply, and Final | "When evidence is short of a decision, Kavach follows the policy: verify before blocking, and open a case. Here a 0.90 score on a product where high scores are mostly legitimate is only solved through other cards: a device seen on 5 cards ever. Initial actions verify; after the assumed denial, the final actions block, report, and monitor the connected cards." |
| 3:00 | Legit close | Case viewer, press `j`/`k` to HHG-001 (or click it in the left rail) | "Half the alerts are legitimate. This one is a weekly $77 charge on a clean account in its home region, closed with no case and no report." |
| 3:15 | Case memory | GraphStudio: InvestigationCase vertex with its edges, then `case_memory` | "Every investigation is written back into the graph, with edges to the card, the transactions, the device and the similar closed cases, and read back before we mark it written. The next investigation on that card retrieves it." |
| 3:40 | Validator | Terminal: `python -m kavach check` then `pytest -q` | "A validator checks every file: schema, exact enums, every ID against the dataset, approval routes, and report consistency. 20 of 20 valid, and every policy rule is unit-tested." |
| 3:55 | Close | README results table | "Deterministic decisions, calibrated evidence, TigerGraph as the system of record, and an LLM only for writing. Thanks for watching." |

## Case viewer
Serve it locally with `python -m http.server 8765 --directory site` and open http://127.0.0.1:8765/#HHG-019, or use the GitHub Pages link once Pages is on.

## Commands to have ready (run each once beforehand so nothing is slow on camera)
```powershell
.venv\Scripts\activate
python -m kavach investigate HHG-006 --backend tg --verbose --no-write   # --no-write keeps the committed answer file
python -m kavach check
pytest -q
```

## GraphStudio queries to prepare
- `device_neighbors`, d = `SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080`, 2016-11-01 to 2016-12-10
- `case_memory`, c = `C13487-K1`
