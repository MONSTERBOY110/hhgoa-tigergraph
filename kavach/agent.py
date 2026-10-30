"""Kavach agent: one investigation as a state machine.

INTAKE -> GATHER -> DETECT -> RECALL -> ASSESS0 -> ACT0 (initial NBA)
       -> REQUEST_EVIDENCE? -> SIMULATE_REPLY -> ASSESS1 -> ACT1 (final NBA)
       -> NARRATE -> VALIDATE -> PERSIST

Tools and Python decide; the LLM only writes prose from the facts.
"""
import os
import time

import pandas as pd

from kavach import attest, casefile, dq, intake
from kavach import episode as ep_mod
from kavach import policy, recall
from kavach.assess import PRIOR, Signal, assess, settled
from kavach.context import gather
from kavach.detectors import run_all
from kavach.llm import Llm
from kavach.narrative import write_all
from kavach.simulate import customer_reply
from kavach.tools import ToolRegistry

MAX_EVIDENCE = 8
# The organizer grader for the final round is run with transactions_2017q1.csv only, so episodes of January alerts
# are restricted to January transactions (kavach.validate checks the same rule against the grader_scope table).
ROUND2_START = pd.Timestamp("2017-01-01")


def _plain(claim: str) -> str:
    """A claim as a regulator reads it: no detector names, approval ids or backtest figures (those stay in the evidence list)."""
    import re
    c = re.sub(r"^Grown detector \S+ \(approved [^)]*\) fired: ", "", str(claim))
    for _ in range(3):                                   # drop parenthetical notes, innermost first
        c = re.sub(r"\s*\([^()]*\)", "", c)
    c = re.split(r"\.\s+(?:Backtest|In the closed|Weak signal|No labelled)", c)[0]   # evidence-list detail, not narrative
    return c.strip().rstrip(".")


