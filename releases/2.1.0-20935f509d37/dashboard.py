#!/usr/bin/env python3
from pathlib import Path
import hashlib, io, json, sys, zipfile
sys.path.insert(0, str(Path(__file__).resolve().parent))
import streamlit as st
import medforge_core as mf
mf.mkdirs(); mf.init_db()
st.set_page_config(page_title="MedForge", page_icon="🧠", layout="wide")
st.title("MedForge")
st.caption("Learn a topic. Build a study pack. Keep your sources.")
with st.sidebar:
    st.write("**Local AI · version 2.1**")
    st.caption("Your PDFs and generated content stay on this Mac. Online research sends the topic to search providers.")
    mf.set_offline(st.toggle("Offline: use saved evidence", value=mf.OFFLINE))
    st.caption("First-time setup needs internet to download packages and local models.")

def show_error(e):
    st.error(str(e))
    st.caption("Completed steps remain saved. Retry the same topic to resume.")

def grade_review(g):
    item=st.session_state.get("review_current")
    if not item: return
    try:
        res=mf.review_card(item,g,scheduler=st.session_state.get("review_scheduler") or mf.DEFAULT_SCHEDULER)
        interval=res["due_in_days"]
        when=f"{interval*1440:.0f} min" if interval<1 else f"{interval:g} day(s)"
        msg=f"Graded {g}/5 · {res['state']} · next in {when} · ease {res['ease_factor']:.2f}"
        if res.get("weakness_recorded"): msg+=" · added to weaknesses"
        if res.get("weakness_resolved"): msg+=" · weakness resolved"
        st.session_state["review_flash"]=msg
    except Exception as e:
        st.session_state["review_flash"]="Could not grade card: "+str(e)
    st.session_state["review_reveal"]=False

def show_pack(out, key):
    st.success("Draft pack ready for source review")
    st.code(str(out), language=None)
    report = out / "evidence-report.json"
    if report.exists():
        scan = json.loads(report.read_text())
        st.caption(f"Citation-label scan: {scan['cited_lines']}/{scan['scanned_lines']} lines. Source support still needs review.")
    data=io.BytesIO()
    with zipfile.ZipFile(data,"w",zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.iterdir()):
            if f.is_file() and not f.is_symlink() and f.stat().st_size < 40_000_000:
                z.write(f, f.name)
    st.download_button("Download complete pack",data.getvalue(),file_name=out.parent.name+"-"+out.name+".zip",mime="application/zip",key=key)

tabs=st.tabs(["PRODUCT","STUDY","REVIEW","LEARNER","TUTOR","ASSESSMENTS","ASK","CURRICULUM","TEXTBOOKS","EVIDENCE","LIBRARY","HISTORY","STATUS"])
with tabs[0]:
    topic=st.text_input("What do you want to learn?",placeholder="cardiac cycle",max_chars=250)
    st.caption("PDFs + PubMed + authoritative web pages → study guide, workbook, cards, quiz and scripts.")
    if st.button("Create product",type="primary",width="stretch"):
        if not topic.strip(): st.warning("Enter one topic.")
        else:
            with st.status("Researching and generating. A full pack can take several minutes on an M1.",expanded=True) as progress:
                try:
                    out=mf.build_product(topic.strip(),include_web=not mf.OFFLINE,open_folder=False)
                    st.session_state["last_pack"]=str(out)
                    progress.update(label="Draft pack created",state="complete")
                except Exception as e:
                    progress.update(label="Saved at the last completed step",state="error"); show_error(e)
    if st.session_state.get("last_pack"): show_pack(Path(st.session_state["last_pack"]),"last-pack")
with tabs[1]:
    topic=st.text_input("Study topic",key="study_topic",max_chars=250)
    st.caption("A guided 20-minute mission. Log your self-score when you finish to build mastery and weakness tracking.")
    if st.button("Start study mission") and topic.strip():
        with st.spinner("Preparing your mission..."):
            try: st.session_state["mission"]=mf.study(topic.strip())
            except Exception as e: show_error(e)
    if st.session_state.get("mission"):
        st.markdown(st.session_state["mission"])
        st.subheader("Log your session result")
        sc1,sc2=st.columns(2)
        score=sc1.slider("Self-score (rubric out of 10)",0,10,5,key="study_score",help="Matches the rubric at the end of your mission.")
        minutes=sc2.number_input("Minutes spent",0,240,20,key="study_minutes")
        wconcept=st.text_input("Concept you missed (optional)",key="study_weak_concept",placeholder="e.g. isovolumetric contraction")
        wmiscon=st.text_input("What was the misconception?",key="study_weak_misconception")
        wsev=st.selectbox("Weakness severity",list(mf.WEAKNESS_SEVERITY),1,key="study_weak_severity")
        if st.button("Log session result",type="primary"):
            weak=[{"concept":wconcept,"misconception":wmiscon,"severity":wsev}] if wconcept.strip() else []
            try:
                res=mf.log_study_result((st.session_state.get("study_topic") or topic).strip(),score,duration_seconds=int(minutes)*60,notes="Logged from dashboard",weaknesses=weak)
                m=res["mastery"]
                st.success(f"Logged {res['score']:.0f}/100 for '{res['topic_id']}'. Mastery {m['mastery_score']}/100 across {m['total_attempts']} attempts.")
            except Exception as e: show_error(e)
    st.subheader("Mastery & weaknesses")
    snap=None
    try: snap=mf.learner_snapshot()
    except Exception as e: show_error(e)
    if snap:
        s=snap["summary"]
        m1,m2,m3,m4,m5=st.columns(5)
        m1.metric("Topics studied",s["topics_studied"])
        m2.metric("Average mastery",f"{s['avg_mastery']:.0f}/100")
        m3.metric("Open weaknesses",s["open_weaknesses"])
        m4.metric("Sessions logged",s["sessions_total"])
        m5.metric("Cards due",s.get("cards_due",0))
        if snap["mastery"]:
            st.markdown("**Topic mastery** (weakest first)")
            st.dataframe(snap["mastery"],width="stretch",hide_index=True)
        else:
            st.info("Log a study session to start tracking mastery.")
        if snap["weaknesses"]:
            with st.expander(f"Unresolved weaknesses ({s['open_weaknesses']})"):
                st.dataframe(snap["weaknesses"],width="stretch",hide_index=True)
        if snap["recent_sessions"]:
            with st.expander("Recent sessions"):
                st.dataframe(snap["recent_sessions"],width="stretch",hide_index=True)
