"""Kavach agent: one investigation as a state machine.

INTAKE -> GATHER -> DETECT -> RECALL -> ASSESS0 -> ACT0 (initial NBA)
       -> REQUEST_EVIDENCE? -> SIMULATE_REPLY -> ASSESS1 -> ACT1 (final NBA)
       -> NARRATE -> VALIDATE -> PERSIST

Tools and Python decide; the LLM only writes prose from the facts.
"""
import time

import pandas as pd

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


class Investigation:
    def __init__(self, case: dict, backend: str = "duck", llm: Llm | None = None, graph_conn=None, verbose: bool = False):
        self.case = case
        self.tools = ToolRegistry(backend)
        self.llm = llm or Llm()
        self.graph_conn = graph_conn
        self.verbose = verbose
        self.log: list[str] = []

    def say(self, msg: str) -> None:
        self.log.append(msg)
        if self.verbose:
            print(f"  [{self.case['case_id']}] {msg}")

    # ------------------------------------------------------------------
    def run(self) -> dict:
        t_start = time.perf_counter()
        tok0 = self.llm.tokens
        case, T = self.case, self.tools
        prior = PRIOR[case["trigger_type"]]

        # GATHER (steps 1-3) and DETECT (step 4)
        ctx = gather(case, T)
        self.say(f"gathered: {len(ctx.h)} card txns, device={ctx.device!r} (on {ctx.device_pop} cards), "
                 f"{0 if ctx.closed_hits is None else len(ctx.closed_hits)} closed-case hits")
        sigs, fnd = run_all(ctx)
        # RECALL (step 5)
        T.step = 5
        a0 = assess(sigs, prior)
        self.say(f"assess0: p={a0.p} {a0.verdict} groups={a0.by_group}")

        # ACT0 (step 6)
        T.step = 6
        need_request = not settled(a0)
        if fnd.recurring and case["trigger_type"] == "customer_report":
            need_request = True
        verdict0 = a0.verdict
        ep0 = ep_mod.build(ctx, fnd, "fraud" if verdict0 != "legitimate" else "legitimate")
        pin0 = self._policy_input(a0, verdict0, ctx, fnd, ep0, reply=None)
        initial = policy.initial_actions(pin0, will_request=need_request)

        requests, a1, reply = [], a0, None
        sigs1 = list(sigs)
        if need_request:
            # REQUEST_EVIDENCE (step 7) + SIMULATE_REPLY
            T.step = 7
            wo = assess([s for s in sigs if s.group != "customer"], prior)
            strongest = max([v for g, v in wo.by_group.items() if g != "score"], default=0.0)
            reply = customer_reply(wo.p, len(wo.fraud_groups), fnd.recurring, case["trigger_type"],
                                   n_episode=len(ep0.txn_ids), strongest_group=strongest)
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
            a1 = assess(sigs1, prior)
            # section 6: a verification response settles the question
            if reply.kind == "deny" and a1.p >= 0.70:
                a1.verdict = "fraud"
            elif reply.kind in ("confirm", "recognise_recurring") and a1.p <= 0.30:
                a1.verdict = "legitimate"
            if reply.kind == "no_reply":
                a1.verdict = "uncertain" if a1.verdict != "fraud" or a1.p < 0.85 else a1.verdict
            self.say(f"assess1: p={a1.p} {a1.verdict}")

        verdict = a1.verdict
        ep = ep_mod.build(ctx, fnd, verdict)
        pattern = ep_mod.pattern_for(ctx, fnd, verdict)
        if verdict == "legitimate":
            fnd.connected_cards, fnd.connected_devices, fnd.shared_element = {}, [], ""
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

        # NARRATE (step 11)
        T.step = 11
        file_sar = "FILE_REPORT" in {x["action"] for x in final}
        facts = self._facts(ctx, fnd, a1, verdict, pattern, ep, sigs1, reply, final, similar_ids)
        texts = write_all(self.llm, facts, want_sar=file_sar, undocumented=pattern == "undocumented")

        evidence = self._evidence(sigs1, fnd)
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

        # PERSIST (step 12): write the case into the graph and read it back
        if self.graph_conn is not None:
            T.step = 12
            from kavach.persist import write_case
            t0 = time.perf_counter()
            ok, gid = write_case(self.graph_conn, answer, case["card_id"])
            T.calls += 1
            T.trace.append({"step": 12, "tool": "write_case", "params": {"case_id": case["case_id"]},
                            "ms": round((time.perf_counter() - t0) * 1000, 1), "n_rows": int(ok)})
            answer["case"]["written_to_graph"], answer["case"]["graph_case_id"] = ok, gid

        answer["tool_calls"] = T.calls
        answer["tokens"] = self.llm.tokens - tok0
        answer["latency_s"] = round(time.perf_counter() - t_start, 2)
        self.trace = {"log": self.log, "tools": T.trace, "signals": [s.__dict__ for s in sigs1], "sources": texts["sources"],
                      "assess0": a0.__dict__, "assess1": a1.__dict__}
        return answer

    # ------------------------------------------------------------------
    def _case_memory(self, ctx, ep, query_text) -> list[dict]:
        """Case memory on the graph lane: earlier investigations written back into TigerGraph that touch this card or
        read like this one. They are cited in the evidence (their ids are graph ids, not dataset ids)."""
        impl = self.tools._impl
        if not hasattr(impl, "case_memory"):
            return []
        out = []
        try:
            hits = [h for h in self.tools.call("case_memory", card_ids=[ctx.card_id] + ep.connected_cards[:5])
                    if h["case_id"] != self.case["case_id"]]
            for h in hits[:2]:
                out.append({"claim": f"Case memory: earlier investigation {h['id']} ({h['case_id']}, verdict {h['verdict']}, pattern "
                                     f"{h['pattern']}) already touches card {h['card_id']}", "source": "graph",
                            "ref": f"query:case_memory(c={h['card_id']})", "entity_ids": [h["card_id"]]})
            sims = [s for s in self.tools.call("similar_investigations", text=query_text, k=3)
                    if s.get("case_id") and s["case_id"] != self.case["case_id"] and (s.get("score") or 0) >= 0.8]
            for s in sims[:1]:
                out.append({"claim": f"Case memory: the most similar earlier investigation by summary is {s['id']} ({s['case_id']}, "
                                     f"verdict {s['verdict']}, similarity {s['score']:.2f})", "source": "graph",
                            "ref": "query:similar_investigations(vectorSearch InvestigationCase.emb)", "entity_ids": []})
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
        )

    def _evidence(self, sigs: list[Signal], fnd) -> list[dict]:
        keep = sorted([s for s in sigs if abs(s.llr) >= 0.15 or s.group == "customer"], key=lambda s: -abs(s.llr))[:MAX_EVIDENCE]
        return [{"claim": s.claim, "source": s.source, "ref": s.ref, "entity_ids": [str(x) for x in s.entity_ids]} for s in keep]

    def _facts(self, ctx, fnd, a, verdict, pattern, ep, sigs, reply, final, similar_ids) -> dict:
        f = ctx.f
        fr = [s.claim for s in sorted(sigs, key=lambda s: -s.llr) if s.llr >= 0.4 and s.group not in ("score",)][:3]
        lg = [s.claim for s in sorted(sigs, key=lambda s: s.llr) if s.llr <= -0.4][:3]
        rows = ep.rows if ep.rows is not None and len(ep.rows) else ctx.h[ctx.h.TransactionID == f["TransactionID"]]
        regions = sorted({int(x) for x in rows.addr1.dropna()})
        channels = sorted(set(rows.channel))
        products = sorted(set(rows.ProductCD))
        devs = sorted({d for d in rows.device_key.dropna()}) if "device_key" in rows else []
        trig = {"risk_score": f"the bank's model scored transaction {int(f['TransactionID'])} at {float(f['risk_score']):.2f}",
                "customer_report": f"customer {ctx.customer_id} disputed transaction {int(f['TransactionID'])}",
                "analyst_request": "an analyst asked for a review of a device profile shared by several cards"}[ctx.trigger]
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
            "trigger": ctx.case["trigger_text"], "headline": headline,
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
            "how": ("a burst of online purchases priced just under a round limit" if fnd.undocumented_kind == "sub_threshold" else
                    "small online authorizations followed by a larger purchase" if fnd.card_testing else
                    "purchases from one device profile used across several customers' cards" if fnd.connected_devices else
                    "card-present purchases on an account already compromised in earlier confirmed cases" if "holder_prior_fraud" in fnd.episode else
                    "online purchases inconsistent with the cardholder" if f["channel"] == "online" else "card-present purchases inconsistent with the cardholder"),
            "top_fraud": "; ".join(fr), "top_legit": "; ".join(lg),
            "n_connected": len(ep.connected_cards), "connected": ep.connected_cards[:8], "shared": fnd.shared_element or "shared element",
            "connected_devices": ep.connected_devices, "prior_cases": similar_ids[:3],
            "customer_statement": stmt, "reply": reply.text if reply else "",
            "containment": containment, "rule": ", ".join(rules) or "§3a",
            "undocumented_kind": fnd.undocumented_kind, "threshold": fnd.notes.get("threshold"),
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
