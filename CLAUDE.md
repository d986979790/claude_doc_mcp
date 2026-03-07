# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

This repo is a **local FastMCP demo server** for Q&A over EDA user guides (built-in defaults: `vcs`, `vc_formal`; and supports arbitrary guides via explicit `guide_id`).

- Main implementation: `vcs_mcp_demo_server.py`
- MCP wiring for Claude Code: `.mcp.json`
- Product/design context: `PRD_FastMCP_VCS_Guide_QA.md`
- Usage examples: `README_DEMO.md`
- Source PDFs (examples): `vcs_user_guide.pdf`, `VC_Formal_UserGuide.pdf`
- Runtime cache/output: `.vcs_mcp_demo/` (indexes + request snapshots)
- Dynamic guide registry: `.vcs_mcp_demo/guides.json` (auto-created when onboarding a new guide)

## Common commands

Run from repository root.

### Environment setup

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install pypdf fastmcp
```

### Health check

```bash
.venv/bin/python vcs_mcp_demo_server.py --health
```

### Build index

> `--guide` 默认是 `vcs`，不传时保持原有 VCS 行为。
>
> 从本版本开始支持“任意文档接入”：首次构建新文档时，显式传入 `--guide <guide_id>` 与 `--pdf-path <file.pdf>` 即可自动注册。
> - `guide_id` 规则：`[a-z0-9_-]+`（会归一化为小写）
> - 首版范围：单 PDF 对应单 guide

Full VCS:

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide vcs --pdf-path ./vcs_user_guide.pdf --force-rebuild
```

Full VC Formal:

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide vc_formal --pdf-path ./VC_Formal_UserGuide.pdf --force-rebuild
```

Onboard any new guide (first build auto-registers):

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide pt_shell --pdf-path ./PrimeTime_User_Guide.pdf --force-rebuild
```

Quick demo (first 80 pages):

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide vcs --pdf-path ./vcs_user_guide.pdf --max-pages 80 --force-rebuild
```

### Ask questions

```bash
.venv/bin/python vcs_mcp_demo_server.py --ask "VCS two-step flow 和 three-step flow 有什么区别？" --guide vcs --top-k 5
```

### Fetch evidence by request id

```bash
.venv/bin/python vcs_mcp_demo_server.py --get-evidence <request_id> --guide vcs --limit 3
```

### Run as MCP server (stdio)

No CLI flags means MCP server mode:

```bash
.venv/bin/python vcs_mcp_demo_server.py
```

### Tests / lint

There is currently no committed lint configuration and no tracked test source files in git. If tests are added locally with pytest, use:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_file.py::test_case -q
```

## Architecture (big picture)

### LLM integration and boundaries

- This repository is a **retrieval + evidence service** (indexing, ranking, citations), not a hosted LLM inference backend.
- In MCP mode, Claude Code (the client-side LLM runtime) calls this server via tools (`build_vcs_index`, `ask_vcs_guide`, `get_vcs_evidence`, `health_check`).
- Model selection is handled by the Claude Code session/runtime, not hardcoded in this repo.
- The server returns structured retrieval results; the LLM layer is responsible for final conversational formatting for end users.

### 1) Dual entrypoint: CLI mode vs MCP server mode

In `vcs_mcp_demo_server.py`, `main()` parses CLI args. If any action flag is present (`--build-index`, `--ask`, `--get-evidence`, `--health`), it runs local CLI flow. Otherwise it starts `mcp.run()` for stdio MCP server mode.

### 2) Tool surface exposed to Claude/MCP clients

The server exposes four MCP tools:

- `build_vcs_index`
- `ask_vcs_guide`
- `get_vcs_evidence`
- `health_check`

These map directly to the same core functions used by CLI mode.

### 3) Data model and storage layout

All runtime artifacts are stored under `.vcs_mcp_demo/`:

- `indexes/<guide>.json` for per-guide index data
- `requests/<guide>/<request_id>.json` for Q&A snapshots/evidence replay

There is compatibility handling for legacy VCS index path `.vcs_mcp_demo/index.json`.

### 4) Guide routing and normalization

Guide keys are normalized by a merged registry model:

- built-in defaults (`vcs`, `vc_formal`) for backward compatibility
- runtime registry from `.vcs_mcp_demo/guides.json`
- alias resolution (e.g. `vcformal`, `vc-formal` -> `vc_formal`)

New guides are registered on first successful `build_vcs_index --guide <guide_id> --pdf-path <file.pdf>`.

Constraints:

- `guide_id` must match `[a-z0-9_-]+` (normalized to lowercase)
- alias conflicts are rejected during registration

### 5) Index build pipeline

`build_vcs_index` performs:

1. Workspace/path validation (`_ensure_path_within_workspace`)
2. PDF text extraction (`pypdf`, fallback to `pdftotext`)
3. Page chunking (`_split_text_to_chunks`)
4. Metadata enrichment per chunk:
   - token frequencies
   - option extraction (e.g. `-debug_access`)
   - section path inference
   - TOC-like chunk detection
5. Corpus statistics (`idf`, average chunk length, TF-IDF norm)
6. Persisted index JSON with retrieval version (`v2_hybrid_bm25_tfidf`)

### 6) Retrieval and answer flow

`ask_vcs_guide` loads guide index, retrieves top-k evidence, then composes answer + citations.

Retrieval in `_score_chunk` is hybrid:

- BM25-like lexical score
- TF-IDF cosine score
- Option hit boost
- Phrase/heading boosts
- TOC-like penalty

`_retrieve` prefers non-TOC chunks first, then fills remaining slots if needed. Each response includes `retrieval_debug`, confidence, and limitations.

### 7) Evidence replay and observability

Each ask call writes a request snapshot keyed by `request_id`. `get_vcs_evidence` reads this payload and returns answer + (optionally truncated) citations for audit/review.

### 8) MCP integration details

`.mcp.json` is preconfigured with server name `vcs-doc`, launching this repo’s venv Python with `vcs_mcp_demo_server.py` as stdio backend.