with tabs[2]:
    st.caption("Spaced repetition (SM-2 or FSRS). Your packs schedule their cards here; grade 0-5 to reschedule.")
    if st.session_state.get("review_flash"): st.success(st.session_state.pop("review_flash"))
    sr=None
    try: sr=mf.spaced_repetition_snapshot()
    except Exception as e: show_error(e)
    if sr:
        s=sr["summary"]; a=sr["analytics"]
        r1,r2,r3,r4=st.columns(4)
        r1.metric("Due now",s["due_cards"])
        r2.metric("Scheduled cards",s["scheduled_cards"])
        r3.metric("New / learning",s["new_cards"]+s["learning_cards"])
        r4.metric("In review",s["review_cards"])
        q1,q2,q3,q4=st.columns(4)
        q1.metric("Reviews today",s["reviews_today"])
        q2.metric("Retention (30 d)",("%.0f%%"%s["retention"]) if s["retention"] is not None else "—")
        q3.metric("Day streak",s["streak_days"])
        q4.metric("Leeches",s["leeches"])
        if s["next_due_at"]: st.caption("Next card due "+s["next_due_at"][:16].replace("T"," ")+" UTC")
        if sr["due"]:
            card=sr["due"][0]
            st.session_state["review_current"]=card["item_id"]
            st.markdown("**Review** · "+("weak topic · " if card["topic_weak"] else "")+card["topic_id"]+" · "+str(s["due_cards"])+" due")
            st.markdown("#### "+(card["question"] or card["item_id"]))
            if st.session_state.get("review_reveal"):
                st.markdown(card["answer"] or "_(answer text not found in the saved pack)_")
                if card["sources"]: st.caption("Pack sources: "+card["sources"])
                st.caption("Grade — 0 blackout · 1-2 fail · 3 pass · 4 good · 5 easy")
                gcols=st.columns(6)
                for g in range(6):
                    if gcols[g].button(str(g),key=f"review_grade_{g}",help=["Blackout","Wrong, familiar","Wrong","Pass with effort","Good recall","Easy recall"][g]):
                        grade_review(g); st.rerun()
                with st.form("review_keyboard",clear_on_submit=True):
                    typed=st.text_input("Keyboard: type a grade 0-5 and press Enter",key="review_typed")
                    submitted=st.form_submit_button("Submit grade")
                if submitted and typed.strip():
                    try:
                        g=int(typed.strip()); assert 0<=g<=5
                    except (ValueError,AssertionError): st.warning("Enter a whole number 0-5.")
                    else: grade_review(g); st.rerun()
            else:
                st.caption("Recall the answer out loud, then reveal it.")
                if st.button("Show answer",type="primary"): st.session_state["review_reveal"]=True; st.rerun()
        else:
            st.info("No cards are due right now. Grade some cards or come back when the next interval elapses.")
        with st.expander(f"Browse due cards ({len(sr['due'])})"):
            st.dataframe([{"Topic":c["topic_id"],"Weak":"yes" if c["topic_weak"] else "","Question":c["question"],"Answer":c["answer"],"State":c["state"],"Reps":c["repetition_count"],"Ease":c["ease_factor"],"Due":c["due_date"][:10]} for c in sr["due"]],width="stretch",hide_index=True)
        if any(d["reviews"] for d in a["reviews_by_day"][-14:]):
            st.markdown("**Reviews per day** (last 14 days)")
            st.bar_chart([{"day":d["date"][5:],"reviews":d["reviews"]} for d in a["reviews_by_day"][-14:]],x="day",y="reviews")
        if a["leeches"]:
            with st.expander(f"Leech cards ({len(a['leeches'])}) — {a['leech_threshold']}+ lapses; rewrite these questions"):
                st.dataframe([{"Topic":x["topic_id"],"Question":x["question"],"Lapses":x["lapses"],"Reviews":x["reviews"],"Last reviewed":x["last_reviewed_at"][:16]} for x in a["leeches"]],width="stretch",hide_index=True)
        packs=sorted(mf.PRODUCTS.glob("*/v*/flashcards.csv"),key=lambda p:p.stat().st_mtime,reverse=True)
        with st.expander(f"Schedule flashcards & scheduling options ({len(packs)} packs)"):
            st.selectbox("Scheduler for new grades",list(mf.SCHEDULERS),key="review_scheduler",help="sm2 = classic SM-2; fsrs = FSRS-4.5 (experimental, fills stability/difficulty).")
            if not packs:
                st.caption("Generate a product first; its flashcards are scheduled automatically.")
            else:
                pack_labels={p.parent.parent.name+" · "+p.parent.name:p for p in packs}
                pack=st.selectbox("Pack",list(pack_labels),key="review_pack")
                if st.button("Schedule pack cards"):
                    try:
                        res=mf.import_flashcards(pack_labels[pack].parent.parent.name,pack_labels[pack])
                        st.session_state["review_flash"]=f"{res['imported']} new cards scheduled — {res['scheduled']} total for {res['topic_id']}."
                        st.rerun()
                    except Exception as e: show_error(e)
