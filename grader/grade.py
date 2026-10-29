#!/usr/bin/env python3
"""
grade.py - the Round 2 grader. Python 3 only, no dependencies.

    python grade.py --teams-dir teams --case-pack case_pack_final.csv --out report.json

Each team folder under --teams-dir holds a cases/ directory of answer files
(<case_id>.json). Findings come in three classes:

  hard     structural: required fields, valid values, internal consistency,
           and - when --transactions is given - ID existence and arithmetic.
  policy   Fraud Policy v1.0: approval routes, R5/R6/R8/R9, case-vs-report (3a),
           tip-off, and whether evidence actually moved the recommendation.
  warning  heuristics a judge reads by hand. Not scored.

Passing every hard and policy check is necessary, not sufficient.
"""
import argparse,csv,json,os,re,sys
from collections import defaultdict

ACTIONS={"ALLOW_TRANSACTION","DECLINE_TRANSACTION","MONITOR_CARD","MONITOR_CONNECTED_CARDS",
 "WARN_CUSTOMER","VERIFY_WITH_CUSTOMER","STEP_UP_AUTH","BLOCK_CARD","BLOCK_ALL_CARDS",
 "GENERATE_REPORT","CREATE_CASE","FILE_REPORT","ESCALATE_TO_ANALYST","CLOSE_NO_FRAUD"}
AUTO={"ALLOW_TRANSACTION","MONITOR_CARD","MONITOR_CONNECTED_CARDS","WARN_CUSTOMER",
 "VERIFY_WITH_CUSTOMER","STEP_UP_AUTH","GENERATE_REPORT","CREATE_CASE","ESCALATE_TO_ANALYST","CLOSE_NO_FRAUD"}
VERDICTS={"fraud","legitimate","uncertain"}
TIPOFF=re.compile(r"\b(sar|suspicious activity report|file[d]? a report|regulator|fincen|"
                  r"reported to the authorit|law enforcement)\b",re.I)

def dig(d,*names):
    """first present key among names, searched one level deep too"""
    if not isinstance(d,dict): return None,False
    for n in names:
        if n in d: return d[n],True
    for v in d.values():
        if isinstance(v,dict):
            for n in names:
                if n in v: return v[n],True
    return None,False

def as_list(v):
    if v is None: return []
    if isinstance(v,str): return [v]
    if isinstance(v,dict): v=list(v.values())
    out=[]
    for x in (v if isinstance(v,list) else [v]):
        if isinstance(x,dict):
            x=x.get("action") or x.get("name") or x.get("type") or ""
        if isinstance(x,str) and x: out.append(x)
    return out

def norm_ids(v):
    out=[]
    if v is None: return out
    if isinstance(v,(str,int,float)): v=[v]
    if isinstance(v,dict): v=list(v.values())
    for x in (v if isinstance(v,list) else []):
        if isinstance(x,dict): x=x.get("id") or x.get("txn_id") or x.get("transaction_id") or ""
        if isinstance(x,float) and float(x).is_integer(): x=int(x)
        s=str(x).strip()
        if s and s.lower() not in ("none","null","nan"): out.append(s)
    return out

