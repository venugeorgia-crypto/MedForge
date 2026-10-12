# MedForge User Manual — v2.1.0

MedForge is a **local-first medical study platform** for Apple Silicon Macs.
Everything runs on your machine — Ollama for AI, SQLite for records, ChromaDB
for search, Streamlit for the dashboard. Your learner data never leaves the
computer. One topic in → a complete, traceable workflow out: evidence
retrieval → tutoring → assessment → study products → quality-checked exports.

> **Medical disclaimer:** every generated pack is a **draft requiring source
> review**. Citation labels (`[S1]`) are pointers, not proof — only a
> verification status (SUPPORTED / PARTIALLY_SUPPORTED / UNSUPPORTED /
> CONTRADICTED / INSUFFICIENT_EVIDENCE) counts as evidence. Doses,
> contraindications, emergencies, diagnostic criteria, procedures and
> treatments always need explicit provenance and human review. Nothing in
> MedForge is medical advice.

---

## 1. Requirements

| Need | Detail |
| --- | --- |
| Mac | Apple Silicon (M1 or newer), macOS 12 (Monterey) or later |
| Python | 3.10–3.14 (3.12 used for development/testing) |
| Ollama | Running at `127.0.0.1:11434`, with `qwen3:4b-instruct` and `embeddinggemma` pulled |
| Disk/RAM | ~2 GB for the app + models; 8 GB RAM is enough |
| Video (optional) | `ffmpeg`, `ffprobe`, `pillow` |
| Anki sync (optional) | Anki desktop running with the AnkiConnect add-on (`127.0.0.1:8765`) |

Install Ollama models once:

```bash
ollama pull qwen3:4b-instruct
ollama pull embeddinggemma
```

## 2. Installation

Pick **one** path.

**A. Recommended — `INSTALL-MEDFORGE.command`.** Double-click it in Finder
(or run `bash INSTALL-MEDFORGE.command`). It verifies its own payload
checksum, installs the engine under `~/MedForge` (or `$MEDFORGE_HOME`), sets
up the database, and runs first-time bootstrap. Verify a download without
installing:

```bash
bash INSTALL-MEDFORGE.command --verify
```

**B. Installer package — `MedForge-2.1.0.pkg`.** Double-click; it installs
`MedForge.app` into `/Applications` and refuses macOS older than 12.0.
The package is currently **unsigned**, so right-click → Open on first launch
if Gatekeeper objects.

**C. Disk image — `MedForge-2.1.0.dmg`.** Open it, drag `MedForge.app` into
`Applications`, read the included `README.txt`.

**D. From source (developers).**

```bash
git clone https://github.com/venugeorgia-crypto/MedForge.git
cd MedForge/releases/2.1.0-20935f509d37
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python medforge_core.py doctor     # readiness check
streamlit run dashboard.py         # dashboard
```

## 3. First run checklist

1. Start Ollama (`ollama serve`) and confirm the two models are pulled.
2. Run the readiness check: `python medforge_core.py doctor`
   (from source) or the `launch verify` command.
3. Open the dashboard (Streamlit URL, default `127.0.0.1:8501`).
4. Import your syllabus: **CURRICULUM** tab → paste weekly/seminar syllabus.
   This builds the Semester → Subject → Week → Seminar → Topic → Subtopic →
   Learning-Objective tree everything else hangs off.
5. Register a textbook (**TEXTBOOKS** tab) so claims get real provenance.
6. Press **Start Today's Study** on the HOME tab.

## 4. Daily workflow

**HOME → Start Today's Study** is the single entry point. One click
orchestrates curriculum → evidence → learner model → tutor → assessment →
products → quality gates, and tells you *why* each step was chosen. The same
flow from the terminal:

```bash
python medforge_core.py study-today [topic] [|budget_minutes] [|run_tutor] ...
python medforge_core.py study-history [topic] [|format] [|output_path]
```

## 5. Dashboard tabs

HOME • PRODUCT • STUDY • REVIEW • LEARNER • TUTOR • ASSESSMENTS • ASK •
CURRICULUM • TEXTBOOKS • EVIDENCE • VIDEOS • STUDY PLAN • LIBRARY • HISTORY •
SYSTEM • EVALUATION