with tabs[3]:
    st.caption("Recency-weighted learner-model estimate (P6) from your own attempts — an inspectable estimate, not a validated psychometric score.")
    try:
        summ=mf.learner_summary(limit=6)
        s=summ["summary"]
        c1,c2,c3,c4,c5,c6=st.columns(6)
        c1.metric("Topics tracked",s["topics_tracked"])
        c2.metric("Avg mastery",f"{s['avg_mastery']:.0f}%")
        c3.metric("Events",s["events_total"])
        c4.metric("Known weaknesses",s["known_weaknesses"])
        c5.metric("Possible (low confidence)",s["possible_weaknesses"])
        c6.metric("Calibration mismatches",s["calibration_mismatches"])
        st.caption(f"Model {summ['model_version']} · half-life {summ['half_life_days']:g} days · overdue cards {s['overdue_cards']}")
        if summ["strongest"]: st.markdown("**Strongest areas**"); st.dataframe(summ["strongest"],width="stretch",hide_index=True)
        if summ["weakest"]: st.markdown("**Weakest areas**"); st.dataframe(summ["weakest"],width="stretch",hide_index=True)
        if summ["improving"]: st.markdown("**Recently improving**"); st.dataframe(summ["improving"],width="stretch",hide_index=True)
        if summ["declining"]: st.markdown("**Recently declining**"); st.dataframe(summ["declining"],width="stretch",hide_index=True)
        if summ["confidence_mismatches"]:
            st.markdown("**Confidence vs performance mismatches** (|gap| ≥ 0.2)")
            st.dataframe(summ["confidence_mismatches"],width="stretch",hide_index=True)
        st.markdown("**Recommended study priorities**")
        st.dataframe([{"Topic":p["topic"],"Priority":p["priority"],"Mastery":p["mastery_percent"],"Weakness":p["components"]["weakness"],"Uncertainty":p["components"]["uncertainty"],"Overdue":p["components"]["overdue"],"Recent failure":p["components"]["recent_failure"],"Prereq impact":p["components"]["prerequisite_impact"]} for p in summ["priorities"]],width="stretch",hide_index=True)
        topics=[p["topic"] for p in mf.study_priority(limit=100)["items"]]
        if topics:
            st.subheader("Topic detail")
            pick=st.selectbox("Topic",topics,key="learner_topic")
            mst=mf.get_mastery(pick); conf=mf.get_confidence(pick); rec=mf.get_recent_performance(pick)
            risks=mf.get_prerequisite_risks(pick)
            if mst:
                d1,d2,d3,d4=st.columns(4)
                d1.metric("Mastery estimate",f"{mst['mastery_percent']:.0f}%")
                d2.metric("Evidence count",mst["evidence_count"])
                d3.metric("Uncertainty",f"{mst['uncertainty']:.2f}")
                d4.metric("Consistency","—" if mst["consistency"] is None else f"{mst['consistency']:.2f}")
                st.caption("Recent performance: "+("—" if rec["recent_performance"] is None else f"{rec['recent_performance']:.2f}")+" · historical: "+("—" if rec["historical_performance"] is None else f"{rec['historical_performance']:.2f}")+" · trend: "+("—" if rec["trend"] is None else f"{rec['trend']:+.2f}")+" · last studied: "+str(mst["last_attempt_at"] or "—"))
                if conf and conf["has_confidence_data"]:
                    st.caption(f"Confidence {conf['confidence_estimate']:.2f} vs mastery {conf['mastery']:.2f} → gap {conf['calibration_gap']:+.2f} ({conf['direction']})" + (" · MISMATCH" if conf["mismatch"] else ""))
                else: st.caption("No learner confidence observations yet — calibration is not fabricated.")
                wks=[w for w in mf.get_weaknesses() if w["topic_id"]==mst["mastery_key"]]
                st.markdown("**Weakness state:** "+("none open" if not wks else "; ".join(f"{w['severity']} ({'possible/low-confidence' if w['low_confidence'] else 'known'}), score {w['weakness_score']}" for w in wks)))
                if risks.get("risks"):
                    st.markdown("**Prerequisites**")
                    st.dataframe([{"Prerequisite":r["title"],"Mastery":r["mastery_percent"],"Evidence":r["evidence_count"],"Weak":"yes" if r["weak"] else "","Impact":r["impact"],"Type":r["relationship_type"]} for r in risks["risks"]],width="stretch",hide_index=True)
                    if risks["weak_count"]: st.caption(f"⚠ {risks['weak_count']} weak prerequisite(s) — exposed as a risk signal, not subtracted from this topic's mastery.")
                if st.button("Recalculate this topic from history",key="learner_recalc"):
                    r=mf.recalculate_mastery(pick)
                    st.success("Materialized state matches recalculation." if r["matches_stored"] else "MISMATCH — stored state differs from history!")
    except Exception as e: show_error(e)
