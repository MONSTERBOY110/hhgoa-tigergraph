# Challenge 4: evidence to SAR

A filing an analyst could send with minor edits, for every case where policy section 3a requires a report, with a
citation on every sentence that a judge can re-run. Code: `kavach/filing.py`. Tests: `tests/test_filing.py` (19).
Output: `sar/<case>.md`, `sar/<case>.html` (self-contained, prints on A4), `sar/manifest.json`.

## Commands

```
python -m kavach.filing --all                 # every answer in cases/; writes sar/ and sar/manifest.json
python -m kavach.filing HHF-006               # one case
python -m kavach.filing verify --all          # every filing in sar/: footnotes, ids, html, timeline, re-run of cited claims
python -m kavach.filing verify HHF-006 --no-rerun
# options: --cases-dir, --attest-dir, --out (defaults cases/, attestations/, sar/)
```

Run it after the final `python -m kavach run --all` (it reads the answers and the attestation sidecars that run
writes). `--all` also removes a stale `sar/<case>.*` for any case that no longer files.

## Section 3a decides whether a filing exists

`filing.section_3a(answer)` reads the policy text, from the answer alone, independently of `kavach/policy.py`:
a report is owed when fraud is confirmed or strongly suspected (verdict `fraud`, or p at or above 0.70 and not
legitimate) AND at least one of: exposure over $1,000; connected cards or a shared device profile listed; the pattern
is undocumented (R9). Then `filing.decide`:

| Answer says | 3a says | Outcome |
|---|---|---|
| `sar.file` true | required, FILE_REPORT and CREATE_CASE in the final actions | `filed`: filing written |
| `sar.file` true | not required | `refused`: nothing written, reason logged in the manifest |
| `sar.file` true | FILE_REPORT or CREATE_CASE missing from the final actions | `refused` |
| `sar.file` false | required | `missing`: nothing drafted, flagged for review, exit 1 |
| `sar.file` false | not required | `not_required` |

The brief's "$23 loss that connects to eleven other cards" is exactly the HHF-004 and HHF-005 shape in the dry run
below: $23.99 and $23.17 of micro charges, 7 and 11 connected cards, so 3a requires a report and one is filed.

## What a filing contains

1. Filing institution: placeholders the analyst fills in (name, identifier, address, contact), the internal case id.
2. Customer and cards concerned: customer, case card (with card brand and type from the index), number of connected
   cards, device profiles, and the subject of the activity (an unidentified party; the linking element when there is one).
3. Suspicious activity: activity dates, total amount over N transactions, the category in plain words (for example
   "Card fraud: coordinated activity across customers that fits none of the documented typologies"), the policy basis
   (`sar.reason`), the assessment, the section 3a test line by line, and the agent's own pattern description for R9.
4. Narrative: the answer's own `sar.narrative`, sentence by sentence, each sentence with a footnote marker.
5. Transaction timeline: every affected transaction from the index (id, time, amount, product, channel, region,
   device profile, and which attested claims cite it), flagged and first marked, total.
6. Connected cards: each card, its customer, what links it, how many of its transactions carry that link and for how
   much, when it first did (with the transaction id), and which attested claims name it.
7. Evidence graph: an inline SVG drawn from the data at build time (case card at the centre, its affected
   transactions on the left, the linking element and other elements the episode carries, connected cards on the right;
   solid edges where the data shows the shared element, dashed where only an attested claim names the card).
8. Citations: one footnote per sentence.
9. Attested claims cited: each claim with what, how (tool and parameters), when (timestamp, dataset version).
10. Checks before filing: exposure equals the sum of the timeline, `sar.total_amount_usd` equals exposure, the first
    suspicious transaction is the earliest, activity dates match, every sentence cited, and the L2 approval status of
    FILE_REPORT from the append-only approvals log (a draft says it must not be sent before an L2 approval is recorded).

Every filing opens with the no tip-off instruction: do not disclose the report, or that one is being considered, to
the subject, the cardholder or anyone outside fraud and compliance staff; nothing in it is sent to the customer. The
alert's free text (`trigger_text`) is never read.

## Citations: every sentence, re-runnable

The narrative sentences are the sidecar's `sar_sentence` claims (S1 to Sn, written by the agent in the same run as the
answer). Each footnote names:

