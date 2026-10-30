# Analyst in the loop (challenge 8)

> **Where the demo decision lives.** The analyst decision used in this write-up is a scripted demonstration (made with the
> analyst role token, as a stand-in for a human analyst). It is kept in `data/feedback_demo.jsonl` so it never changes the
> production answers; reproduce the before-and-after run with `KAVACH_FEEDBACK_FILE=data/feedback_demo.jsonl`. The production
> log `data/feedback.jsonl` holds only decisions a person records with `python -m kavach.feedback record ...`.


The agent should not repeat a mistake an analyst already corrected. An analyst accepts or rejects a **finding** (a signal the agent raised on a case, such as `device_ring` or `risk_score_untrusted`); the decision is kept in an append-only, hash-chained log; and the next investigation in the same context weighs that finding differently. No model is retrained: the decision changes one number, the likelihood ratio of that finding, in a stated and bounded way, and every changed signal says which decisions changed it.

Code: `kavach/feedback.py`. Tests: `tests/test_feedback.py` (13). Log: `data/feedback.jsonl` (not committed; `data/` is ignored).

## The flow

```
python -m kavach.feedback record HHG-019 device_ring reject --as-role analyst --reason "..."   # or --token <bearer>
python -m kavach.feedback list
python -m kavach.feedback verify
```

* **Who may decide.** The role comes only from the bearer token, resolved by `kavach.harness.approvals.role_for` (the same secret tokens and the same server-side check as the approvals; a role written in a request is never read). Scope `element` and `context` need role analyst or above. Scope `channel` changes behaviour for every product of a channel, so it needs L1 or above. An unknown token or a role that is too low appends a `denied` record and raises `Forbidden`.
* **What is recorded.** `{id FB-..., case_id, finding, decision, scope, role, approver (sha256 prefix of the token, never the token), ts, reason, key, signal snapshot (group, llr, claim, entity ids, how = the re-runnable query), context (trigger, product, channel), flagged_txn_id, dataset_version, prev_hash, hash}`. The finding must be one the agent actually raised on that case: `record` re-runs the case (no prose, nothing written) to read its signals, or takes them from the caller.
* **Where.** Its own append-only chain, `data/feedback.jsonl` (env `KAVACH_FEEDBACK_FILE`), with the canonical JSON, sha256 chain and file lock of `data/approvals.jsonl`. Feedback is not a policy action with an approval route, so it does not go into the approvals log. There is no update and no delete; a change of mind is a new record and each approver's latest decision on a key wins.
* **Fail closed.** `adjust()` verifies the chain before it uses any record; an edited, inserted or reordered line raises `FeedbackIntegrityError` (a `HarnessError`), so the investigation ends in an incident instead of running on tampered feedback.

## What changes, and by how much

`feedback.adjust(signals, ctx) -> signals` runs after detection. A record's key is the finding plus the context of the case it was made on:

| scope | key | role |
|---|---|---|
| element | detector, trigger, channel, product, and the shared value the finding is about (device profile, email domain, region or amount from its query parameters) | analyst |
| context (default) | detector, trigger, channel, product code | analyst |
| channel | detector, trigger, channel | L1 |

The trigger is part of every key because the same finding means different things on a model alert, where the customer has said nothing, and on a customer dispute (the policy's R1 and R2 are different rules). When a signal's key matches, the net decision of the distinct approvers decides: more rejects than accepts, the llr is multiplied by **0.25** (the finding still counts, at a quarter); more accepts, by 1.1 per net accept, **capped at 1.2**; a tie leaves it alone. The sign never flips, a zero-weight signal is never touched, and the input list is not mutated. The adjusted signal's claim ends with `[analyst feedback FB-...: down-weighted x0.25, weight +2.37 -> +0.59]` and it carries `feedback = {records, factor, llr_before, llr_after, net, key}`, which the trace and the attestation sidecar keep (`attest._sig_dict` copies the signal's attributes). `KAVACH_FEEDBACK=0` switches the whole mechanism off.

## The scenario: one feedback round from a Round 1 case

The bank's real outcome for **HHG-019** (closed record CC-5584, joined on card, customer and opened_at) is a single-transaction `card_not_present_new_device` fraud with **no connected cards and no report**. Our agent raised `device_ring` on it: "Rare device profile Windows | other | chrome 61.0 | 1280x720 (5 cards ever) was used on 4 other cards within 30 days, 4 of them for amounts within 2% of this $99.92 purchase" (llr +2.37, query `device_txns(device_key=..., t0=2016-12-01 17:28:53, days=30)`). In Round 1 that ring made us file a report and list four connected cards the bank did not accept (postmortem section 5). An analyst rejects the finding:

```
python -m kavach.feedback record HHG-019 device_ring reject --as-role analyst --reason "Analyst review against the bank's closed record for this investigation (outcome confirmed card_not_present_new_device fraud, 1 transaction, no connected cards, no report): the other cards on this rare device profile were not a ring. A shared profile with matched amounts on a model alert is not ring evidence in this context."
```

Record `FB-4d6109880a6e`, role analyst, approver `e11b6ba9a3eb`, key `{detector: device_ring, trigger: risk_score, channel: online, product: R}`, hash `9d8f3a77...837d`, dataset version `eb6caf5672c6`, flagged txn 3503878. `python -m kavach.feedback verify` -> `{"ok": true, "records": 1}`.