with tabs[4]:
    st.caption("Interactive adaptive tutor (P7) — P6 learner state + P4 evidence. Sessions are saved, so a refresh resumes the same session.")
    try:
        c1,c2,c3,c4=st.columns([2,1,1,1])
        tutor_topic=c1.text_input("Topic (blank = recommended)",key="tutor_topic",max_chars=200)
        tutor_mode=c2.selectbox("Mode",["auto"]+list(mf.TUTOR_SESSION_MODES),key="tutor_mode")
        tutor_goal=c3.selectbox("Session goal",list(mf.TUTOR_SESSION_GOALS),key="tutor_goal")
        if c4.button("Start session",key="tutor_start"):
            started=mf.start_tutor_session(tutor_topic.strip() or None,mode=None if tutor_mode=="auto" else tutor_mode,goal=tutor_goal)
            st.session_state["tutor_session_id"]=started["session_id"]
            st.rerun()
        if st.session_state.get("tutor_session_id") is None:
            try: st.session_state["tutor_session_id"]=mf.resume_tutor_session()["session"]["tutor_session_id"]
            except Exception: pass
        sid=st.session_state.get("tutor_session_id")
        if sid is None:
            st.info("No tutor session yet — choose a topic (or leave blank for the recommended one) and start.")
        else:
            state=mf.get_tutor_state(sid); sess=state["session"]; prog=state["progress"]
            st.markdown("**Objective:** "+sess["session_objective"])
            m1,m2,m3,m4,m5=st.columns(5)
            m1.metric("Interactions",f"{prog['interactions']}/{prog['target_interactions']}")
            m2.metric("Correct",prog["correct"]); m3.metric("Incorrect",prog["incorrect"])
            m4.metric("Difficulty",prog["difficulty"]); m5.metric("Status",sess["status"])
            st.caption(f"Topic {sess['topic']} · mode {sess['mode']} · stage {sess['stage']} · evidence {len(state['evidence_rows'])} item(s) · {state['assessment']['status']}")
            if state["assessment"]["status"]!="SUPPORTED":
                st.warning(state["policy"]["message"])
            if sess.get("current_explanation"):
                st.markdown("**Teaching**"); st.markdown(sess["current_explanation"])
            if sess["status"] in ("completed","aborted"):
                st.markdown("**Session summary**"); st.json(mf.get_tutor_summary(sid))
            else:
                q=state["question"]
                if q is None:
                    if st.button("Prepare next step",key="tutor_next"):
                        mf.next_tutor_step(sid); st.rerun()
                else:
                    st.markdown(f"**Question {prog['question_number']}** ({q['question_type']}, difficulty {q['difficulty']}): {q['prompt']}")
                    if q["options"]:
                        answer=st.radio("Choose an option",q["options"],key=f"tutor_opts_{q['item_id']}")
                    else:
                        answer=st.text_area("Your answer",key=f"tutor_ans_{q['item_id']}",max_chars=2000)
                    conf=st.slider("How confident are you?",0.0,1.0,0.5,0.05,key=f"tutor_conf_{q['item_id']}")
                    b1,b2=st.columns(2)
                    if b1.button("Submit answer",key="tutor_submit"):
                        st.session_state["tutor_last"]=mf.submit_answer(sid,answer,confidence=conf)
                        st.rerun()
                    if b2.button("End session",key="tutor_end"):
                        mf.complete_tutor_session(sid,reason="learner ended the session"); st.rerun()
                last=st.session_state.get("tutor_last") or {}
                if last.get("retryable"):
                    st.warning("Grading needs the local model; your answer is saved — retry when it is available.")
                    if st.button("Retry grading",key="tutor_retry"): st.rerun()
                grade=last.get("grade") or {}
                if grade.get("grading_status")=="graded":
                    if grade.get("correctness")=="correct": st.success(f"Correct ({grade.get('score')}). "+str(grade.get("explanation") or ""))
                    elif grade.get("correctness")=="partial": st.info(f"Partially correct ({grade.get('score')}). "+str(grade.get("explanation") or ""))
                    else: st.error(f"Incorrect ({grade.get('score')}). "+str(grade.get("explanation") or ""))
                    if grade.get("missing_key_points"): st.caption("Missing: "+"; ".join(grade["missing_key_points"][:3]))
                adaptation=((last.get("adaptation") or {}).get("reason"))
                if adaptation: st.caption("Next: "+str(adaptation))
            with st.expander("Evidence used (private source excerpts)"):
                for ev in state["evidence_rows"]:
                    st.caption(f"{ev.get('locator') or ev.get('chunk_id') or ''} — {(ev.get('excerpt') or '')[:220]}")
            if st.button("Recalculate learner state from history",key="tutor_recalc"):
                r=mf.recalculate_mastery(sess["topic"]); st.success("Materialized state matches recalculation." if r["matches_stored"] else "MISMATCH — stored state differs from history!")
    except Exception as e: show_error(e)
