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

tabs=st.tabs(["PRODUCT","STUDY","REVIEW","ASK","LIBRARY","HISTORY","STATUS"])
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
    question=st.text_area("Ask your indexed evidence library",max_chars=1500)
    if st.button("Ask") and question.strip():
        with st.spinner("Finding source passages..."):
            try: st.session_state["answer"]=mf.ask(question.strip())
            except Exception as e: show_error(e)
    if st.session_state.get("answer"): st.markdown(st.session_state["answer"])
with tabs[4]:
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
with tabs[5]:
    states=sorted(mf.PRODUCTS.glob("*/v*/state.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    if not states: st.info("Your generated packs will appear here.")
    for path in states[:30]:
        try: state=json.loads(path.read_text())
        except (OSError,ValueError): continue
        with st.expander(f"{state.get('topic','Topic')} · {path.parent.name} · {'Draft complete' if state.get('complete') else 'In progress'}"):
            if state.get("complete"): show_pack(path.parent,"history-"+str(path))
            else: st.caption("Enter this topic in PRODUCT to resume compatible saved work.")
with tabs[6]:
    try:
        s=mf.status()
        a,b,c=st.columns(3)
        a.metric("Evidence passages",s["chunks"]); b.metric("PDFs",s["pdfs"]); c.metric("Pack versions",s["products"])
        st.write("Ollama: "+("running" if s["ollama"] else "will start when needed"))
        st.write("Installed models: "+(", ".join(s["models"]) or "downloaded automatically when needed"))
    except Exception as e: show_error(e)
st.divider()
st.caption("Educational drafts. Citation labels are traceability aids; review source passages before sharing medical claims.")