| Tab | Use it for |
| --- | --- |
| HOME | Start Today's Study; global SEARCH across curriculum, evidence, products, history |
| PRODUCT | Generate a study pack (guide, cheat sheet, flashcards, quiz, mind map, script) |
| STUDY / STUDY PLAN | Plans and missions; step-by-step execution |
| REVIEW | Spaced-repetition queue (SM-2/FSRS), retention heatmap, leeches |
| LEARNER | Mastery snapshot, weaknesses, confidence calibration |
| TUTOR | Adaptive teach→ask→evaluate sessions (7 modes), evidence-first |
| ASSESSMENTS | Versioned item bank, blueprints, PRACTICE / EXAM / REVIEW sessions |
| ASK | Single evidence-grounded Q&A |
| CURRICULUM / TEXTBOOKS | Syllabus import, prerequisite graph, textbook editions + links |
| EVIDENCE | Claims, provenance, verification verdicts |
| VIDEOS | Storyboard → narrated MP4 with captions (16:9, 9:16, 1:1) |
| LIBRARY / HISTORY | Past products; unified study history trace |
| SYSTEM | Environment, diagnostics, backups, launch controls |
| EVALUATION | Golden-set regression runs for the QA pipeline |

## 6. Keyboard shortcuts (dashboard)

Shortcuts are ignored while typing in a text field.

| Keys | Action |
| --- | --- |
| Cmd/Ctrl + K | Focus global search |
| Cmd/Ctrl + T | New study session |
| Cmd/Ctrl + H | Home |
| Cmd/Ctrl + R | Review (spaced repetition) |
| Cmd/Ctrl + S | Study |
| Cmd/Ctrl + A | Assessments |
| Cmd/Ctrl + E | Evidence |
| Cmd/Ctrl + ? | Show shortcut help |

## 7. CLI quick reference

```bash
python medforge_core.py product "cardiac cycle"        # full study pack
python medforge_core.py ask "what triggers GH release?" # grounded Q&A
python medforge_core.py study "cardiac cycle"           # interactive mission
python medforge_core.py due                             # due SR cards
python medforge_core.py review <item_id> 4 fsrs        # grade review 0–5 (sm2|fsrs)
python medforge_core.py assessment psychometrics demo  # offline IRT/CAT/DIF demo
python medforge_core.py assessment psychometrics irt 0.5\|1.2\|0.0
python medforge_core.py drug check "warfarin|aspirin"  # interaction check
python medforge_core.py drug scan <notes-file>        # scan text for interactions
python medforge_core.py fsrs params|optimize|heatmap|sweep
python medforge_core.py anki check|sync|import        # AnkiConnect deck sync
python medforge_core.py launch verify|run|restart|upgrade|rollback|diagnose
python medforge_core.py study-today [<topic>] [|budget_minutes] ...
python medforge_core.py diagnose [full|verify-backup|restore|repair]
```

Full reference: `docs/CLI_USAGE.md`.

## 8. Spaced repetition

Two schedulers: classic **SM-2** and **FSRS** (stability/difficulty based).
`REVIEW` shows due cards, retention heatmap, and leeches (repeatedly failed
cards, which also raise learner-model weaknesses). Tune per deck:

```bash
python medforge_core.py fsrs params     # current parameters per deck
python medforge_core.py fsrs optimize   # fit parameters to your history
python medforge_core.py fsrs heatmap    # 7×4 retention grid
python medforge_core.py fsrs sweep      # parameter sweep report
```

## 9. Assessments

Six item types (MCQ single/multi, true/false, recall, short answer, clinical
reasoning), versioned so an old exam never changes meaning when an item is
edited. Three session modes: **PRACTICE** (immediate feedback), **EXAM**
(feedback withheld), **REVIEW** (teaching support). Grading reuses the tutor
engine; model failures never lose answers and never award credit (attempts
become retryable). Scores feed the learner model and remediation, which points
at weak topics — it never starts tutor sessions by itself.