with tabs[5]:
    st.caption("Question-level assessments (P8) — versioned item bank, deterministic blueprints, PRACTICE/EXAM/REVIEW. Sessions are saved, so a reload resumes the same assessment.")
    try:
        blueprints=mf.list_blueprints()
        bp_options={f"{b['title']} — {b['blueprint_id']} ({b['item_count']} items)":b["blueprint_id"] for b in blueprints}
        c1,c2,c3,c4,c5=st.columns([2,2,1,1,1])
        scope_target=c1.text_input("Scope (topic/seminar/week title or node id) — used without a blueprint",key="assess_scope",max_chars=200)
        bp_label=c2.selectbox("Blueprint",["(ad hoc scope)"]+list(bp_options),key="assess_bp")
        assess_mode=c3.selectbox("Mode",list(mf.ASSESSMENT_MODES),key="assess_mode")
        assess_count=c4.number_input("Items",1,100,10,key="assess_count")
        assess_minutes=c5.number_input("Minutes (0 = none)",0,300,0,key="assess_minutes")
        if st.button("Create assessment",type="primary",key="assess_create"):
            try:
                if bp_label!="(ad hoc scope)":
                    started=mf.create_assessment(blueprint_id=bp_options[bp_label],mode=assess_mode,item_count=int(assess_count),time_limit_minutes=int(assess_minutes) or None)
                else:
                    node=mf._resolve_scope_node(scope_target.strip()) if scope_target.strip() else None
                    if node is None:
                        st.warning("No curriculum node matches that scope — import a syllabus first or pick a blueprint.")
                        started=None
                    else:
                        scope_type=(node["node_type"].lower() if node["node_type"].lower() in ("topic","seminar","week","subject") else "custom")
                        started=mf.create_assessment(scope_type=scope_type,scope_node_id=node["id"],mode=assess_mode,item_count=int(assess_count),time_limit_minutes=int(assess_minutes) or None,title=f"{node['node_type']}: {node['title']}")
                if started and started.get("created"):
                    st.session_state["assess_id"]=started["assessment_id"]
                    st.rerun()
                elif started:
                    st.error(started.get("error") or "Could not create the assessment.")
            except Exception as e: show_error(e)
        recent=mf.list_assessments(limit=5)
        if recent:
            st.caption("Recent assessments")
            st.dataframe([{"ID":r["assessment_id"],"Title":r["title"],"Mode":r["mode"],"Status":r["status"],"Answered":r["progress"]["answered"],"Items":r["item_count"],"%":r["percentage"]} for r in recent],width="stretch",hide_index=True)
        aid=st.session_state.get("assess_id")
        if aid is None:
            try:
                resumed=mf.resume_assessment()
                if resumed.get("resumed"): aid=resumed["assessment_id"]
            except Exception: pass
        if aid is None:
            st.info("No assessment yet — choose a scope or blueprint and create one.")
        else:
            st.session_state["assess_id"]=aid
            state=mf.get_assessment_state(aid)
            prog=state["progress"]
            m1,m2,m3,m4,m5=st.columns(5)
            m1.metric("Status",state["status"]); m2.metric("Answered",f"{prog['answered']}/{state['item_count']}")
            m3.metric("Flagged",prog["flagged"]); m4.metric("Correct",prog["correct"])
            remaining=state.get("remaining_seconds")
            m5.metric("Remaining",("—" if remaining is None else f"{remaining/60:.1f} min"))
            st.caption(f"{state['title']} · mode {state['mode']} · question {state['current_order']} of {state['item_count']} · {state['feedback_policy']['mode']}")
            if state["status"] in ("created","active"):
                current=state.get("current_item")
                if current is None:
                    st.info("Question ready — press Next to present it.")
                    if st.button("Next question",key="assess_next0"): mf.get_current_item(aid); st.rerun()
                else:
                    st.markdown(f"**Question {current['question_order']}** ({current['item_type']}, difficulty {current['difficulty']}): {current['stem']}")
                    choices=current.get("choices") or []
                    key_suffix=f"{aid}_{current['question_order']}"
                    if current["item_type"]=="MCQ_MULTI" and choices:
                        answer=st.multiselect("Select all that apply",[c["text"] for c in choices],key=f"assess_multi_{key_suffix}")
                    elif choices:
                        answer=st.radio("Choose an option",[c["text"] for c in choices],key=f"assess_opt_{key_suffix}")
                    else:
                        answer=st.text_area("Your answer",key=f"assess_ans_{key_suffix}",max_chars=2000)
                    conf=st.slider("How confident are you?",0.0,1.0,0.5,0.05,key=f"assess_conf_{key_suffix}")
                    b1,b2,b3,b4=st.columns(4)
                    if b1.button("Submit answer",type="primary",key="assess_answer"):
                        st.session_state["assess_last"]=mf.submit_assessment_answer(aid,answer,confidence=conf)
                        st.rerun()
                    if b2.button("Flag for review",key="assess_flag"):
                        mf.flag_item(aid,order=current["question_order"],reason="marked during the assessment"); st.rerun()
                    if b3.button("Previous",key="assess_prev"): mf.previous_item(aid); st.rerun()
                    if b4.button("Next",key="assess_next"): mf.next_item(aid); st.rerun()
                    last=st.session_state.get("assess_last") or {}
                    if last.get("retryable"):
                        st.warning("Grading needs the local model; your answer is saved — retry without losing the attempt.")
                        if st.button("Retry grading",key="assess_retry"): mf.retry_pending_grading(aid); st.rerun()
                    elif last.get("feedback_withheld"):
                        st.info("Answer saved. Exam feedback is withheld until submission.")
                    feedback=(last.get("feedback") or current.get("feedback") or {})
                    if feedback.get("available"):
                        if feedback.get("correctness")=="correct": st.success(f"Correct ({feedback.get('score')}). "+str(feedback.get("explanation") or ""))
                        elif feedback.get("correctness")=="partial": st.info(f"Partially correct ({feedback.get('score')}). "+str(feedback.get("explanation") or ""))
                        else: st.error(f"Incorrect ({feedback.get('score')}). "+str(feedback.get("explanation") or ""))
                        if feedback.get("missing_key_points"): st.caption("Missing: "+"; ".join(feedback["missing_key_points"][:3]))
                        if (feedback.get("teaching") or {}).get("message"): st.caption(str(feedback["teaching"]["message"]))
                    st.caption(f"Grading status: {current.get('grading_status')} · flagged: {'yes' if current.get('flagged') else 'no'}")
                if st.button("Submit assessment",key="assess_submit"):
                    mf.complete_assessment(aid); st.rerun()
            else:
                result=(mf.get_assessment_result(aid).get("result") or {})
                score=result.get("score") or {}
                s1,s2,s3,s4=st.columns(4)
                s1.metric("Raw score",f"{score.get('raw_score')}/{score.get('max_score')}")
                s2.metric("Percentage",("—" if score.get("percentage") is None else f"{score['percentage']:.1f}%"))
                s3.metric("Passed",("—" if score.get("passed") is None else ("yes" if score["passed"] else "no")))
                s4.metric("Pending grading",score.get("pending_items",0))
                if score.get("pending_items"):
                    st.warning("Some answers are saved but not graded yet — retry grading to score them without duplicating attempts.")
                    if st.button("Retry pending grading",key="assess_retry_done"): mf.retry_pending_grading(aid); st.rerun()
                st.caption(result.get("note") or "")
                if result.get("domains"): st.dataframe([{"Domain":k,"Answered":v["answered"],"%":v["percentage"],"Correct":v["correct"],"Partial":v["partial"],"Incorrect":v["incorrect"]} for k,v in result["domains"].items()],width="stretch",hide_index=True)
                if result.get("weakest_domains"): st.markdown("**Weakest areas:** "+", ".join(result["weakest_domains"]))
                if result.get("strongest_domains"): st.markdown("**Strongest areas:** "+", ".join(result["strongest_domains"]))
                conf=result.get("confidence") or {}
                st.caption(f"Confidence: mean {conf.get('mean')} · correct {conf.get('correct_mean')} · incorrect {conf.get('incorrect_mean')} · mismatches {conf.get('mismatch_count')}")
                if result.get("flagged_items"): st.caption("Flagged for review: "+", ".join(str(f["question_order"]) for f in result["flagged_items"]))
                remediation=mf.get_assessment_remediation(aid)
                if remediation.get("weak_topics"): st.markdown("**Remediation — weak topics:** "+", ".join(remediation["weak_topics"]))
                if remediation.get("prerequisite_gaps"): st.caption("Prerequisite gaps: "+"; ".join(f"{g['prerequisite']} → {g['topic']}" for g in remediation["prerequisite_gaps"]))
                if (remediation.get("tutor_remediation") or {}).get("topic"): st.caption(f"Suggested tutor session (not started automatically): {remediation['tutor_remediation']['topic']} · mode {remediation['tutor_remediation']['mode']}")
                review=mf.review_assessment(aid)
                with st.expander(f"Question review ({len(review.get('questions') or [])} questions)"):
                    for q in review.get("questions") or []:
                        st.markdown(f"**Q{q['question_order']}** ({q['item_type']}): {q['stem']}")
                        st.caption(f"Your answer: {q.get('learner_answer')} · score {q.get('score')} · {'flagged' if q.get('flagged') else 'not flagged'}")
                        st.caption(f"Correct: {q.get('correct_answer') or ', '.join(q.get('correct_choices') or [])} — {q.get('explanation')}")
                        for ref in (q.get("evidence_refs") or [])[:2]:
                            st.caption(f"Evidence: {ref.get('locator') or ref.get('evidence_id')} (evidence_id {ref.get('evidence_id')})")
            if st.button("Start a different assessment",key="assess_clear"):
                st.session_state.pop("assess_id",None); st.rerun()
    except Exception as e: show_error(e)