Why this finding: before choosing, a census replayed 515 closed cases through the current agent (all 255 cleared cases opened September to October 2016 as `risk_score` alerts on their flagged transaction, which is what they were; 260 seeded confirmed frauds as `customer_report` alerts on their latest episode transaction, which is what they were). It found **12 cleared customers blocked (3 of them also reported), and `device_ring` on a model alert was behind every one of them**; on a fraud replay it only ever fired with a customer report.

## Before and after on the same cases

Every case runs once with `KAVACH_FEEDBACK=0`. A probe computes what the recorded decisions would change; a case where at least one signal would change runs again with feedback on. A case with no matching signal gets identical signals in both arms by construction, so its after is its before. Both arms of a case run in the same process on the same code and index (`KAVACH_NO_LLM=1`, `KAVACH_RECORD=0`, nothing written). Truth: the closed record (Round 1 pack: CC-5566..CC-5585 joined on card, customer and opened_at; replays: their own record). The detectors only read closed cases opened before the alert, so a replay never sees its own outcome.

```
for i in 0 1 2 3 4 5; do python -m kavach.feedback eval --source-case HHG-019 --label ctx --shard $i --shards 6 --hhf & done
python -m kavach.feedback merge --label ctx --source-case HHG-019       # logs/feedback/ctx.json
```

"Acted against" counts the actions the brief scores as zero on a legitimate case (BLOCK_CARD, BLOCK_ALL_CARDS, FILE_REPORT); the second line adds DECLINE_TRANSACTION.

**One analyst reject, scope context (the record in data/feedback.jsonl):**

| set | cases | touched | cleared customers blocked or reported | ... incl. declined | frauds called fraud | frauds blocked |
|---|---|---|---|---|---|---|
| closed-case replays | 515 (255 cleared, 260 fraud) | 4 | **12 -> 9** | 45 -> 42 | 141 -> 141 | 141 -> 141 |
| Round 1 pack, other 19 | 19 (10 cleared, 9 fraud) | 0 | 0 -> 0 | 1 -> 1 (HHG-013 decline) | 9 -> 9 | 9 -> 9 |
| Round 2 pack (no truth) | 18 | 0 | no answer changes | | | |

Changed: CC-3385, CC-5078, CC-5129 (cleared) go from BLOCK_CARD at p 0.93 to 0.96 to legitimate at p 0.26 to 0.29 with ALLOW, VERIFY, MONITOR_CARD, CLOSE_NO_FRAUD. CC-3105 (cleared) stays blocked and reported: it also carries `device_in_confirmed_cases` (+1.4), which the analyst did not reject.

**Sensitivity: the same reject widened to scope channel by an L1 reviewer** (`KAVACH_FEEDBACK_FILE=logs/feedback/fb_channel.jsonl`, record FB-d969de8b5195, label `chan`; not in data/feedback.jsonl):

| set | touched | cleared customers blocked or reported | ... incl. declined | cleared left uncertain | frauds called fraud |
|---|---|---|---|---|---|
| closed-case replays | 12 | **12 -> 3** | 45 -> 42 | 33 -> 39 | 141 -> 141 |
| Round 1 pack, other 19 | 0 | 0 -> 0 | 1 -> 1 | 1 -> 1 | 9 -> 9 |

Changed: CC-3385, CC-5078, CC-5129 to legitimate; CC-3602, CC-3616, CC-3703, CC-4397, CC-4642, CC-5043 from BLOCK_CARD to uncertain with DECLINE, CREATE_CASE, MONITOR and ESCALATE_TO_ANALYST (R1 and R8: the customer is no longer blocked and a person decides); CC-3105, CC-3379, CC-5324 stay blocked because of other findings (`device_in_confirmed_cases`, `device_burst`).

**Fraud caught: no change** in either scope (141 of 260 replayed frauds called fraud before and after; 9 of 9 Round 1 frauds). The frauds that carry `device_ring` are customer reports, a different key.

**The cost, stated.** Re-run with the feedback, the source case HHG-019 itself goes from fraud p 0.96 to legitimate p 0.29: the ring was the only fraud evidence the agent had, and the bank confirmed the fraud through the customer. The analyst's rejection was right about the ring and the agent cannot tell "not a ring" from "not fraud" with a weight alone. Two guards follow: the factor is 0.25, not 0, so a ring that repeats with other evidence still counts; and the key includes the trigger, so a customer who has already denied the charge (R2) is never affected by a rejection made on a model alert.

## Integration (lead)

`kavach/detectors/__init__.py`, at the end of `run_all`, after the ring and pattern filters:

```python
    # analyst feedback (kavach.feedback): accepted or rejected findings re-weighted in the same context; KAVACH_FEEDBACK=0 disables
    from kavach import feedback
    sigs = feedback.adjust(sigs, ctx)
    changed = feedback.changed(sigs)
    if changed:
        fnd.notes["analyst_feedback"] = changed
    return sigs, fnd
```

Optional: a server endpoint `POST /feedback {case_id, finding, decision, reason, scope}` with the bearer token calling `feedback.record(...)` (returns 403 on `Forbidden`); a casefile event `detected` already lists the signal names, and `fnd.notes["analyst_feedback"]` can be added to its `extra`. With the current `data/feedback.jsonl` (one context-scope record) the 18 HHF answers do not change (0 touched in the eval above).
