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
    init_db, get_collection, upsert_records, migrate_database,
    ensure_v3_tables, start_session, record_session, update_mastery,
    record_weakness, resolve_weakness, log_study_result, learner_snapshot,
    card_item_id, import_flashcards, due_items, review_card, review_analytics,
    spaced_repetition_snapshot, SCHEDULERS, DEFAULT_SCHEDULER,
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
    elif cmd == "mastery":
        print(json.dumps(learner_snapshot(), indent=2))
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