with tabs[6]:
    question=st.text_area("Ask your indexed evidence library",max_chars=1500)
    if st.button("Ask") and question.strip():
        with st.spinner("Finding source passages..."):
            try: st.session_state["answer"]=mf.ask(question.strip())
            except Exception as e: show_error(e)
    if st.session_state.get("answer"): st.markdown(st.session_state["answer"])
with tabs[7]:
    st.caption("Canonical curriculum: Semester → Subject → Week → Seminar → Topic → Subtopic → Learning Objective. Paste a weekly or seminar syllabus; topics become traceable study targets.")
    c1,c2,c3=st.columns(3)
    syn_subject=c1.text_input("Subject (optional)",key="curr_subject",placeholder="e.g. Endocrinology")
    syn_semester=c2.text_input("Semester (optional)",key="curr_semester",placeholder="e.g. Semester 3")
    syn_week=c3.number_input("Attach loose seminars to week (0 = ignore)",0,52,0,key="curr_week")
    syllabus=st.text_area("Paste weekly syllabus",height=220,key="curr_syllabus",placeholder="Week 1: Hypothalamus & Pituitary\nSeminar: Pituitary hormones\n- Anterior pituitary hormones\n  - GH and IGF-1 axis\nLO: Explain the GH axis with feedback control")
    if st.button("Import syllabus",type="primary") and syllabus.strip():
        try:
            res=mf.import_syllabus(
                syllabus,
                subject_title=syn_subject.strip() or None,
                semester_title=syn_semester.strip() or None,
                week_number=int(syn_week) or None,
            )
            if res["created_total"]==0:
                st.info("Nothing new — every parsed node already exists (idempotent import).")
            else:
                st.success("Imported: "+", ".join(f"{v} {k}(s)" for k,v in sorted(res["created"].items())))
        except Exception as e: show_error(e)
    try:
        prog=mf.curriculum_progress()
    except Exception as e:
        prog=None; show_error(e)
    if prog:
        s=prog["summary"]
        p1,p2,p3,p4,p5=st.columns(5)
        p1.metric("Subjects",s["subjects"])
        p2.metric("Weeks",s["weeks"])
        p3.metric("Topics",s["topics"])
        p4.metric("Studied",s["topics_studied"])
        p5.metric("Mastered (≥70)",s["topics_mastered"])
        if s["studied_unmapped"]:
            st.warning("Studied topics not yet mapped to the curriculum: "+", ".join(s["studied_unmapped"]))
        if s["topics"]==0:
            st.info("Import a syllabus above to populate your curriculum.")
        if prog["subjects"]:
            with st.expander("Curriculum tree & progress"):
                st.json(mf.curriculum_tree())
        with st.expander("Topic traceability lookup"):
            look=st.text_input("Studied topic",key="curr_lookup",placeholder="e.g. pituitary-gland or Anterior pituitary hormones")
            if st.button("Show curriculum position") and look.strip():
                res=mf.topic_path(look.strip())
                if res is None: st.warning("Not mapped yet — add it to a syllabus, then re-run lookup.")
                else: st.success(res["position"])
        with st.expander("Prerequisites"):
            pa,pb,pc=st.columns([2,2,1])
            pre_topic=pa.text_input("Topic",key="prereq_topic")
            pre_req=pb.text_input("Requires",key="prereq_req")
            pre_type=pc.selectbox("Type",list(mf.PREREQUISITE_TYPES),0,key="prereq_type")
            if st.button("Add prerequisite") and pre_topic.strip() and pre_req.strip():
                try:
                    mf.add_prerequisite(pre_topic.strip(),pre_req.strip(),pre_type)
                    st.success(f"Recorded: {pre_req} → {pre_topic} ({pre_type})")
                except Exception as e: show_error(e)
