#!/usr/bin/env python3
"""MedForge Core — CLI entry point and compatibility shim over the medforge package.

The heavy lifting lives in current/medforge/ modules; this file keeps the
`medforge_core.py` module name working for medforge_launch.py and dashboard.py.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# Ensure both the package dir and the workspace root are importable
_CURRENT = Path(__file__).resolve().parent
_BASE = _CURRENT.parent
for _p in (str(_CURRENT), str(_BASE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from medforge import (  # noqa: E402
    VERSION, BASE, APP, DOCS, PRODUCTS, DBDIR, CHROMA_DIR, META_DB, TMP, LOGS,
    OLLAMA_URL, EMBED_MODEL, PREFERRED_CHAT, FALLBACK_MODELS, NCBI_EMAIL, OFFLINE,
    TRUSTED_DOMAINS, NODE_TYPES, PREREQUISITE_TYPES, WEAKNESS_SEVERITY, SESSION_TYPES,
    SPACED_REPETITION_STATES, SPACED_REPETITION_ITEM_TYPES, MEDICAL_PUBLICATION_TYPES,
    SYSTEM_EVIDENCE,
    TUTOR_SESSION_MODES, TUTOR_SESSION_GOALS, TUTOR_DEFAULT_GOAL,
    TUTOR_MAX_INTERACTIONS, TUTOR_VERSION,
    init_db, get_collection, upsert_records, migrate_database,
    ensure_v3_tables, start_session, record_session, update_mastery,
    record_weakness, resolve_weakness, log_study_result, learner_snapshot,
    card_item_id, import_flashcards, due_items, review_card, review_analytics,
    spaced_repetition_snapshot, SCHEDULERS, DEFAULT_SCHEDULER,
    complete_session, ensure_learner_model_tables, record_learning_event,
    recalculate_mastery, recalculate_all, get_mastery, get_confidence,
    get_recent_performance,
    get_weaknesses, get_prerequisite_risks, study_priority, detect_weaknesses,
    learner_history, learner_summary,
    ensure_tutor_tables, start_tutor_session, get_tutor_state, resume_tutor_session,
    select_tutor_target, next_tutor_step, submit_answer, complete_tutor_session,
    get_tutor_summary, list_tutor_sessions, public_view,
    hybrid_retrieve, source_pack, keyword_results, vector_results,
    generate_text, citation_audit, parse_tsv_cards,
    write_pdf, make_anki,
    ingest_pdfs, pubmed_import, web_research, fetch_web_text, domain_quality,
    ensure_models, ensure_ollama_running, model_names, model_exists,
    test_chat_model, stop_model, embed, chat, ollama_alive, pull_model, http_json,
    build_product, product_dir, save_state, ask, study, status,
    HIERARCHY, normalize_title, title_key, ensure_curriculum_tables,
    find_node, ensure_node, get_children, import_syllabus, parse_syllabus,
    add_prerequisite, remove_prerequisite, curriculum_tree, topic_path,
    curriculum_progress, detect_duplicates,
    ensure_textbook_tables, resolve_metadata, register_textbook,
    ingest_textbook, list_textbooks, book_structure, link_curriculum_text,
    unlink_curriculum_text, textbook_evidence_for_topic,
    suggest_curriculum_links, registered_source_paths,
    SOURCE_PRIORITY, CURRICULUM_TEXT_LINK_TYPES,
    ensure_evidence_tables, extract_claims, store_claims, verify_claim,
    verify_product_claims, claims_list, claim_info, evidence_snapshot,
    CLAIM_VERIFICATION_STATUS,
    ensure_assessment_tables, create_item, get_item, list_items, validate_item,
    approve_item, retire_item, create_blueprint, list_blueprints, select_items,
    create_assessment, start_assessment, get_assessment_state, resume_assessment,
    submit_assessment_answer, flag_item, next_item, previous_item,
    complete_assessment, get_assessment_result, get_item_statistics,
    get_assessment_remediation, review_assessment, list_assessments,
    ASSESSMENT_ITEM_TYPES, ASSESSMENT_MODES,
    ensure_study_tables, get_study_status, recommend_next_action,
    get_knowledge_gaps, get_readiness, build_study_plan, get_plan, list_plans,
    get_today_plan, start_study_mission, get_mission_state, list_missions,
    complete_mission_action, complete_mission, harvest_mission_results,
    launch_mission_engines, get_study_history, adaptation_profile,
    STUDY_VERSION, PLANNER_VERSION,
    generate_canonical_content, get_content, list_content,
    render_study_products, regenerate_product, get_product_status, list_artifacts,
    artifact_provenance, content_consistency_report,
    CONTENT_VERSION, PROMPT_VERSION,
    mkdirs, sh, slugify, utcnow, atomic_text, job_lock, serialized, chunks, batch,
)

import medforge.types as _types  # noqa: E402

__version__ = VERSION


def set_offline(value: bool) -> None:
    """Toggle offline mode for the whole engine (used by the dashboard toggle).

    Keeps both this module's OFFLINE attribute and the engine-wide flag in sync.
    """
    global OFFLINE
    OFFLINE = bool(value)
    _types.OFFLINE = bool(value)


def _resolve_scope_node(target: str):
    """Resolve a curriculum node by id or title (CLI assessment scopes)."""
    import sqlite3

    con = sqlite3.connect(META_DB)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id, node_type, title FROM curriculum_nodes WHERE id=?", (target,)
        ).fetchone()
        if row is None:
            row = con.execute(
                "SELECT id, node_type, title FROM curriculum_nodes"
                " WHERE lower(title)=lower(?)"
                " ORDER BY CASE node_type WHEN 'Topic' THEN 0 ELSE 1 END, order_index LIMIT 1",
                (target,),
            ).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def main() -> None:
    mkdirs()
    init_db()
    if len(sys.argv) < 2:
        print("Commands: product <topic> | ask <question> | study <topic> | "
              "study-log <topic> <score> | mastery | due | "
              "review <item_id> <grade 0-5> [sm2|fsrs] | import-cards [topic] | "
              "syllabus [file] | curriculum | path <topic> | "
              "prereq <topic>|<prerequisite> [|strict|recommended|co-requisite] | "
              "textbooks | textbook-add <pdf>[|title|edition|authors|publisher|year|isbn|subject][|embed=0] | "
              "textbook-info <edition_id> | "
              "textbook-link <topic>|<edition_id>[|<node_id>][|primary|supporting|supplementary] | "
              "textbook-evidence <topic> | "
              "claims [STATUS] | claim-info <claim_id> | verify-pack [pack_dir] | "
              "evidence-status | "
              "learner | mastery <topic> | weaknesses | history <topic> | "
              "recalculate <topic|all> | study-priority | "
              "tutor start <topic|--recommended>[|mode|goal] (also tutor \"start|<topic>|<mode>|<goal>\") | "
              "tutor status [id] | tutor next <id> | tutor answer <id>|<answer>|<confidence 0-1> | "
              "tutor end <id> | tutor summary <id> | "
              "assessment list | assessment blueprints | "
              "assessment create <blueprint_id|scope_node>|<MODE>[|<items>[|<minutes>]] | "
              "assessment start <id> | assessment status [id] | assessment next <id> | "
              "assessment answer <id>|<answer>[|<confidence 0-1>] | "
              "assessment flag <id>[|<order>][|<reason>] | assessment submit <id> | "
              "assessment result <id> | assessment review <id> | "
              "assessment remediate <id> | assessment items [STATUS] | "
              "assessment item-info <item_id>[|<version>] | "
              "assessment stats <item_id>[|<version>] | "
              "assessment blueprint-new <title>|<scope_node>|<scope_type>[|<items>] | "
              "migrate | doctor")
        return
    cmd = sys.argv[1].lower()
    arg = " ".join(sys.argv[2:]).strip()

    if cmd == "product":
        if not arg:
            arg = input("Topic: ").strip()
        if not arg:
            raise SystemExit("No topic entered.")
        build_product(arg, include_web=not _types.OFFLINE)
    elif cmd == "ask":
        if not arg:
            arg = input("Question: ").strip()
        print(ask(arg))
    elif cmd == "study":
        if not arg:
            arg = input("Topic: ").strip()
        print(study(arg))
    elif cmd == "study-log":
        # study-log <topic ...> <score 0-10 or 0-100>
        try:
            topic, score = arg.rsplit(maxsplit=1)
        except ValueError:
            raise SystemExit("Usage: medforge_core study-log <topic> <score 0-10>") from None
        result = log_study_result(topic, float(score),
                                  notes="Logged from the command line")
        print(json.dumps(result, indent=2))
    elif cmd == "due":
        print(json.dumps(spaced_repetition_snapshot(), indent=2))
    elif cmd == "review":
        # review <item_id> <grade 0-5> [sm2|fsrs]
        parts = arg.split()
        if len(parts) == 2:
            parts.append(None)
        if len(parts) != 3:
            raise SystemExit("Usage: medforge_core review <item_id> <grade 0-5> [sm2|fsrs]")
        item_id, grade, scheduler = parts
        try:
            grade = int(grade)
        except ValueError:
            raise SystemExit("Usage: medforge_core review <item_id> <grade 0-5> [sm2|fsrs]") from None
        print(json.dumps(review_card(item_id, grade, scheduler=scheduler), indent=2))
    elif cmd == "import-cards":
        # import-cards [topic] — with no topic, schedule every saved pack.
        targets = []
        if arg:
            targets = [(arg, None)]
        else:
            targets = [
                (p.parent.parent.parent.name, p)
                for p in sorted(PRODUCTS.glob("*/v*/flashcards.csv"))
            ]
        if not targets:
            raise SystemExit("No flashcards.csv found under products/; run product <topic> first.")
        results = []
        for topic, path in targets:
            try:
                results.append(import_flashcards(topic, path))
            except Exception as e:
                results.append({"topic_id": topic, "error": str(e)})
        print(json.dumps(results, indent=2))
    elif cmd == "syllabus":
        # syllabus [file] — weekly or seminar-level pasted syllabus → curriculum.
        # A file path imports its text; with no argument, read the piped stdin.
        if arg:
            text = Path(arg).read_text(encoding="utf-8")
        elif not sys.stdin.isatty():
            text = sys.stdin.read()
        else:
            raise SystemExit("Usage: medforge_core syllabus <file> (or pipe text in)")
        print(json.dumps(import_syllabus(text), indent=2))
    elif cmd == "curriculum":
        print(json.dumps({
            "progress": curriculum_progress(),
            "duplicates": detect_duplicates(),
        }, indent=2))
    elif cmd == "path":
        if not arg:
            raise SystemExit("Usage: medforge_core path <topic>")
        result = topic_path(arg)
        if result is None:
            print(json.dumps({"topic": arg, "mapped": False,
                              "hint": "Import a syllabus containing this topic."}, indent=2))
        else:
            print(json.dumps({"topic": arg, "mapped": True,
                              "position": result["position"],
                              "chain": result["chain"]}, indent=2))
    elif cmd == "prereq":
        # prereq <topic>|<prerequisite>[|strict|recommended|co-requisite]
        # Pipe-delimited because topic titles contain spaces.
        parts = [p.strip() for p in arg.split("|")]
        if len(parts) == 2:
            parts.append("strict")
        if len(parts) != 3 or not all(parts[:2]):
            raise SystemExit(
                'Usage: medforge_core prereq "<topic>|<prerequisite>[|type]"'
            )
        print(json.dumps(add_prerequisite(parts[0], parts[1], parts[2]), indent=2))
    elif cmd == "textbooks":
        print(json.dumps(list_textbooks(), indent=2))
    elif cmd == "textbook-add":
        # textbook-add <pdf>[|title|edition|authors|publisher|year|isbn|subject][|embed=0]
        parts = [p.strip() for p in arg.split("|")] if arg else []
        if not parts or not parts[0]:
            raise SystemExit(
                "Usage: medforge_core textbook-add <pdf>"
                "[|title|edition|authors|publisher|year|isbn|subject][|embed=0]"
            )
        embed = not any(p.lower() == "embed=0" for p in parts)
        fields = [p for p in parts[1:] if p.lower() != "embed=0"]
        keys = ["title", "edition", "authors", "publisher",
                "publication_year", "isbn", "subject"]
        meta: Dict[str, Any] = {}
        for key, value in zip(keys, fields):
            if not value:
                continue
            meta[key] = int(value) if key == "publication_year" else value
        print(json.dumps(ingest_textbook(parts[0], embed_text=embed, **meta), indent=2))
    elif cmd == "textbook-info":
        if not arg:
            raise SystemExit("Usage: medforge_core textbook-info <edition_id>")
        print(json.dumps(book_structure(arg), indent=2))
    elif cmd == "textbook-link":
        # textbook-link <topic>|<edition_id>[|<node_id>][|link_type]
        parts = [p.strip() for p in arg.split("|")] if arg else []
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise SystemExit(
                "Usage: medforge_core textbook-link <topic>|<edition_id>"
                "[|<textbook_node_id>][|primary|supporting|supplementary]"
            )
        node_id = parts[2] if len(parts) > 2 and parts[2] else None
        link_type = parts[3] if len(parts) > 3 and parts[3] else "primary"
        print(json.dumps(
            link_curriculum_text(parts[0], parts[1], node_id=node_id,
                                 link_type=link_type), indent=2,
        ))
    elif cmd == "textbook-evidence":
        if not arg:
            raise SystemExit("Usage: medforge_core textbook-evidence <topic>")
        print(json.dumps(textbook_evidence_for_topic(arg.strip()), indent=2))
    elif cmd == "claims":
        # claims [STATUS] — bounded listing of extracted claims.
        # Named `claim_status` so it does not shadow the legacy `status()`
        # command (every `main()` local is function-scoped in Python).
        claim_status = arg.strip().upper() or None
        if claim_status and claim_status not in CLAIM_VERIFICATION_STATUS:
            raise SystemExit(
                "STATUS must be one of: " + ", ".join(CLAIM_VERIFICATION_STATUS)
            )
        print(json.dumps(claims_list(status=claim_status, limit=50), indent=2))
    elif cmd == "claim-info":
        if not arg:
            raise SystemExit("Usage: medforge_core claim-info <claim_id>")
        info = claim_info(arg.strip())
        if info is None:
            raise SystemExit(f"Unknown claim_id: {arg.strip()}")
        print(json.dumps(info, indent=2))
    elif cmd == "verify-pack":
        # verify-pack [pack_dir] — extract & verify claims from an existing pack.
        # Reuses the pack's saved sources; bounded (≤12 claims, ≤2 candidates)
        # so an 8 GB M1 never loads a whole textbook for verification.
        pack = Path(arg).expanduser().resolve() if arg else None
        if pack is None:
            candidates = sorted(
                PRODUCTS.glob("*/v*/"), key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            pack = next(
                (p for p in candidates if (p / "source-map.json").is_file()), None
            )
        if pack is None or not (pack / "source-map.json").is_file():
            raise SystemExit("No product pack with source-map.json found under products/.")
        sources = json.loads((pack / "source-map.json").read_text(encoding="utf-8"))
        texts = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(pack.glob("*.md"))
        }
        if not texts:
            raise SystemExit(f"No generated markdown found in {pack}.")
        model = ensure_models()
        result = verify_product_claims(
            texts, sources, topic=pack.parent.name,
            generation_run="/".join(pack.parts[-2:]), model=model,
            max_claims=12, max_candidates=2, outdir=pack,
        )
        print(json.dumps({k: v for k, v in result.items()
                          if k not in ("claims", "label_map")}, indent=2))
        for claim in result["claims"][:10]:
            print(json.dumps(claim, indent=2))
    elif cmd == "evidence-status":
        print(json.dumps(evidence_snapshot(), indent=2))
    elif cmd == "learner":
        detect_weaknesses()
        print(json.dumps(learner_summary(), indent=2))
    elif cmd == "mastery":
        # mastery [topic] — no arg: legacy snapshot; with a topic: P6 state.
        if not arg:
            print(json.dumps(learner_snapshot(), indent=2))
            return
        info = get_mastery(arg.strip())
        if info is None:
            raise SystemExit(
                f"No learner-model state for {arg.strip()!r}; record a session first."
            )
        info["confidence"] = get_confidence(arg.strip())
        info["recent"] = get_recent_performance(arg.strip())
        info["prerequisite_risks"] = get_prerequisite_risks(arg.strip())
        print(json.dumps(info, indent=2))
    elif cmd == "weaknesses":
        detect_weaknesses()
        print(json.dumps(get_weaknesses(), indent=2))
    elif cmd == "history":
        if not arg:
            raise SystemExit("Usage: medforge_core history <topic>")
        print(json.dumps(learner_history(arg.strip(), limit=50), indent=2))
    elif cmd == "recalculate":
        if not arg:
            raise SystemExit("Usage: medforge_core recalculate <topic|all>")
        if arg.strip().lower() == "all":
            print(json.dumps(recalculate_all(), indent=2))
        else:
            print(json.dumps(recalculate_mastery(arg.strip()), indent=2))
    elif cmd == "study-priority":
        print(json.dumps(study_priority(), indent=2))
    elif cmd == "tutor":
        # tutor start <topic|--recommended>[|mode|goal] | tutor status [id] |
        # tutor next <id> | tutor answer <id>|<answer>[|<confidence 0-1>] |
        # tutor end <id> | tutor summary <id> | tutor sessions | tutor targets
        # Accept both `tutor "start|<topic>|<mode>|<goal>"` and `tutor start <topic>`:
        # the first token is always the action, the rest is pipe-delimited payload.
        parts = [p.strip() for p in arg.split("|")] if arg else []
        if parts:
            head = parts[0].split(None, 1)
            parts[0] = head[0].lower()
            if len(head) > 1:
                parts.insert(1, head[1])
        action = parts[0] if parts else ""
        if action == "start":
            target = parts[1] if len(parts) > 1 else ""
            mode = parts[2] if len(parts) > 2 and parts[2] else None
            goal = parts[3] if len(parts) > 3 and parts[3] else None
            if not target:
                raise SystemExit(
                    "Usage: medforge_core tutor start <topic>|--recommended[|mode|goal]"
                )
            topic = None if target.lower() in ("--recommended", "recommended") else target
            print(json.dumps(start_tutor_session(topic, mode=mode, goal=goal), indent=2))
        elif action == "status":
            state = get_tutor_state(int(parts[1])) if len(parts) > 1 else resume_tutor_session()
            print(json.dumps(state, indent=2))
        elif action == "targets":
            print(json.dumps(select_tutor_target(), indent=2))
        elif action == "sessions":
            print(json.dumps(list_tutor_sessions(), indent=2))
        elif action == "next":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core tutor next <session_id>")
            print(json.dumps(next_tutor_step(int(parts[1])), indent=2))
        elif action == "answer":
            if len(parts) < 3:
                raise SystemExit(
                    "Usage: medforge_core tutor answer <session_id>|<answer>[|<confidence 0-1>]"
                )
            confidence = float(parts[3]) if len(parts) > 3 and parts[3] else None
            print(json.dumps(submit_answer(int(parts[1]), parts[2], confidence=confidence), indent=2))
        elif action == "end":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core tutor end <session_id>")
            print(json.dumps(complete_tutor_session(
                int(parts[1]), reason="learner ended the session"), indent=2))
        elif action == "summary":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core tutor summary <session_id>")
            print(json.dumps(get_tutor_summary(int(parts[1])), indent=2))
        else:
            raise SystemExit(
                "Usage: medforge_core tutor start|status|next|answer|end|summary|sessions|targets"
            )
    elif cmd == "assessment":
        # assessment list | assessment blueprints | assessment create <target>|<MODE> |
        # assessment start <id> | assessment status [id] | assessment next <id> |
        # assessment answer <id>|<answer>[|<confidence>] | assessment flag <id>|<order>|<reason> |
        # assessment submit <id> | assessment result <id> | assessment review <id> |
        # assessment remediate <id> | assessment items [STATUS] |
        # assessment item-info <item_id>[|<version>] | assessment stats <item_id>[|<version>] |
        # Accepts both `assessment create bp-1|EXAM` and `assessment list`.
        parts = [p.strip() for p in arg.split("|")] if arg else []
        if parts:
            head = parts[0].split(None, 1)
            parts[0] = head[0].lower()
            if len(head) > 1:
                parts.insert(1, head[1])
        action = parts[0] if parts else ""
        if action in ("", "list"):
            print(json.dumps(list_assessments(), indent=2))
        elif action == "blueprints":
            print(json.dumps(list_blueprints(), indent=2))
        elif action == "create":
            target = parts[1] if len(parts) > 1 else ""
            if not target:
                raise SystemExit(
                    "Usage: medforge_core assessment create <blueprint_id|scope_node>|<MODE>"
                    "[|<items>[|<minutes>]]"
                )
            mode = (parts[2] if len(parts) > 2 and parts[2] else "PRACTICE").upper()
            count = int(parts[3]) if len(parts) > 3 and parts[3] else None
            minutes = int(parts[4]) if len(parts) > 4 and parts[4] else None
            try:
                result = create_assessment(blueprint_id=target, mode=mode,
                                           item_count=count, time_limit_minutes=minutes)
            except ValueError:
                node = _resolve_scope_node(target)
                if node is None:
                    raise SystemExit(
                        f"No blueprint or curriculum node matches {target!r}."
                    )
                scope_type = (node["node_type"].lower()
                              if node["node_type"].lower() in
                              ("topic", "seminar", "week", "subject") else "custom")
                result = create_assessment(
                    scope_type=scope_type, scope_node_id=node["id"], mode=mode,
                    item_count=count or 10, time_limit_minutes=minutes,
                    title=f"{node['node_type']}: {node['title']}",
                )
            print(json.dumps(result, indent=2))
        elif action == "start":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment start <assessment_id>")
            print(json.dumps(start_assessment(parts[1]), indent=2))
        elif action == "status":
            state = (get_assessment_state(parts[1]) if len(parts) > 1 and parts[1]
                     else resume_assessment())
            print(json.dumps(state, indent=2))
        elif action == "next":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment next <assessment_id>")
            print(json.dumps(next_item(parts[1]), indent=2))
        elif action == "previous":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment previous <assessment_id>")
            print(json.dumps(previous_item(parts[1]), indent=2))
        elif action == "answer":
            if len(parts) < 3:
                raise SystemExit(
                    "Usage: medforge_core assessment answer <assessment_id>|<answer>"
                    "[|<confidence 0-1>]"
                )
            confidence = float(parts[3]) if len(parts) > 3 and parts[3] else None
            print(json.dumps(submit_assessment_answer(
                parts[1], parts[2], confidence=confidence), indent=2))
        elif action == "flag":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment flag <assessment_id>[|<order>][|<reason>]")
            order = int(parts[2]) if len(parts) > 2 and parts[2] else None
            reason = parts[3] if len(parts) > 3 and parts[3] else ""
            print(json.dumps(flag_item(parts[1], order=order, reason=reason), indent=2))
        elif action == "submit":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment submit <assessment_id>")
            print(json.dumps(complete_assessment(parts[1]), indent=2))
        elif action == "result":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment result <assessment_id>")
            print(json.dumps(get_assessment_result(parts[1]), indent=2))
        elif action == "review":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment review <assessment_id>")
            print(json.dumps(review_assessment(parts[1]), indent=2))
        elif action == "remediate":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment remediate <assessment_id>")
            print(json.dumps(get_assessment_remediation(parts[1]), indent=2))
        elif action == "items":
            item_status = parts[1].upper() if len(parts) > 1 and parts[1] else None
            print(json.dumps(list_items(status=item_status), indent=2))
        elif action == "item-info":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment item-info <item_id>[|<version>]")
            version = int(parts[2]) if len(parts) > 2 and parts[2] else None
            item = get_item(parts[1], version)
            item["statistics"] = get_item_statistics(parts[1], version)
            print(json.dumps(item, indent=2))
        elif action == "stats":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core assessment stats <item_id>[|<version>]")
            version = int(parts[2]) if len(parts) > 2 and parts[2] else None
            print(json.dumps(get_item_statistics(parts[1], version), indent=2))
        elif action == "blueprint-new":
            if len(parts) < 4:
                raise SystemExit(
                    "Usage: medforge_core assessment blueprint-new <title>|<scope_node>"
                    "|<scope_type>[|<items>]"
                )
            node = _resolve_scope_node(parts[2])
            items = int(parts[4]) if len(parts) > 4 and parts[4] else 10
            print(json.dumps(create_blueprint(
                parts[1], scope_type=parts[3].lower(),
                scope_node_id=node["id"] if node else parts[2], item_count=items,
            ), indent=2))
        elif action == "item-new":
            # item-new <type>|<stem>|<topic>[|<choices a;b;c>][|<correct A>][|<evidence_id>]
            if len(parts) < 4:
                raise SystemExit(
                    "Usage: medforge_core assessment item-new <TYPE>|<stem>|<topic>"
                    "[|<choices a;b;c>][|<correct A>][|<evidence_id>]"
                )
            item_type = parts[1].upper()
            if item_type not in ASSESSMENT_ITEM_TYPES:
                raise SystemExit(f"item type must be one of {list(ASSESSMENT_ITEM_TYPES)}")
            choices = [c for c in (parts[4].split(";") if len(parts) > 4 and parts[4] else []) if c]
            correct = [c for c in (parts[5].split(";") if len(parts) > 5 and parts[5] else []) if c]
            evidence_id = parts[6] if len(parts) > 6 and parts[6] else ""
            print(json.dumps(create_item(
                item_type, parts[2], topic=parts[3], concept=parts[3],
                choices=choices, correct_choices=correct, correct_answer=correct[0] if correct and not choices else "",
                evidence_refs=[{"evidence_id": evidence_id}] if evidence_id else [],
                source_kind="cli",
            ), indent=2))
        else:
            raise SystemExit(
                "Usage: medforge_core assessment list|blueprints|create|start|status|next|"
                "previous|answer|flag|submit|result|review|remediate|items|item-info|stats|"
                "blueprint-new|item-new"
            )
    elif cmd == "study-intel":
        # P9 study intelligence (orchestrator over P2/P3/P4/P6/P7/P8/SR):
        # study-intel status | recommend [topic][|goal] | gaps | readiness <topic>
        # study-intel plan [minutes|days|objective] | today [plan_id] |
        # study-intel start <topic|--recommended> | engines <mission_id> |
        # study-intel next <mission_id> | complete <mission_id> | finish <mission_id>
        # study-intel missions [status] | mission <id> | history | profile <topic>
        parts = [p.strip() for p in arg.split("|")] if arg else []
        if parts:
            head = parts[0].split(None, 1)
            parts[0] = head[0].lower()
            if len(head) > 1:
                parts.insert(1, head[1])
        action = parts[0] if parts else "status"
        if action == "status":
            print(json.dumps(get_study_status(), indent=2))
        elif action == "recommend":
            topic = parts[1] if len(parts) > 1 and parts[1] else None
            goal = parts[2] if len(parts) > 2 and parts[2] else None
            print(json.dumps(recommend_next_action(topic=topic, goal=goal), indent=2))
        elif action == "gaps":
            print(json.dumps(get_knowledge_gaps(), indent=2))
        elif action == "readiness":
            if len(parts) < 2 or not parts[1]:
                raise SystemExit("Usage: medforge_core study-intel readiness <topic>")
            print(json.dumps(get_readiness(parts[1]), indent=2))
        elif action == "profile":
            if len(parts) < 2 or not parts[1]:
                raise SystemExit("Usage: medforge_core study-intel profile <topic>")
            print(json.dumps(adaptation_profile(parts[1]), indent=2))
        elif action == "plan":
            # Pipe-delimited like the rest of the CLI: plan <minutes>|<days>|<objective>
            if len(parts) < 2 or not parts[1]:
                raise SystemExit(
                    "Usage: medforge_core study-intel plan <minutes>|<days>|<objective>"
                )
            try:
                minutes = int(parts[1])
                days = int(parts[2]) if len(parts) > 2 and parts[2] else 1
            except ValueError:
                raise SystemExit(
                    "Usage: medforge_core study-intel plan <minutes>|<days>|<objective>"
                )
            objective = parts[3] if len(parts) > 3 and parts[3] else ""
            print(json.dumps(build_study_plan(daily_minutes=minutes, days=days,
                                              objective=objective), indent=2))
        elif action == "today":
            print(json.dumps(get_today_plan(
                parts[1] if len(parts) > 1 and parts[1] else None), indent=2))
        elif action == "start":
            target = parts[1] if len(parts) > 1 else ""
            if not target:
                raise SystemExit(
                    "Usage: medforge_core study-intel start <topic|--recommended>"
                )
            topic = None if target.lower() in ("--recommended", "recommended") else target
            print(json.dumps(start_study_mission(topic=topic), indent=2))
        elif action == "engines":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core study-intel engines <mission_id>")
            print(json.dumps(launch_mission_engines(parts[1]), indent=2))
        elif action == "next":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core study-intel next <mission_id>")
            print(json.dumps(get_mission_state(parts[1]), indent=2))
        elif action == "complete":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core study-intel complete <mission_id>")
            print(json.dumps(complete_mission_action(parts[1]), indent=2))
        elif action == "finish":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core study-intel finish <mission_id>")
            print(json.dumps(complete_mission(parts[1]), indent=2))
        elif action == "missions":
            # Avoid binding the name `status` here: it would shadow the legacy
            # `status()` command in this same function scope.
            mission_status = parts[1] if len(parts) > 1 and parts[1] else None
            print(json.dumps(list_missions(status=mission_status), indent=2))
        elif action == "mission":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core study-intel mission <id>")
            print(json.dumps(get_mission_state(parts[1]), indent=2))
        elif action == "history":
            print(json.dumps(get_study_history(), indent=2))
        else:
            raise SystemExit(
                "Usage: medforge_core study-intel status|recommend|gaps|readiness|"
                "profile|plan|today|start|engines|next|complete|finish|missions|"
                "mission|history"
            )
    elif cmd == "product-intel":
        # P9 canonical content + deterministic product rendering:
        # product-intel build <topic>[|types|outdir] | status <content_id> |
        # product-intel inspect <content_id> | artifacts [content_id] |
        # product-intel provenance <artifact_id> | consistency <content_id> |
        # product-intel regenerate <content_id>[|outdir]
        parts = [p.strip() for p in arg.split("|")] if arg else []
        if parts:
            head = parts[0].split(None, 1)
            parts[0] = head[0].lower()
            if len(head) > 1:
                parts.insert(1, head[1])
        action = parts[0] if parts else ""
        if action == "build":
            topic = parts[1] if len(parts) > 1 else ""
            if not topic:
                raise SystemExit(
                    "Usage: medforge_core product-intel build <topic>[|types|outdir]"
                )
            artifact_types = [t.strip() for t in parts[2].split(",") if t.strip()] \
                if len(parts) > 2 and parts[2] else None
            outdir = Path(parts[3]) if len(parts) > 3 and parts[3] else None
            try:
                item = generate_canonical_content(topic)
            except RuntimeError as exc:
                # Honest abstention (e.g. no evidence) — data, not a crash.
                print(json.dumps({"created": False, "refused": str(exc)}, indent=2))
                return
            print(json.dumps({
                "content_id": item["content_id"],
                "generation_mode": item["generation_mode"],
                "cached": item["cached"],
                "render": render_study_products(
                    item["content_id"], artifact_types=artifact_types, outdir=outdir),
            }, indent=2))
        elif action == "status":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core product-intel status <content_id>")
            print(json.dumps(get_product_status(parts[1]), indent=2))
        elif action == "inspect":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core product-intel inspect <content_id>")
            print(json.dumps(get_content(parts[1]), indent=2))
        elif action in ("artifacts", "list"):
            cid = parts[1] if len(parts) > 1 and parts[1] else None
            print(json.dumps(list_artifacts(content_id=cid), indent=2))
        elif action == "provenance":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core product-intel provenance <artifact_id>")
            print(json.dumps(artifact_provenance(parts[1]), indent=2))
        elif action == "consistency":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core product-intel consistency <content_id>")
            print(json.dumps(content_consistency_report(parts[1]), indent=2))
        elif action == "regenerate":
            if len(parts) < 2:
                raise SystemExit("Usage: medforge_core product-intel regenerate <content_id>")
            outdir = Path(parts[2]) if len(parts) > 2 and parts[2] else None
            print(json.dumps(regenerate_product(parts[1], outdir=outdir), indent=2))
        else:
            raise SystemExit(
                "Usage: medforge_core product-intel build|status|inspect|artifacts|"
                "provenance|consistency|regenerate"
            )
    elif cmd == "migrate":
        with job_lock():
            print(json.dumps(migrate_database(), indent=2))
    elif cmd == "doctor":
        with job_lock():
            model = ensure_models()
            import sqlite3
            con = sqlite3.connect(META_DB)
            try:
                check = con.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                con.close()
            if check != "ok":
                raise RuntimeError("Metadata database check: " + check)
            get_collection().count()
            print("READY: chat, embeddings, SQLite and vector store checked. Model:", model)
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    else:
        raise SystemExit("Unknown command.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Rerun the same command to resume saved work.")
        sys.exit(130)
    except Exception as e:
        mkdirs()
        with open(LOGS / "last-error.log", "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        print(f"\nMedForge stopped: {e}\nSaved work is retained. Details: {LOGS / 'last-error.log'}", file=sys.stderr)
        sys.exit(1)