- the sentence's own claim (for example **S2**), whose query is `lookup_ids` over the ids it cites;
- the evidence claims that support it (`supported_by`: the E and D claims that share its ids), each with its own query;
- the ids, grouped as transactions, cards, customers, closed cases and device profiles;
- for a sentence that names no transaction (who, prior cases, the cardholder's statement), every affected transaction
  with the claims that attest them (for example "Episode transactions (attested in **S2**, **S3**, **S4**)"; with an LLM
  narrative each transaction is often named in its own sentence).

A sentence whose claim cites no ids is replaced by the closest template sentence that has ids
(`kavach.attest.sar_sentences_template` over facts rebuilt from the answer and the index), keeping only ids the sidecar
attests; the footnote says it was rewritten and quotes the original. If nothing attested remains, the sentence is
dropped and the drop is recorded. An uncited sentence is never written.

`verify` checks the documents as written: every narrative sentence ends in a marker and markers run 1..n, one per
footnote; every footnote names claims that exist in the sidecar and ids that the sidecar attests; every id exists in
the index; the HTML carries the same footnotes (claims and ids in data attributes) as the Markdown; the timeline is
exactly `affected_txn_ids`; the answer still files under 3a; no en or em dash; the HTML has no external reference; the
no tip-off line is present; and every cited claim is re-run (`kavach.attest.verify_claim`).

## Fail closed

No filing, with the reason in the manifest and exit 1, when: the answer has no attestation sidecar; the sidecar's SAR
sentences do not reproduce the answer's narrative (a stale sidecar from another run); an affected transaction, card
or cited id is not in the index; no sentence carries an attested id. Link details for connected cards are
supplementary: if they cannot be built the cards are still listed as attested, and the page says so.

## Dry run (30 Oct 01:15 IST, current code, no LLM, scratch folder, nothing written to cases/ or sar/)

`run_case` with `KAVACH_NO_LLM=1 KAVACH_RECORD=0` for all 18 cases, answers and sidecars to a scratch folder, then
`python -m kavach.filing --all` and `verify --all` on that folder:

| Case | Verdict | Exposure | Connected | 3a basis | Filing | Verify |
|---|---|---:|---:|---|---|---|
| HHF-001 | fraud 0.89, undocumented | $2,189.02 | 14 | exposure, connected, R9 | filed, 9 sentences, 3 txns | 33 checks, 0 failed, 1 warning |
| HHF-002 | fraud 0.97, undocumented | $2,166.71 | 14 | exposure, connected, R9 | filed, 9 sentences, 3 txns | 33 checks, 0 failed, 1 warning |
| HHF-003 | fraud 0.94, undocumented | $8,551.47 | 2 | exposure, connected, R9 | filed, 9 sentences, 7 txns | 33 checks, 0 failed |
| HHF-004 | fraud 0.97, undocumented | $23.99 | 7 | connected, R9 | filed, 9 sentences, 6 txns | 33 checks, 0 failed, 1 warning |
| HHF-005 | fraud 0.95, undocumented | $23.17 | 11 | connected, R9 | filed, 9 sentences, 6 txns | 33 checks, 0 failed |
| HHF-006 | fraud 0.88, undocumented | $967.68 | 16 | connected, R9 | filed, 9 sentences, 3 txns | 32 checks, 0 failed |
| other 12 | fraud without a 3a condition (007), uncertain or legitimate | | | not required | none | |

`filing: 18 answer(s): filed 6, refused 0, missing 0, not_required 12, error 0`; `verify: 6 filing(s), 0 failed check(s)`.

How each ring is linked in the filing (built from the data, not copied from the claim text):
- HHF-006: recipient email domain `sc.rr.com`, from the ring query over the case card and the 16 cards
  (`python -m kavach.site ring HHF-006`): each card's payments to the domain, amount and first payment.
- HHF-001, HHF-002: no shared device; the link is the access signature `C|IP_PROXY:ANONYMOUS|New|mobile`, from a
  re-run of the query recorded in claim E1 (`entity_neighbors(entity_field=access_signature, ...)`), 2 or 3 transactions
  per card, $497 to $2,167.
- HHF-004, HHF-005: no shared value at all; the link is the behaviour the claim's query selects
  (`small_online_txns(max_amt=6.0)`), with each card's small online charges in that query's window.

The three warnings are one upstream defect (below), not a filing problem: the index check passes for those keys.

## Live check on the LLM narratives (30 Oct 01:28 IST)

The lead's final run (`kavach run --all --backend tg --persist`, LLM prose) had written HHF-001 to HHF-004 by 01:28.
`python -m kavach.filing HHF-001 HHF-002 HHF-003 HHF-004 --out <scratch>` then `verify`: 4 filed (7, 7, 7 and 8
sentences: the LLM rewrites the narrative and `kavach.attest.cite_sentences` cites each sentence), `verify: 4
filing(s), 0 failed check(s)`, no warnings (the LLM text drops the trailing space of the device keys).

## Known issues for the lead

- `kavach/attest.py` strips whitespace from every id (`_norm_scalar`), and a device key with an empty screen or
  device field ends with a space in the index (`"SM-J700T1 |  |  | "`). The sidecar then records the key stripped and
  `python -m kavach attest` FAILS the SAR sentence that names it (HHF-001 S3, HHF-002 S3, HHF-004 S3 today). Fix in
  `attest._lookup_ids`: `select distinct trim(device_key) k from txn where trim(device_key) in (select unnest(?))`
  (and return `k`). `kavach.filing` already matches device keys trimmed on both sides and reports the attest re-run as
  a warning when that is the only difference.
- Template SAR prose: sentence 6 reads "The same a repeated template of product C purchases ... links this activity"
  (`facts["shared"]` is a phrase with an article for template rings), and sentence 5 pastes raw evidence claims
  (detector names, approval ids, lift figures) into the "why". The LLM rewrite fixes both when it is accepted; with the
  template only, consider a SAR-specific `shared` noun phrase and a shorter `top_fraud` for `sar_template`.

## Integration

After the final run (and after any L2 approval of FILE_REPORT, so the filing shows it):
```
python -m kavach.filing --all
python -m kavach.filing verify --all
```
Commit `sar/` (Markdown, HTML, manifest). Optional CLI wiring in `kavach/cli.py`:
`@app.command() def filing(case: str = "", all: bool = False): from kavach.filing import main; raise typer.Exit(main([case] if case else ["--all"]))`.