with tabs[8]:
    st.caption("Registered textbooks with stable edition identity and page/section provenance. Re-importing the same file is idempotent; a changed edition becomes a new version.")
    try:
        books=mf.list_textbooks()
    except Exception as e:
        books=[]; show_error(e)
    editions=[(f"{d['title']} · {e.get('edition_label') or 'edition'} · {e['id']} · {e['ingest_status']}",e["id"]) for d in books for e in d["editions"]]
    with st.expander(f"Registered textbooks ({len(editions)} edition(s))"):
        for label,_eid in editions: st.markdown("- "+label)
        if not editions: st.caption("None yet — add a textbook PDF below.")
    t1,t2,t3=st.columns([3,2,2])
    tb_path=t1.text_input("Textbook PDF path",key="tb_path",placeholder="/Users/…/textbook.pdf")
    tb_title=t2.text_input("Title (optional)",key="tb_title")
    tb_edition=t3.text_input("Edition (optional)",key="tb_edition")
    if st.button("Register & ingest",type="primary") and tb_path.strip():
        with st.spinner("Extracting page-wise structure..."):
            try:
                res=mf.ingest_textbook(tb_path.strip(),embed_text=not mf.OFFLINE,title=tb_title.strip() or None,edition=tb_edition.strip() or None)
                st.success(f"{'Re-used existing' if res.get('skipped') else 'Ingested'} {res['title']}: {res['pages_extracted']}/{res['pages']} pages, {res['chapters']} chapters, {res['chunks_total']} chunks, status {res['ingest_status']}"+(f" · OCR pending for {res['pages_no_text']} page(s)" if res['pages_no_text'] else ""))
            except Exception as e: show_error(e)
    if editions:
        pick=st.selectbox("Inspect edition",[l for l,_ in editions],key="tb_pick")
        eid=dict(editions)[pick]
        try:
            info=mf.book_structure(eid)
            e=info["edition"]; d=info["document"]
            c1,c2,c3,c4=st.columns(4)
            c1.metric("Pages",e["page_count"]); c2.metric("Extracted",e["extracted_pages"])
            c3.metric("No text (OCR pending)",e["skipped_pages"]); c4.metric("Chunks",e["chunk_count"])
            st.caption(f"{d['title']} · {d.get('authors') or 'authors n/a'} · {d.get('publisher') or 'publisher n/a'} · {d.get('publication_year') or 'year n/a'} · ISBN {d.get('isbn') or 'n/a'} · hash {e['content_hash'][:12]}… · {e['ingest_status']}")
            with st.expander("Chapters & sections"): st.json(info["nodes"])
            with st.expander(f"Pages (first {len(info['pages'])} of {info['pages_total']})"): st.dataframe(info["pages"],width="stretch",hide_index=True)
        except Exception as e: show_error(e)
        with st.expander("Link curriculum topic → textbook evidence"):
            l1,l2,l3=st.columns([2,2,1])
            link_topic=l1.text_input("Curriculum topic",key="tb_link_topic")
            link_node=l2.text_input("Chapter/section id (optional)",key="tb_link_node")
            link_type=l3.selectbox("Link type",list(mf.CURRICULUM_TEXT_LINK_TYPES),0,key="tb_link_type")
            if st.button("Create link") and link_topic.strip():
                try:
                    res=mf.link_curriculum_text(link_topic.strip(),eid,node_id=link_node.strip() or None,link_type=link_type)
                    st.success(("Linked" if res["created"] else "Link already existed")+f": {res['curriculum_node']['title']}")
                except Exception as e: show_error(e)
        with st.expander("Discover textbook evidence for a topic"):
            ev_topic=st.text_input("Topic or curriculum node id",key="tb_ev_topic")
            if st.button("Find evidence") and ev_topic.strip():
                try:
                    ev=mf.textbook_evidence_for_topic(ev_topic.strip())
                    if ev["count"]==0: st.info("No linked textbook evidence yet for this topic.")
                    else:
                        st.success(f"{ev['count']} linked source(s) for '{ev['curriculum_node']['title']}'")
                        for link in ev["links"]:
                            st.markdown(f"**{link.get('textbook_node_title') or link['document_title']}** · {link['link_type']} · p. {link.get('start_page') or link.get('page_start') or '?'}–{link.get('end_page') or link.get('page_end') or '?'}")
                            for pv in link["previews"]: st.caption(pv["locator"]+" — "+pv["text"][:180])
                except Exception as e: show_error(e)
