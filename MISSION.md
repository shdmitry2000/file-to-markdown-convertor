# Mission

<!--
  Owner: humans only. This file is on the protected list; the factory cannot edit it.
-->

**Derived from:** `README.md`, `docs/`, and the code as of v1.6.4
**Last reconciled with it:** 2026-11-15

## What the converter is

A service that turns business documents (PDF, Word, PowerPoint, Excel, images) into
clean, faithful Markdown and chunks for the RAG platform. It keeps the meaning of the
document (tables, headings, Excel formulas and their cached results), not just the
visible text.

It runs as one FastAPI API with ZeroMQ workers, which can also run embedded inside the
API process. It is a single-tenant internal service, called by `rag_eval_bank`.

## Who it is for

- The RAG platform (`rag_eval_bank`) and its developers, who need documents turned into
  Markdown and chunks they can index without losing tables or numbers.

The converter is not a search engine, a chatbot, or a document store.

## Core capabilities (in scope)

**Conversion**
- PDF, Word, PowerPoint, Excel (`.xlsx`, legacy `.xls`), images to Markdown with frontmatter
- Converter backends: Docling, dbank, Excel (keeps formulas), slides, VLM, markitdown
- Async jobs: submit, poll status, fetch content, cancel

**Chunking**
- `POST /chunk`, `/chunk-async` on converted Markdown

**Operations**
- `/health`, `/build-id`, `/capabilities`, queue visibility, timeouts, worker scaling

## Out of scope -- the factory must never build this

**Other parts of the RAG platform**
- Embeddings, vector storage or retrieval. That belongs to `rag_eval_bank`.
- Answering questions about documents, or summarizing them.

**Other directions of conversion**
- Converting Markdown back into PDF, Word or Excel, or editing source documents.

**Product surfaces it does not have**
- Its own end-user web UI or document viewer.
- Its own user accounts, login or billing.
- A document management system: versioning, search, or long-term storage of originals.

**Ingestion**
- Fetching documents from URLs, email, SharePoint or Drive. Callers hand it files.

## Hard invariants -- not tunable by any issue

1. **Never invent content.** A missing Excel value stays `(no cached result)`; nothing is
   recalculated or guessed. Wrong numbers in a bank's RAG are worse than missing ones.
2. **A cancelled conversion ends as `cancelled`, never `failed`.** Callers treat the two
   differently.
3. **Concurrent requests for the same file share one `conversion_id`.** While a
   conversion is pending or processing, a second submit of the same file gets the same
   id instead of queuing a duplicate.
4. **Core PDF, Word and Excel conversion works with no LLM key configured.** VLM paths
   are an addition, never a dependency.
5. **The factory cannot modify governance files.** `MISSION.md`, `FACTORY_RULES.md` and
   the conventions file are the constitution. A PR touching any of them is an automatic
   reject.
6. **The factory cannot modify its own judge.** `harness/` and `.factory/holdout/` define
   what "working" means here. Adding an assertion is always welcome; removing or
   loosening one is a human decision, always.

## Allowed evolutions

- Adding a GitHub Actions workflow that runs the unit gate (first issue).
- Better fidelity on tables, headings, RTL/Hebrew text, and Excel layouts.
- Performance, timeouts, and worker scaling, as long as the invariants hold.

## Definition of done

**Gate 1 -- tests pass.** `uv run --extra test pytest tests/ -q` (no LLM keys; VLM paths
skipped or mocked).

**Gate 2 -- no fidelity regression.** Output for the existing fixtures does not lose
text, tables or formulas it had before.

**Gate 3 -- the end-to-end path passes as a real user.** The journeys in
`harness/END-TO-END.md`.

## Open questions -- decisions nobody has made yet

These are undecided, not forbidden. **The factory may propose an answer to any of
them**, build against it, and record what it assumed; the merge is then held for a
human.

- **Q1** Should results persist across an API restart, or stay in memory?
- **Q2** What is the maximum file size the API accepts?

**Except these, which do stop the factory:**

- Adding any authentication or caller identity to the API.
- Deleting or migrating stored conversion results.

## Decisions

- 2026-11-15 **Is `shared/` vendored into this repo?** Yes, the 49-module import closure (PR #2).

## What the factory does NOT own -- permanently human

- Whether the Markdown READS well to a person or an LLM: layout, ordering, noise.
- Which converter backend is the right default for a document type.
- Whether a fidelity trade-off (speed vs. accuracy) is acceptable for the business.

The factory owns the conversion rules, the job lifecycle and the API contract: the layer
whose correctness can be asserted.