def grade_case(cid,ans,meta,tx):
    """returns list of (cls, code, message)"""
    F=[]
    hard=lambda c,m: F.append(("hard",c,m))
    pol =lambda c,m: F.append(("policy",c,m))
    warn=lambda c,m: F.append(("warning",c,m))
    if not isinstance(ans,dict):
        hard("not_an_object","answer file is not a JSON object"); return F

    case,_=dig(ans,"case","investigation","assessment")
    case=case if isinstance(case,dict) else ans
    nba,_=dig(ans,"next_best_actions","next_best_action","actions")
    sar,_=dig(ans,"sar","SAR","suspicious_activity_report")

    # ---------------- hard ----------------
    got_cid,_=dig(ans,"case_id","caseId")
    if str(got_cid or "").upper()!=cid.upper():
        hard("case_id_mismatch",f"case_id is {got_cid!r}, expected {cid}")
    verdict,hasv=dig(case,"verdict","final_verdict","decision","disposition")
    verdict=str(verdict or "").strip().lower()
    if not hasv: hard("missing_verdict","no verdict field")
    elif verdict not in VERDICTS: hard("bad_verdict",f"verdict {verdict!r} is not one of {sorted(VERDICTS)}")
    prob,hasp=dig(case,"fraud_probability","probability","p_fraud")
    try: prob=float(prob)
    except (TypeError,ValueError): prob=None
    if not hasp or prob is None: hard("missing_probability","no numeric fraud_probability")
    elif not 0.0<=prob<=1.0: hard("bad_probability",f"fraud_probability {prob} is outside 0..1")
    pattern,hasq=dig(case,"pattern","fraud_pattern")
    if not hasq or not str(pattern or "").strip(): hard("missing_pattern","no pattern field")
    aff=norm_ids(dig(case,"affected_txn_ids","affected_transaction_ids","affected_transactions")[0])
    first=norm_ids(dig(case,"first_suspicious_txn_id","first_fraud_txn_id")[0])
    exposure,hase=dig(case,"exposure_usd","exposure")
    try: exposure=float(exposure)
    except (TypeError,ValueError): exposure=None
    if not hase or exposure is None: hard("missing_exposure","no numeric exposure_usd")
    elif exposure<0: hard("negative_exposure",f"exposure_usd is {exposure}")

    raw_fin=dig(nba,"final","after_evidence","final_actions")[0] if isinstance(nba,dict) else nba
    fin=[a.strip().upper() for a in as_list(raw_fin)]
    routes={}
    for x in (raw_fin if isinstance(raw_fin,list) else []):
        if isinstance(x,dict):
            an=str(x.get("action") or x.get("name") or "").strip().upper()
            rt=x.get("route") or x.get("approval_route") or x.get("approval")
            if an and rt: routes[an]=str(rt).strip().upper()
    ini=[a.strip().upper() for a in as_list(dig(nba,"initial","before_evidence","initial_actions")[0] if isinstance(nba,dict) else None)]
    if not fin: hard("missing_final_actions","next_best_actions.final is empty or absent")
    for a in set(fin+ini)-ACTIONS:
        hard("unknown_action",f"{a!r} is not an action in Fraud Policy v1.0")

    # internal consistency
    if verdict=="legitimate":
        if aff: hard("inconsistent_affected","verdict legitimate but affected_txn_ids is non-empty")
        if exposure not in (None,0) and exposure>0: hard("inconsistent_exposure","verdict legitimate but exposure_usd > 0")
    if verdict=="fraud" and not aff:
        hard("inconsistent_affected","verdict fraud but affected_txn_ids is empty")
    if first and aff and first[0] not in aff:
        hard("first_not_in_affected",f"first_suspicious_txn_id {first[0]} is not in affected_txn_ids")
    if prob is not None and verdict=="fraud" and prob<0.5:
        warn("low_prob_fraud",f"verdict fraud with probability {prob}")
    if prob is not None and verdict=="legitimate" and prob>0.5:
        warn("high_prob_legit",f"verdict legitimate with probability {prob}")

    # ids / arithmetic, only when transaction data was supplied
    if tx:
        for t in aff+first:
            if t not in tx: hard("unknown_txn_id",f"{t} does not exist in the dataset")
        known=[t for t in aff if t in tx]
        if known and exposure is not None:
            calc=round(sum(tx[t]["amt"] for t in known),2)
            if abs(calc-exposure)>max(0.02,calc*0.005):
                hard("exposure_mismatch",f"exposure_usd {exposure} != sum of affected amounts {calc}")
        if known and first and first[0] in tx:
            earliest=min(known,key=lambda t:tx[t]["dt"])
            if earliest!=first[0]:
                hard("first_not_earliest",f"first_suspicious_txn_id {first[0]} is not the earliest affected txn ({earliest})")
        fl=meta.get("flagged_txn_id")
        if verdict=="fraud" and fl and aff and fl not in aff:
            warn("flagged_not_affected",f"flagged txn {fl} is not among affected_txn_ids")

    # ---------------- policy ----------------
    sar_file=None
    if isinstance(sar,dict):
        v,_=dig(sar,"file","required","filed","should_file")
        if isinstance(v,bool): sar_file=v
        elif isinstance(v,str): sar_file=v.strip().lower() in ("yes","true","required","file")
    elif isinstance(sar,bool): sar_file=sar
    in_final="FILE_REPORT" in fin
    if sar_file is not None and sar_file!=in_final:
        pol("sar_disagrees_with_actions",f"sar.file={sar_file} but FILE_REPORT {'is' if in_final else 'is not'} in final actions")

    blocks={"BLOCK_CARD","BLOCK_ALL_CARDS"}&set(fin)
    if blocks and exposure is not None:
        want="L2" if exposure>2500 else "L1"
        for b in sorted(blocks):
            route=routes.get(b) or (str(dig(ans,"approval_route","expected_route_for_block")[0] or "").upper() or None)
            if b=="BLOCK_ALL_CARDS": want_b="L2"
            else: want_b=want
            if route and route!=want_b:
                pol("wrong_route",f"{b} at exposure {exposure} needs route {want_b}, answer says {route}")
            elif not route:
                warn("no_route_stated",f"{b} is recommended but no approval route is stated (expected {want_b})")
    for a,rt in routes.items():
        if a in AUTO and rt not in ("AUTO","",None):
            warn("route_on_auto_action",f"{a} is an auto action but is routed {rt}")
    if "BLOCK_ALL_CARDS" in fin:
        pol("r10_block_all","R10: BLOCK_ALL_CARDS requires two confirmed cards or confirmed credential compromise - state the evidence")
    denied = meta.get("trigger_type")=="customer_report"
    if prob is not None and blocks and prob<0.70 and not denied:
        if not ({"VERIFY_WITH_CUSTOMER","STEP_UP_AUTH"} & set(ini)):
            pol("r1_block_on_weak_signal",f"R1: block recommended at probability {prob} on a single signal with no prior verification step")
    if verdict=="uncertain" and exposure is not None and exposure>500:
        if "ESCALATE_TO_ANALYST" not in fin:
            pol("r8_no_escalation",f"R8: uncertain with exposure {exposure} requires ESCALATE_TO_ANALYST")
    if str(pattern or "").strip().lower() in ("undocumented","unknown","novel","other"):
        missing={"CREATE_CASE","FILE_REPORT","ESCALATE_TO_ANALYST"}-set(fin)
        if missing: pol("r9_undocumented",f"R9: undocumented pattern also requires {sorted(missing)}")
    # 3a: case vs report
    if verdict=="fraud":
        conn=norm_ids(dig(case,"connected_card_ids","connected_cards","linked_cards")[0])
        need = (exposure is not None and exposure>1000) or bool(conn)
        if need and not in_final:
            pol("r3a_report_required","3a: exposure over $1,000 or a shared element is present, so FILE_REPORT is required")
        if not need and in_final and (exposure is not None and exposure<=1000) and not conn:
            warn("r3a_report_not_required","FILE_REPORT filed although 3a does not appear to require it")
        if "CREATE_CASE" not in fin:
            pol("r3a_no_case","3a: a report always has a case behind it - CREATE_CASE is missing")
    # tip-off: nothing the customer sees may mention a report
    ev,_=dig(ans,"evidence_requests","evidence_request")
    blob=json.dumps(ev) if ev is not None else ""
    if TIPOFF.search(blob):
        pol("tipoff","evidence_requests text mentions a report or regulator; the customer must never see this")
    narr,_=dig(sar if isinstance(sar,dict) else {},"narrative","text")
    if narr and len(str(narr).split())<40:
        warn("thin_sar_narrative",f"SAR narrative is only {len(str(narr).split())} words; it must stand alone")
    # 3b: did the recommendation actually move?
    if ini and fin:
        changed,_=dig(nba if isinstance(nba,dict) else {},"what_changed","change","delta")
        if set(ini)!=set(fin) and not str(changed or "").strip():
            pol("r3b_no_reason","3b: final actions differ from initial but what_changed is empty")
    return F

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--teams-dir",required=True)
    ap.add_argument("--case-pack",required=True)
    ap.add_argument("--transactions",help="optional: enables ID existence and arithmetic checks")
    ap.add_argument("--answer-key",help="organizers only: adds accuracy against the key")
    ap.add_argument("--out",default="report.json")
    a=ap.parse_args()
    pack={r["case_id"]:r for r in csv.DictReader(open(a.case_pack,newline=""))}
    if not pack: sys.exit("case pack is empty")
    tx={}
    if a.transactions:
        with open(a.transactions,newline="") as fh:
            rd=csv.DictReader(fh)
            for r in rd:
                tx[r["TransactionID"]]={"amt":float(r["TransactionAmt"]),"dt":int(r["TransactionDT"])}
        print(f"loaded {len(tx):,} transactions for ID and arithmetic checks")
    key=json.load(open(a.answer_key)) if a.answer_key else None
    report={"case_pack":os.path.basename(a.case_pack),"n_cases":len(pack),"teams":{}}
    for team in sorted(os.listdir(a.teams_dir)):
        tdir=os.path.join(a.teams_dir,team)
        if not os.path.isdir(tdir): continue
        cdir=os.path.join(tdir,"cases")
        if not os.path.isdir(cdir): cdir=tdir
        per={}; counts=defaultdict(int)
        for cid in pack:
            p=os.path.join(cdir,f"{cid}.json")
            if not os.path.exists(p):
                per[cid]={"findings":[["hard","missing_answer_file",f"{cid}.json not found"]]}
                counts["hard"]+=1; continue
            try: ans=json.load(open(p,encoding="utf-8"))
            except Exception as e:
                per[cid]={"findings":[["hard","unparseable",f"{e}"]]}
                counts["hard"]+=1; continue
            f=grade_case(cid,ans,pack[cid],tx)
            for cls,_,_ in f: counts[cls]+=1
            per[cid]={"findings":[list(x) for x in f]}
            if key and cid in key:
                v,_=dig(ans.get("case",ans),"verdict","final_verdict","decision")
                per[cid]["verdict_correct"]=str(v or "").lower()==key[cid]["truth"]
        t={"cases_answered":sum(1 for c in per.values() if not any(x[1]=="missing_answer_file" for x in c["findings"])),
           "hard":counts["hard"],"policy":counts["policy"],"warning":counts["warning"],
           "clean":sum(1 for c in per.values() if not [x for x in c["findings"] if x[0]!="warning"]),
           "cases":per}
        if key: t["verdicts_correct"]=sum(1 for c in per.values() if c.get("verdict_correct"))
        report["teams"][team]=t
        extra=f" verdicts {t['verdicts_correct']}/{len(pack)}" if key else ""
        print(f"{team:28} answered {t['cases_answered']:>2}/{len(pack)}  hard {t['hard']:>3}  "
              f"policy {t['policy']:>3}  warn {t['warning']:>3}  clean {t['clean']:>2}/{len(pack)}{extra}")
    json.dump(report,open(a.out,"w"),indent=1)
    print(f"\nwrote {a.out}")

if __name__=="__main__": main()