with tabs[9]:
    st.caption("Claim → verification status → evidence → source provenance. Excerpts stay in the local database (private, not redistributed).")
    try:
        snap=mf.evidence_snapshot()
        a,b,c=st.columns(3)
        a.metric("Claims",snap["counts"]["claims"])
        b.metric("Evidence records",snap["counts"]["evidence"])
        c.metric("Needs review",snap["needs_review"])
        if snap["by_status"]:
            st.caption("Statuses: "+" · ".join(f"{k}: {v}" for k,v in sorted(snap["by_status"].items())))
        statuses=["ALL"]+list(mf.CLAIM_VERIFICATION_STATUS)
        chosen=st.selectbox("Filter by verification status",statuses)
        listing=mf.claims_list(status=None if chosen=="ALL" else chosen,limit=50)
        st.caption(f"{listing['total']} claim(s)" + ("" if chosen=="ALL" else f" with status {chosen}")+" · newest first, up to 50 shown")
        for claim in listing["claims"]:
            head=claim["claim_text"][:90]+("…" if len(claim["claim_text"])>90 else "")
            with st.expander(f"{head} · {claim['verification_status']}"):
                st.write(claim["claim_text"])
                conf=f" · confidence {claim['verification_confidence']:.2f}" if claim["verification_confidence"] is not None else ""
                st.caption(f"Type: {claim['claim_type']} · Review: {claim['review_status']} · Topic: {claim['topic'] or '—'} · Labels: {claim['source_labels'] or '—'}{conf}")
                info=mf.claim_info(claim["claim_id"])
                for trace in (info or {}).get("provenance",[]):
                    st.divider()
                    st.markdown(f"**{trace['relationship']}** · evidence `{trace['evidence_id']}`")
                    src=" · ".join(x for x in [trace["document"],trace["edition"],trace["chapter"],trace["section"],(f"p. {trace['page']}" if trace["page"] else trace["locator"])] if x)
                    st.caption("Source: "+(src or trace["url"] or "no structured locator"))
                    st.caption("Excerpt (private): "+trace["excerpt"][:400])
                runs=(info or {}).get("runs",[])
                if runs:
                    with st.expander("Verification history"):
                        for run in runs[:15]:
                            st.caption(f"{run['created_at']} · {run['result']} · {run['method']} · {run['notes'][:140]}")
    except Exception as e: show_error(e)
with tabs[10]:
    st.caption("Add medical PDFs you are entitled to use. Changed files are indexed on the next product run.")
    uploads=st.file_uploader("Add PDFs",type=["pdf"],accept_multiple_files=True)
    if st.button("Save selected PDFs"):
        for upload in uploads or []:
            name=Path(upload.name.replace("\\", "/")).name
            blob=upload.getvalue()
            if not name.lower().endswith(".pdf") or not blob.startswith(b"%PDF-"):
                st.warning("Skipped a file that was not a PDF."); continue
            target=mf.DOCS/name
            if target.exists() and target.read_bytes()!=blob:
                target=target.with_name(target.stem+"-"+hashlib.sha256(blob).hexdigest()[:8]+".pdf")
            target.write_bytes(blob)
            st.success("Saved "+target.name)
    st.code(str(mf.DOCS),language=None)
    st.caption("Scanned PDFs with no readable text are reported. OCR is not yet included.")
with tabs[11]:
    states=sorted(mf.PRODUCTS.glob("*/v*/state.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    if not states: st.info("Your generated packs will appear here.")
    for path in states[:30]:
        try: state=json.loads(path.read_text())
        except (OSError,ValueError): continue
        with st.expander(f"{state.get('topic','Topic')} · {path.parent.name} · {'Draft complete' if state.get('complete') else 'In progress'}"):
            if state.get("complete"): show_pack(path.parent,"history-"+str(path))
            else: st.caption("Enter this topic in PRODUCT to resume compatible saved work.")
with tabs[12]:
    try:
        s=mf.status()
        a,b,c=st.columns(3)
        a.metric("Evidence passages",s["chunks"]); b.metric("PDFs",s["pdfs"]); c.metric("Pack versions",s["products"])
        st.write("Ollama: "+("running" if s["ollama"] else "will start when needed"))
        st.write("Installed models: "+(", ".join(s["models"]) or "downloaded automatically when needed"))
    except Exception as e: show_error(e)
st.divider()
st.caption("Educational drafts. Citation labels are traceability aids; review source passages before sharing medical claims.")
