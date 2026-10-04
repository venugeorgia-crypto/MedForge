# MedForge 2.1 — one-command reliability update

Continue the recovered MedForge V2 source. This package improves installation,
model checks, recovery, citation reporting and the local dashboard. The master
prompt describes the larger V3 target; it is not a claim that all V3 features exist.

## Run once on your Mac

Download `MEDFORGE-ONE-COMMAND.command` to Downloads, then paste:

```bash
bash "$HOME/Downloads/MEDFORGE-ONE-COMMAND.command"
```

It checks Python, installs packages in a private environment, finds/starts
Ollama, tests embeddings and a small local chat model, creates the commands,
and opens the local browser dashboard. The first installation may download
several GB. Generation takes time; one command does not mean instant output.

For installation followed immediately by a product, supply its topic:

```bash
bash "$HOME/Downloads/MEDFORGE-ONE-COMMAND.command" "cardiac cycle"
```

After setup, open a new Terminal and use:

```bash
product "cardiac cycle"
```

Or double-click `~/MedForge/MEDFORGE.command`. `medforge` opens the dashboard,
`medstudy "topic"` creates a study mission, and `medask "question"` asks the
indexed evidence. `medforge doctor` checks model and database readiness.

## What is included

- Local Ollama chat and embedding checks; exact model tags, capability checks,
  conservative model-size selection, one MedForge generation job at a time.
- Existing installed chat models are tested; if necessary, downloads
  `qwen3:4b-instruct`, then tries `qwen3:1.7b`. Embeddings use `embeddinggemma`.
- PDF indexing, PubMed abstracts, fetched authoritative web text, hybrid
  keyword/vector retrieval, bounded source context, and cached/offline use.
- Study guide, workbook and cheat sheet PDFs; `.apkg` and CSV cards; quiz,
  viva, case exercises, mind map, short/explainer scripts and carousel text.
- Fixed source snapshots, atomic state writes, file checksums for resumed
  generated text, versioned products, history and a downloadable pack.
- Local dashboard bound to `127.0.0.1`, automatic browser opening, safe PDF
  upload filenames, and friendly errors with saved diagnostic logs.

## Medical evidence status

Every generated pack is a **draft requiring source review**. The citation scan
includes bullets, unknown labels and flashcards. It checks label format, not
whether the cited passage proves the claim. It can flag nonmedical text too.
The heuristic relevance cutoff is not a medical relevance guarantee. Domain
weights are ranking hints, not percentages of correctness or study quality.

All factual material remains subject to human review before publication.
Source excerpts are included for private verification; remove copyrighted
excerpts and check reuse permissions before distributing a pack.

## Compatibility and data

The target is the user's M1 Mac, 8 GB, with Python and Ollama already present.
Python 3.10–3.14 is accepted; this build was checked on Linux/Python 3.12,
not on the user's Mac/Python 3.14. If Ollama is missing and Homebrew exists,
the installer uses Homebrew. Otherwise the official Ollama install remains
a prerequisite. There is no hidden paid API or bundled cloud GPT model.

Existing `docs`, `output`, V1 databases and application files remain in place.
V2 databases continue at `~/MedForge/database`; V1 vectors are not migrated
into V2 automatically (PDFs in `docs` can be reindexed). Commands are backed
up. Releases are installed in `~/MedForge/releases`, selected by `current`.
A separate `.venv-v2.1` avoids replacing the old `.venv`.

New V2.1 jobs resume compatible snapshots. An unfinished V2 job is retained
and gets a new version because its citation mapping cannot be trusted on resume.
Updating a known PDF retires that file's old chunks after successful indexing.
Removing a PDF from `docs` does not purge previously indexed text in this release.

Normal research requires internet. Offline mode requires installed models and
relevant saved evidence. Free web search can be rate-limited; PubMed or cached
sources may still work. Generation needs the Mac awake; lid closure is not
promised to keep it running.

## Explicitly pending for V3

Semantic claim verification; automatic OCR; scored interactive study sessions;
weakness scheduling; curriculum completion; daily knowledge updates; narrated
MP4 output; illustrated carousel exports; publication approval workflow.
The old V2 installer did not implement these despite earlier broad descriptions.
They are acceptance targets in `MEDFORGE-ASTRA-MASTER-PROMPT.md`.

## Engineering references

- [GPT-6 Astra prompting guidance](https://developers.openai.com/api/docs/guides/latest-model)
- [Ollama chat API](https://docs.ollama.com/api/chat)
- [Exact local model tag](https://ollama.com/library/qwen3:4b-instruct)
- [Streamlit configuration](https://docs.streamlit.io/develop/api-reference/configuration/config.toml)

Selecting Astra for the coding conversation is separate from MedForge's local
runtime. No installer command changes a ChatGPT conversation's selected model.
