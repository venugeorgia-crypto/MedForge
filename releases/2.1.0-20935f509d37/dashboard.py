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

tabs=st.tabs(["PRODUCT","STUDY","REVIEW","ASK","CURRICULUM","TEXTBOOKS","EVIDENCE","LIBRARY","HISTORY","STATUS"])
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
with tabs[5]:
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
with tabs[6]:
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
with tabs[7]:
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
with tabs[8]:
    states=sorted(mf.PRODUCTS.glob("*/v*/state.json"),key=lambda p:p.stat().st_mtime,reverse=True)
    if not states: st.info("Your generated packs will appear here.")
    for path in states[:30]:
        try: state=json.loads(path.read_text())
        except (OSError,ValueError): continue
        with st.expander(f"{state.get('topic','Topic')} · {path.parent.name} · {'Draft complete' if state.get('complete') else 'In progress'}"):
            if state.get("complete"): show_pack(path.parent,"history-"+str(path))
            else: st.caption("Enter this topic in PRODUCT to resume compatible saved work.")
with tabs[9]:
    try:
        s=mf.status()
        a,b,c=st.columns(3)
        a.metric("Evidence passages",s["chunks"]); b.metric("PDFs",s["pdfs"]); c.metric("Pack versions",s["products"])
        st.write("Ollama: "+("running" if s["ollama"] else "will start when needed"))
        st.write("Installed models: "+(", ".join(s["models"]) or "downloaded automatically when needed"))
    except Exception as e: show_error(e)
st.divider()
st.caption("Educational drafts. Citation labels are traceability aids; review source passages before sharing medical claims.")