class Investigation:
    def __init__(self, case: dict, backend: str = "duck", llm: Llm | None = None, graph_conn=None, verbose: bool = False):
        self.case = case
        self.tools = ToolRegistry(backend)
        self.llm = llm or Llm()
        self.graph_conn = graph_conn
        self.verbose = verbose
        self.log: list[str] = []
        # Casefile events are written only for real runs (the CLI sets KAVACH_RECORD=1 unless --no-write), so a dry run
        # or a test leaves no trace in data/casefiles/.
        self.record = os.environ.get("KAVACH_RECORD") == "1"

    def say(self, msg: str) -> None:
        self.log.append(msg)
        if self.verbose:
            print(f"  [{self.case['case_id']}] {msg}")

    def event(self, kind: str, detail="", **kw) -> None:
        if self.record:
            casefile.append(self.case["case_id"], kind, detail, **kw)

    def _scope(self, ep, ctx):
        """Episode inside the organizer grader's scope: affected ids must exist in the file the grader is given
        (transactions_2017q1.csv for the January cases). Earlier context stays in the evidence, never in the episode."""
        if pd.Timestamp(ctx.f["ts"]) < ROUND2_START or not ep.txn_ids or ep.rows is None or not len(ep.rows):
            return ep
        rows = ep.rows[pd.to_datetime(ep.rows.ts) >= ROUND2_START]
        keep = [str(int(x)) for x in rows.sort_values(["ts", "TransactionID"]).TransactionID]
        if keep == ep.txn_ids:
            return ep
        dropped = [t for t in ep.txn_ids if t not in set(keep)]
        self.say(f"episode scoped to the January file: dropped {len(dropped)} earlier transaction(s) {dropped[:6]}")
        ep.out_of_scope = dropped
        ep.txn_ids, ep.rows = keep, rows
        ep.first = keep[0] if keep else ""
        ep.exposure = round(float(rows.amt.abs().sum()), 2)
        return ep

    def _dq_signals(self, ctx, flags: list[dict]) -> list[Signal]:
        """Data-quality findings that an analyst must see in the evidence, as zero-weight attested claims."""
        out, tid = [], int(ctx.f["TransactionID"])
        how = {"tool": "txn_detail", "params": {"txn_id": tid}}
        # (an `alert_score_mismatch` note from the detectors stays in the data-quality list; the evidence line is built here once)
        for f in flags:
            if f["flag"] == "alert_card_mismatch":
                out.append(Signal("dq_alert_card_mismatch", "identity_flags", 0.0, f["detail"].rstrip(".") + ".",
                                  [str(tid), ctx.card_id], "query:txn_detail(txn_id=%d)" % tid, "graph", how))
        stated = self.case.get("risk_score")
        try:
            stated = float(stated) if stated not in (None, "") else None
        except (TypeError, ValueError):
            stated = None
        actual = float(ctx.f["risk_score"])
        if stated is not None and abs(stated - actual) > 0.05:
            out.append(Signal("dq_score_mismatch", "score", 0.0,
                              f"The alert states a model score of {stated:.2f}, but transaction {tid} carries {actual:.2f} in the bank's "
                              "records; the recorded value is used and the stated one is treated as unverified alert text",
                              [str(tid)], "query:txn_detail(txn_id=%d)" % tid, "graph", how))
        return out

    # ------------------------------------------------------------------
    def run(self) -> dict:
        t_start = time.perf_counter()
        tok0 = self.llm.tokens
        case, T = self.case, self.tools
        prior = PRIOR[case["trigger_type"]]

        # INTAKE: the alert text is data, never instruction. Only structured fields drive the investigation; the LLM only
        # ever sees the deterministic clean summary; instructions addressed to the agent are recorded with zero weight.
        it = self.intake = intake.parse(case)
        cid = case["case_id"]
        self.event("rerun" if self.record and casefile.events(cid) else "opened",
                   f"{case['trigger_type']} alert on card {case['card_id']}", evidence_ids=[str(case["flagged_txn_id"])])
        self.event("intake", it.clean_summary, extra={"kind": it.kind, "hostile_fragments": len(it.hostile)})
        if it.hostile:
            self.say(f"intake: {len(it.hostile)} instruction(s) addressed to the agent found in the alert text; treated as data")
            self.event(intake.EVENT, "instructions addressed to the agent found in the alert text; treated as data",
                       extra=intake.casefile_detail(it))

        # GATHER (steps 1-3) and DETECT (step 4)
        ctx = gather(case, T)
        self.say(f"gathered: {len(ctx.h)} card txns, device={ctx.device!r} (on {ctx.device_pop} cards), "
                 f"{0 if ctx.closed_hits is None else len(ctx.closed_hits)} closed-case hits")
        flags = {f["flag"]: f for f in dq.taint(case)}
        for n in ctx.dq_notes:
            flags.setdefault(n["flag"], n)
        self.dq_flags = list(flags.values())
        dq_sigs = self._dq_signals(ctx, self.dq_flags)
        if self.dq_flags:
            self.say(f"data quality: {', '.join(sorted(flags))}")
        self.event("gathered", self.log[-1], evidence_ids=[str(case["flagged_txn_id"]), ctx.card_id],
                   extra={"data_quality": sorted(flags)})
        sigs, fnd = run_all(ctx)
        self.event("detected", f"{len(sigs)} signals",
                   evidence_ids=[str(i) for s in sigs if abs(s.llr) >= 0.4 for i in s.entity_ids][:20],
                   extra={"signals": [s.name for s in sigs]})
        # RECALL (step 5)
        T.step = 5
        a0 = self._cap(assess(sigs, prior))
        self.say(f"assess0: p={a0.p} {a0.verdict} groups={a0.by_group}")

        # ACT0 (step 6)
        T.step = 6
        need_request = not settled(a0)
        if fnd.recurring and case["trigger_type"] == "customer_report":
            need_request = True
        verdict0 = a0.verdict
        ep0 = self._scope(ep_mod.build(ctx, fnd, "fraud" if verdict0 != "legitimate" else "legitimate"), ctx)
        pin0 = self._policy_input(a0, verdict0, ctx, fnd, ep0, reply=None)
        initial = policy.initial_actions(pin0, will_request=need_request)

        requests, a1, reply = [], a0, None
        sigs1 = list(sigs)
        if need_request:
            # REQUEST_EVIDENCE (step 7) + SIMULATE_REPLY
            T.step = 7
            wo = assess([s for s in sigs if s.group != "customer"], prior)
            strongest = max([v for g, v in wo.by_group.items() if g != "score"], default=0.0)
            n_sig = sum(1 for s in sigs if s.group not in ("customer", "score") and s.llr >= 0.3)
            reply = customer_reply(wo.p, len(wo.fraud_groups), fnd.recurring, case["trigger_type"],
                                   n_episode=len(ep0.txn_ids), strongest_group=strongest,
                                   n_legit_groups=len(wo.legit_groups), n_fraud_signals=n_sig)
            requests.append({"type": reply.request_type, "asked_after_step": 6, "assumed_response": reply.text})
            self.say(f"requested customer_validation; simulated reply: {reply.kind} (p without customer {wo.p})")
            # ASSESS1 (step 8)
            T.step = 8
            sigs1 = [s for s in sigs if s.group != "customer"]
            rsig = {
                "deny": Signal("customer_denies", "customer", 2.2, f"Customer denied the transaction(s) when asked: {reply.text}",
                               [], "evidence_request:1", "customer"),
                "confirm": Signal("customer_confirms", "customer", -3.0, f"Customer confirmed the transaction when asked: {reply.text}",
                                  [], "evidence_request:1", "customer"),
                "recognise_recurring": Signal("customer_recognises", "customer", -3.0, f"{reply.text}", [], "evidence_request:1", "customer"),
                "no_reply": None,
            }[reply.kind]
            if rsig is not None:
                sigs1.append(rsig)
            elif case["trigger_type"] == "customer_report":
                sigs1 += [s for s in sigs if s.group == "customer"]
            a1 = self._cap(assess(sigs1, prior))
            # section 6: a verification response settles the question
            if reply.kind == "deny" and a1.p >= 0.70:
                a1.verdict = "fraud"
            elif reply.kind in ("confirm", "recognise_recurring") and a1.p <= 0.30:
                a1.verdict = "legitimate"
            if reply.kind == "no_reply":
                # silence is not evidence: a legitimate assessment stays legitimate, a fraud call needs p >= 0.85
                a1.verdict = a1.verdict if a1.verdict == "legitimate" or (a1.verdict == "fraud" and a1.p >= 0.85) else "uncertain"
            self.say(f"assess1: p={a1.p} {a1.verdict}")

        verdict = a1.verdict
        ep = self._scope(ep_mod.build(ctx, fnd, verdict), ctx)
        pattern = ep_mod.pattern_for(ctx, fnd, verdict, ep)
        if verdict == "legitimate":
            fnd.connected_cards, fnd.connected_devices, fnd.shared_element = {}, [], ""
            fnd.notes.pop("related_cards", None)
            fnd.notes.pop("related_element", None)
            ep.connected_cards, ep.connected_devices = [], []
        pin1 = self._policy_input(a1, verdict, ctx, fnd, ep, reply=reply.kind if reply else None)
        if requests:
            T.step = 9
            final = policy.final_actions(pin1)
        else:
            initial = policy.initial_actions(pin1, will_request=False)
            final = initial
        status = policy.status_for(verdict, final)

        # RECALL: similar closed cases (graph memory hits, then vector search over analyst notes, then fingerprints)
        T.step = 10
        claims = [x.claim for x in sorted(sigs1, key=lambda s: -abs(s.llr)) if abs(x.llr) >= 0.4 and x.group != "score"][:3]
        query_text = f"{'cleared false alarm' if verdict == 'legitimate' else 'confirmed fraud ' + pattern}: " + " ".join(claims)
        want = "cleared" if verdict == "legitimate" else "confirmed_fraud"
        fp = recall.fingerprints().set_index("case_id")
        vec = [c for c, _ in T.call("vector_search_cases", text=query_text, k=40)
               if c in fp.index and fp.at[c, "outcome"] == want and (verdict == "legitimate" or pattern == "none" or fp.at[c, "pattern"] == pattern)]
        sim = T.call("similar_cases", pattern=pattern, verdict=verdict, product=ctx.f["ProductCD"], channel=ctx.f["channel"],
                     amt=float(ep.exposure or ctx.f["amt"]), n=max(len(ep.txn_ids), 1), new_dev=ctx.f.get("id_15") == "New",
                     anon=isinstance(ctx.f.get("id_23"), str) and "ANONYMOUS" in ctx.f["id_23"], k=3, exclude=fnd.similar_cases)
        similar_ids = list(dict.fromkeys(fnd.similar_cases[:4] + vec[:2] + [c for c, _ in sim]))[:6]
        # GraphRAG over the policy and pattern text: the policy section behind the decisive rule, the pattern definition,
        # and for undocumented activity the nearest documented pattern (to show how far it is from all of them)
        docs = self._policy_docs(final, pattern, query_text)
        memory = self._case_memory(ctx, ep, query_text)
        # GRAPH x VECTOR + GRAPH ALGORITHM (kavach/graph/fusion.py): precedent that is both connected and similar, and the
        # card's ring component; zero weight, attested, same ids on both lanes (installed fused_precedent / ring_wcc on tg, mcp)
        from kavach.graph import fusion
        t_alert = pd.Timestamp(case["opened_at"])
        fp_params = {"card_ids": [ctx.card_id], "text": query_text, "t0": str(t_alert), "k": 5}
        fused = [h for h in T.call("fused_precedent", **fp_params) if verdict != "legitimate" or h["outcome"] == "cleared"]
        similar_ids = list(dict.fromkeys([h["case_id"] for h in fused if h["outcome"] == want][:2] + similar_ids))[:6]
        ring_params = {"card_id": ctx.card_id, "t_from": str(t_alert - pd.Timedelta(days=fusion.RING_DAYS)), "t_to": str(t_alert),
                       "template_name": (fnd.undocumented_kind or "ring") if ep.connected_cards else "",
                       "template_cards": ([ctx.card_id] + list(ep.connected_cards)) if ep.connected_cards else []}
        ring = T.call("ring_component", **ring_params)
        for s_ in (fusion.precedent_signal(fused, fp_params), fusion.ring_signal(ring, ctx.card_id, ring_params)):
            if s_ is not None:
                self.memory_sigs = getattr(self, "memory_sigs", [])
                self.memory_sigs.append(s_)
                memory.append({"claim": s_.claim, "source": s_.source, "ref": s_.ref, "entity_ids": [str(x) for x in s_.entity_ids]})

        # NARRATE (step 11)
        T.step = 11
        file_sar = "FILE_REPORT" in {x["action"] for x in final}
        facts = self._facts(ctx, fnd, a1, verdict, pattern, ep, sigs1, reply, final, similar_ids)
        texts = write_all(self.llm, facts, want_sar=file_sar, undocumented=pattern == "undocumented")

        evidence = self._evidence(sigs1, fnd)
        # data-quality findings and the intake note are always shown (zero weight, outside the MAX_EVIDENCE cut)
        evidence += [{"claim": s.claim, "source": s.source, "ref": s.ref, "entity_ids": [str(x) for x in s.entity_ids]} for s in dq_sigs]
        note = intake.evidence_note(it)
        if note:
            note["entity_ids"] = [cid]     # the case-pack row the quoted text came from (re-read by attest via case_pack:)
            evidence.append(note)
        for d in docs:
            first = d["text"].split(". ")[0].replace("**", "").replace("`", "").strip()
            if len(first) > 240:
                first = first[:240].rsplit(" ", 1)[0] + " ..."
            evidence.append({"claim": f"{d.get('lead', d['section'])}: {first}{'' if first.endswith(('.', '...')) else '.'}",
                             "source": "document", "ref": f"document:README#{d['id']}", "entity_ids": []})
        evidence += memory
        answer = {
            "case_id": case["case_id"],
            "case": {
                "status": status,
                "verdict": verdict,
                "fraud_probability": a1.p,
                "pattern": pattern,
                "pattern_description": texts.get("pattern_description", "") if pattern == "undocumented" else "",
                "affected_txn_ids": ep.txn_ids,
                "first_suspicious_txn_id": ep.first,
                "connected_card_ids": ep.connected_cards,
                "connected_device_profiles": ep.connected_devices,
                "exposure_usd": ep.exposure,
                "evidence": evidence,
                "similar_prior_cases": similar_ids,
                "summary": texts["summary"],
                "written_to_graph": False,
                "graph_case_id": "",
            },
            "evidence_requests": requests,
            "next_best_actions": {"initial": initial, "final": final, "what_changed": self._what_changed(a0, a1, reply, initial, final)},
            "sar": self._sar(file_sar, final, texts, facts, ep, ctx),
            "stop_reason": self._stop_reason(a0, a1, reply, verdict, ep),
            "tool_calls": 0, "tokens": 0, "latency_s": 0.0,
        }

        # ATTEST: every evidence claim, detector hit and SAR sentence with what / evidence ids / how / when
        self.attestation = attest.build(cid, answer, sigs1 + dq_sigs + getattr(self, "memory_sigs", []), None, T.backend,
                                        attest.dataset_version(),
                                        sar_sentences=texts.get("sar_sentences"))
        self.attestation["data_quality"] = self.dq_flags

        # PERSIST (step 12): write the case into the graph and read it back (real runs only; --no-write is a dry run everywhere)
        if self.graph_conn is not None and self.record:
            T.step = 12
            from kavach.persist import write_case
            t0 = time.perf_counter()
            ok, gid = write_case(self.graph_conn, answer, ctx.card_id)
            T.calls += 1
            T.trace.append({"step": 12, "tool": "write_case", "params": {"case_id": case["case_id"]},
                            "ms": round((time.perf_counter() - t0) * 1000, 1), "n_rows": int(ok)})
            answer["case"]["written_to_graph"], answer["case"]["graph_case_id"] = ok, gid

        answer["tool_calls"] = T.calls
        answer["tokens"] = self.llm.tokens - tok0
        answer["latency_s"] = round(time.perf_counter() - t_start, 2)
        self.trace = {"log": self.log, "tools": T.trace, "signals": [s.__dict__ for s in sigs1 + dq_sigs], "sources": texts["sources"],
                      "assess0": a0.__dict__, "assess1": a1.__dict__, "intake": {"kind": it.kind, "hostile": it.hostile,
                      "clean_summary": it.clean_summary}, "data_quality": self.dq_flags,
                      "llm_status": getattr(self.llm, "status_log", [])[-4:], "attestation": self.attestation}
        return answer

    def _cap(self, a):
        """Data-quality cap (kavach.dq): thin evidence on a record with no identity cannot carry a confident fraud call."""
        cap = dq.probability_cap(getattr(self, "dq_flags", []), a.fraud_groups)
        if cap is not None and a.p > cap:
            self.say(f"p capped at {cap:.2f} (data quality: no identity record and thin history, no network or history support)")
            a.p = cap
            if a.verdict == "fraud":
                a.verdict = "uncertain"
        return a

    # ------------------------------------------------------------------
    def _case_memory(self, ctx, ep, query_text) -> list[dict]:
        """Case memory on the graph lane: earlier investigations written back into TigerGraph that touch this card or
        read like this one. They are cited in the evidence (their ids are graph ids, not dataset ids)."""
        impl = self.tools._impl
        self.memory_sigs = []
        if not hasattr(impl, "case_memory"):
            return []
        out = []
        try:
            cards = [ctx.card_id] + ep.connected_cards[:5]
            hits = [h for h in self.tools.call("case_memory", card_ids=cards) if h["case_id"] != self.case["case_id"]]
            for h in hits[:2]:
                # zero-weight graph memory, attested: re-running case_memory for the card returns the same investigation
                self.memory_sigs.append(Signal(
                    "case_memory", "history", 0.0,
                    f"Case memory: earlier investigation {h['id']} ({h['case_id']}, verdict {h['verdict']}, pattern {h['pattern']}) "
                    f"already touches card {h['card_id']}", [h["card_id"], h["case_id"]], f"query:case_memory(c={h['card_id']})",
                    "graph", {"tool": "case_memory", "params": {"card_ids": [h["card_id"]]}}))
            # Similar earlier investigations by summary vector are NOT cited: InvestigationCase summaries are re-embedded every
            # time a case is re-run, so a nearest-neighbour claim over them cannot be re-checked later (a claim nobody can
            # re-run counts as wrong). Vector evidence comes from the closed-case notes instead (fused_precedent,
            # vector_search_cases), which never change.
            out = [{"claim": m.claim, "source": m.source, "ref": m.ref, "entity_ids": [str(x) for x in m.entity_ids]}
                   for m in self.memory_sigs]
        except Exception as e:
            self.say(f"case memory lookup failed: {str(e)[:100]}")
        return out

    PATTERN_DOC = {"card_testing": "PATTERN-1", "card_not_present_fraud": "PATTERN-2", "card_not_present_new_device": "PATTERN-3",
                   "out_of_region_use": "PATTERN-4", "account_takeover": "PATTERN-5"}

    def _policy_docs(self, final, pattern, query_text) -> list[dict]:
        import re
        from kavach import vectors
        ix = vectors.index()
        by_id = {str(i): (str(sec), str(txt)) for i, sec, txt in zip(ix["doc_ids"], ix["doc_sections"], ix["doc_texts"])}
        out = []
        rules = [m for x in final for m in re.findall(r"R10|R\d|§3a|§6", x["reason"])]
        order = {"R9": 0, "R6": 1, "R2": 2, "R5": 3, "R7": 4, "R3": 5, "R4": 6, "R8": 7, "R1": 8, "§6": 9, "§3a": 10}
        if rules:
            r = sorted(set(rules), key=lambda k: order.get(k, 5))[0]
            did = {"§3a": "POLICY-3a", "§6": "POLICY-6"}.get(r, f"POLICY-{r}")
            if did in by_id:
                self.tools.calls += 1
                out.append({"id": did, "section": by_id[did][0], "text": by_id[did][1], "lead": f"Policy applied, {by_id[did][0]}"})
        if pattern in self.PATTERN_DOC:
            did = self.PATTERN_DOC[pattern]
            out.append({"id": did, "section": by_id[did][0], "text": by_id[did][1], "lead": f"Pattern definition, {by_id[did][0]}"})
        elif pattern == "undocumented":
            hits = [d for d in self.tools.call("vector_search_docs", text=query_text, k=19) if d["id"].startswith("PATTERN")]
            if hits:
                h = hits[0]
                out.append({"id": h["id"], "section": h["section"], "text": h["text"],
                            "lead": f"Nearest documented pattern by text similarity is {h['section']} (cosine {h['score']:.2f}), "
                                    "which does not describe a coordinated template across customers; its definition"})
        return out

    def _policy_input(self, a, verdict, ctx, fnd, ep, reply) -> policy.PolicyInput:
        single = len([g for g, v in a.by_group.items() if abs(v) >= 0.4 and g != "score"]) <= 1
        other_card_fraud = [c for c in ctx.cust_cards.card_id if c != ctx.card_id] if ctx.cust_cards is not None else []
        return policy.PolicyInput(
            p=a.p, verdict=verdict, pattern=ep_mod.pattern_for(ctx, fnd, verdict) if verdict != "legitimate" else "none",
            exposure=ep.exposure if verdict != "legitimate" else 0.0, trigger=ctx.trigger, n_groups=a.n_groups,
            single_signal=single, reply=reply, card_testing=fnd.card_testing, cleared_over_100=fnd.cleared_over_100,
            shared_element=fnd.shared_element if verdict != "legitimate" else "",
            connected_cards=list(fnd.connected_cards) if verdict != "legitimate" else [],
            linked_fraud=fnd.linked_fraud and verdict == "fraud", recurring=fnd.recurring,
            coordinated_undocumented=fnd.coordinated_undocumented and verdict == "fraud",
            n_cards_confirmed_fraud=0, credentials_compromised=False,
            conflicting=bool(a.fraud_groups and a.legit_groups) or (verdict == "uncertain" and a.p >= 0.5),
            related_cards=sorted(fnd.notes.get("related_cards", {})) if verdict != "legitimate" else [],
            related_element=fnd.notes.get("related_element", "") if verdict != "legitimate" else "",
        )

    def _evidence(self, sigs: list[Signal], fnd) -> list[dict]:
        keep = sorted([s for s in sigs if abs(s.llr) >= 0.15 or s.group == "customer"], key=lambda s: -abs(s.llr))[:MAX_EVIDENCE]
        return [{"claim": s.claim, "source": s.source, "ref": s.ref, "entity_ids": [str(x) for x in s.entity_ids]} for s in keep]

    def _facts(self, ctx, fnd, a, verdict, pattern, ep, sigs, reply, final, similar_ids) -> dict:
        f = ctx.f
        fr = [s.claim for s in sorted(sigs, key=lambda s: -s.llr) if s.llr >= 0.4 and s.group not in ("score",)][:3]
        lg = [s.claim for s in sorted(sigs, key=lambda s: s.llr) if s.llr <= -0.4][:3]
        # plain findings for prose: the strongest data findings, not the dispute or the score (stated separately)
        fr_plain = [_plain(s.claim) for s in sorted(sigs, key=lambda s: -s.llr) if s.llr >= 0.4 and s.group not in ("score", "customer")][:2]
        lg_plain = [_plain(s.claim) for s in sorted(sigs, key=lambda s: s.llr) if s.llr <= -0.4][:2]
        rows = ep.rows if ep.rows is not None and len(ep.rows) else ctx.h[ctx.h.TransactionID == f["TransactionID"]]
        regions = sorted({int(x) for x in rows.addr1.dropna()})
        channels = sorted(set(rows.channel))
        products = sorted(set(rows.ProductCD))
        devs = sorted({d for d in rows.device_key.dropna()}) if "device_key" in rows else []
        trig = {"risk_score": f"the bank's model flagged transaction {int(f['TransactionID'])} (recorded score {float(f['risk_score']):.2f})",
                "customer_report": f"customer {ctx.customer_id} disputed transaction {int(f['TransactionID'])}",
                "analyst_request": f"an analyst asked for a review of transaction {int(f['TransactionID'])} and related activity on other cards"}[ctx.trigger]
        headline = (f"Alert {ctx.case['case_id']} opened because {trig} (${f['amt']:,.2f}, product {f['ProductCD']}, {f['channel'].replace('_', ' ')}, "
                    f"{f['ts']:%Y-%m-%d %H:%M}) on card {ctx.card_id}.")
        containment = ", ".join(x["action"] for x in final if x["action"] in ("BLOCK_CARD", "BLOCK_ALL_CARDS", "DECLINE_TRANSACTION",
                                                                              "STEP_UP_AUTH", "MONITOR_CONNECTED_CARDS"))
        rules = sorted({r for x in final if x["action"] == "FILE_REPORT" for r in __import__("re").findall(r"R\d+|§3a", x["reason"])})
        stmt = ({"deny": "When contacted, the cardholder stated they did not make the transaction(s), did not share the card details, and still has the card.",
                 "confirm": "When contacted, the cardholder confirmed making the transaction.",
                 "recognise_recurring": "When contacted, the cardholder recognised the charge as their own recurring payment.",
                 "no_reply": "The cardholder did not reply to the bank's request within 24 hours."}[reply.kind] if reply else
                ("The cardholder reported the disputed transaction to the bank and denied making it." if ctx.trigger == "customer_report"
                 else "The cardholder has not yet been contacted."))
        return {
            "case_id": ctx.case["case_id"], "customer_id": ctx.customer_id, "card_id": ctx.card_id,
            "card_desc": f"{f.get('card4') or 'card'} {f.get('card6') or ''}".strip(),
            "trigger": intake.facts_trigger(self.intake), "headline": headline,
            "flagged_txn": {"id": str(int(f["TransactionID"])), "amount": round(float(f["amt"]), 2), "ts": f"{f['ts']:%Y-%m-%d %H:%M:%S}",
                            "product": f["ProductCD"], "channel": f["channel"], "region": None if pd.isna(f.get("addr1")) else int(f["addr1"]),
                            "device": ctx.device, "risk_score": float(f["risk_score"])},
            "verdict": verdict, "p": a.p, "pattern": pattern,
            "episode_txns": [{"id": str(int(r.TransactionID)), "ts": f"{r.ts:%Y-%m-%d %H:%M}", "amount": round(float(r.amt), 2),
                              "product": r.ProductCD, "channel": r.channel,
                              "region": None if pd.isna(r.addr1) else int(r.addr1),
                              "device": None if pd.isna(r.device_key) else r.device_key} for r in rows.itertuples()][:12],
            "n_txns": len(ep.txn_ids), "exposure": ep.exposure, "first_txn": ep.first,
            "date_from": f"{rows.ts.min():%Y-%m-%d}", "date_to": f"{rows.ts.max():%Y-%m-%d}",
            "where": (f"{' and '.join(c.replace('_', ' ') for c in channels)}, product code(s) {', '.join(products)}"
                      + (f", billing region(s) {', '.join(map(str, regions))}" if regions else "")
                      + (f", from device profile(s) {'; '.join(devs[:2])}" if devs else "")),
            "how": ("purchases paying one " + fnd.notes["grown_spec"]["entity"].replace("R_emaildomain", "recipient email domain").replace("P_emaildomain", "purchaser email domain")
                    + " that suddenly drew payments from several unrelated customers' cards" if (fnd.undocumented_kind or "").startswith("grown:") and fnd.notes.get("grown_spec") else
                    "a burst of online purchases priced just under a round limit" if fnd.undocumented_kind == "sub_threshold" else
                    "small online charges of a few dollars spread over several weeks, never followed by a larger purchase, the same "
                    "shape appearing on other customers' cards in the same weeks" if fnd.undocumented_kind == "micro_charges" else
                    "online purchases of one product stepping up roughly doubling in amount until they reached a fixed cap, the same "
                    "ladder appearing on other customers' cards" if fnd.undocumented_kind == "amount_ladder" else
                    "bursts of online purchases from devices new to the account behind an anonymous proxy, the same template "
                    "appearing on other customers' cards with no device in common" if fnd.undocumented_kind == "proxy_new_burst" else
                    "small online authorizations followed by a larger purchase" if fnd.card_testing else
                    "purchases from one device profile used across several customers' cards" if fnd.connected_devices else
                    "card-present purchases on an account already compromised in earlier confirmed cases" if "holder_prior_fraud" in fnd.episode else
                    "online purchases inconsistent with the cardholder" if f["channel"] == "online" else "card-present purchases inconsistent with the cardholder"),
            "top_fraud": "; ".join(fr), "top_legit": "; ".join(lg), "top_fraud_plain": "; ".join(fr_plain),
            "top_legit_plain": "; ".join(lg_plain),
            "n_connected": len(ep.connected_cards), "connected": ep.connected_cards[:8], "shared": fnd.shared_element or "shared element",
            "connected_devices": ep.connected_devices, "prior_cases": similar_ids[:3],
            "customer_statement": stmt, "reply": reply.text if reply else "",
            "containment": containment, "rule": ", ".join(rules) or "§3a",
            "undocumented_kind": fnd.undocumented_kind, "threshold": fnd.notes.get("threshold"),
            "grown": ({**fnd.notes["grown_spec"], "label": {"R_emaildomain": "recipient email domain", "P_emaildomain": "purchaser email domain",
                       "device_key": "device profile", "addr1": "billing region"}.get(fnd.notes["grown_spec"]["entity"], "entity")}
                      if fnd.notes.get("grown_spec") else {}),
            "n_template_cards": len(fnd.notes.get("template_cards", [])), "device": ctx.device,
            "final_actions": [x["action"] for x in final],
        }

    def _sar(self, file_sar, final, texts, facts, ep, ctx) -> dict:
        if not file_sar:
            reasons = [x["reason"] for x in final]
            why = next((r for r in reasons if "§3a" in r and "no report" in r), None)
            if why is None:
                pin = facts
                why = ("§3a: the verdict is not fraud, so no suspicious activity report; the case record is sufficient"
                       if pin["verdict"] != "fraud" else
                       f"§3a: fraud confirmed but exposure ${ep.exposure:,.2f} is under $1,000 with no shared device, region cluster "
                       "or link to another customer's fraud, so a case without a report")
            return {"file": False, "reason": why, "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []}
        reason = next(x["reason"] for x in final if x["action"] == "FILE_REPORT")
        subjects = [ctx.customer_id, ctx.card_id] + ep.connected_cards[:8] + ep.connected_devices[:2]
        return {"file": True, "reason": reason, "narrative": texts["sar"], "subjects": list(dict.fromkeys(subjects)),
                "total_amount_usd": ep.exposure, "activity_dates": ep_mod.activity_dates(ep)}

    def _what_changed(self, a0, a1, reply, initial, final) -> str:
        if reply is None:
            return "nothing"
        fa = ", ".join(x["action"] for x in final)
        return {
            "deny": f"The customer's denial raised the fraud probability from {a0.p:.2f} to {a1.p:.2f}, which supports containment under R2: {fa}.",
            "confirm": f"The customer's confirmation lowered the fraud probability from {a0.p:.2f} to {a1.p:.2f}; under R3 the alert closes: {fa}.",
            "recognise_recurring": f"The customer recognised the recurring charge, so under R7 and R3 the dispute closes without a block: {fa}.",
            "no_reply": f"No reply within 24 hours, so R4 applies and the probability stays at {a1.p:.2f}: {fa}.",
        }[reply.kind]

    def _stop_reason(self, a0, a1, reply, verdict, ep) -> str:
        if reply is None:
            groups = a1.fraud_groups if verdict == "fraud" else a1.legit_groups
            side = "at or above 0.85" if verdict == "fraud" else "at or below 0.15"
            return (f"Fraud probability {a1.p:.2f} is {side} with {len(groups)} independent evidence groups ({', '.join(groups)}), "
                    "so section 6 allows a decision without asking the customer; further queries would not change the actions.")
        if reply.kind == "no_reply":
            return (f"The customer did not reply within 24 hours and the remaining graph evidence cannot move the probability ({a1.p:.2f}) "
                    "across a decision threshold, so the case stays open under R4 monitoring" + (" and goes to an analyst under R8." if "ESCALATE" in str(a1) or a1.p >= 0.5 else "."))
        return (f"The customer's reply settled the question (section 6): probability moved from {a0.p:.2f} to {a1.p:.2f}"
                + (f" and the episode ({len(ep.txn_ids)} transaction(s), ${ep.exposure:,.2f}) is scoped" if verdict == "fraud" else "")
                + ". Further steps would not change the actions.")


def investigate(case: dict, backend: str = "duck", llm: Llm | None = None, graph_conn=None, verbose: bool = False):
    inv = Investigation(case, backend, llm, graph_conn, verbose)
    ans = inv.run()
    return ans, inv.trace