The built-in psychometric panel reports IRT difficulty/discrimination,
test information, item fit, and Mantel-Haenszel DIF flags. Treat these as
**provisional, small-sample estimates** — the dashboard labels them as such,
and they are not competence claims.

## 10. Anki sync

With Anki + AnkiConnect running, push any product's flashcards straight into
a deck, or import `.apkg` files back. CLI: `anki check` (connection),
`anki sync` (push deck), `anki import` (pull package). No Anki? Use the
standard `.apkg`/CSV exports from the LIBRARY tab instead.

## 11. Drug interaction checker

Cross-references prescribed medications against a built-in interaction and
contraindication base (30+ interactions): `drug check "drugA|drugB"` for a
pair, `drug scan` to sweep your flashcards or notes for risky combinations.
Findings are alerts for review with a clinician/pharmacist — not prescribing
decisions.

## 12. Exports

- **Anki** (`.apkg`, CSV), **PDF** study packs, **Obsidian/Notion** Markdown
  with YAML frontmatter and bidirectional links.
- **Private vs distributable:** private bundles may contain learner history
  and evidence excerpts; distributable bundles contain only rendered
  artifacts + reference locators. Learner fields, prompts and long verbatim
  runs are rejected at export time (covered by 18 automated privacy tests).

## 13. Videos

Render narrated explainer videos (storyboard → frames → TTS → MP4 + SRT/VTT
captions) in 16:9, 9:16 or 1:1, including SVG anatomical diagrams (e.g. heart
chambers, HPA axis). Needs `ffmpeg`/`ffprobe`.

## 14. Refresh, automation, maintenance

- **Knowledge refresh:** detects new textbook editions (hash diff), PubMed
  set changes and web updates, then re-verifies affected claims.
- **Automation:** nightly workflow (maintenance → refresh → verified backup →
  study prep) via `launchd`; every run is audited.
- **Backups & upgrades:** every database change is additive with a verified
  backup and integrity checks. `launch verify` (environment),
  `launch upgrade` / `launch rollback` (releases),
  `diagnose verify-backup|restore|repair` (data emergencies).

## 15. Privacy & data

Local-first: the database (`database/`), backups (`backups/`), logs and
products live on your Mac and are excluded from version control. Installer
packages ship **code only — never your database**. Uninstalling = deleting
the app plus `~/MedForge` (export anything you want to keep first).

## 16. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Dashboard won't start | `python medforge_core.py doctor`; check Ollama is serving |
| Tutor/grading hangs | Model missing — `ollama pull qwen3:4b-instruct`; retry re-grades the same attempt, answers are never lost |
| Anki sync fails | Anki open with AnkiConnect add-on? `anki check` |
| Assessment session lost | Sessions persist — `assessment status` resumes; restart recovery never duplicates events |
| Database concern | `diagnose verify-backup`, then `restore` if needed; `repair` for FTS issues |
| macOS Gatekeeper blocks .pkg/.app | Right-click → Open (unsigned test builds) |

## 17. Folder layout (source checkout)

```text
MedForge/
├── USER_MANUAL.md            this file
├── SESSION_MEMORY.md         session handover notes (this + prior sessions)
├── README.md / AGENTS.md / knowledge.md
├── INSTALL-MEDFORGE.command  self-extracting installer (generated, canonical)
├── MEDFORGE.command          launcher shim
├── MedForge.app / .pkg / .dmg   desktop artifacts (unsigned test builds)
├── releases/2.1.0-20935f509d37/ active source (medforge/, dashboard.py, CLI)
├── core/                     DB schema + migrations (V3→V16)
├── docs/                     architecture + per-phase matrices
├── tests/                    pytest suite (477 passed, 1 skipped)
├── tools/                    installer builder, distribution XML
├── updates/                  Sparkle appcast draft
├── database/ backups/ logs/ products/ tmp/   local runtime data (git-ignored)
└── current -> releases/...   machine-specific active-release symlink
```

Run the test suite any time with `.venv-v2.1/bin/python -m pytest tests/ -q`.
