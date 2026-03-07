from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from pypdf import PdfReader
except ModuleNotFoundError:  # pragma: no cover - runtime dependency
    PdfReader = None  # type: ignore[assignment]

try:
    from fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - optional for CLI fallback mode
    FastMCP = None  # type: ignore[assignment]


class NoopMCP:
    """Fallback MCP wrapper so demo can run in CLI mode without fastmcp installed."""

    def tool(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        return fn

    def run(self) -> None:
        raise RuntimeError(
            "fastmcp 未安装，无法以 MCP Server 模式启动。"
            "请先安装 fastmcp，或使用 --build-index / --ask 运行本地 CLI Demo。"
        )


mcp = FastMCP("vcs-guide-mcp-demo") if FastMCP else NoopMCP()

WORKSPACE_ROOT = Path.cwd().resolve()
DATA_ROOT = (WORKSPACE_ROOT / ".vcs_mcp_demo").resolve()
INDEXES_DIR = (DATA_ROOT / "indexes").resolve()
REQUESTS_ROOT = (DATA_ROOT / "requests").resolve()
LEGACY_VCS_INDEX_PATH = (DATA_ROOT / "index.json").resolve()

DEFAULT_GUIDES: dict[str, dict[str, Any]] = {
    "vcs": {
        "label": "VCS User Guide",
        "default_pdf": "vcs_user_guide.pdf",
        "version_hint": "请确认你的 VCS 版本与手册版本一致（X-2025.06-SP1）",
        "aliases": ["vcs"],
    },
    "vc_formal": {
        "label": "VC Formal User Guide",
        "default_pdf": "VC_Formal_UserGuide.pdf",
        "version_hint": "请确认你的 VC Formal 版本与手册版本一致",
        "aliases": ["vcformal", "vc-formal", "vc_formal"],
    },
}
GUIDES_REGISTRY_PATH = (DATA_ROOT / "guides.json").resolve()
GUIDE_ID_RE = re.compile(r"^[a-z0-9_-]+$")

TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+-]+")
OPTION_RE = re.compile(r"-[A-Za-z0-9_]+")
HEADING_NUMBER_RE = re.compile(r"^(\d+(\.\d+){0,4})\s+\S+")
TOC_DOT_LINE_RE = re.compile(r"\.{3,}\s*\d+\s*$")
TOC_SHORT_LINE_RE = re.compile(r"^[A-Za-z].{3,90}\s+\d{1,4}$")

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "with",
    "和",
    "与",
    "以及",
    "怎么",
    "如何",
    "什么",
    "区别",
    "用法",
    "功能",
    "问题",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_path_within_workspace(target: Path) -> None:
    root = WORKSPACE_ROOT.resolve()
    resolved_target = target.resolve()
    if resolved_target != root and root not in resolved_target.parents:
        raise ValueError(f"E9001: 目标路径越界，不允许访问 {resolved_target}")


def _sanitize_guide_id(guide: str | None) -> str:
    raw = (guide or "vcs").strip().lower()
    if not raw:
        raw = "vcs"
    if not GUIDE_ID_RE.match(raw):
        raise ValueError("E1001: guide_id 仅允许小写字母、数字、下划线或连字符（[a-z0-9_-]+）")
    return raw


def _normalize_guide_meta(guide_id: str, raw_meta: dict[str, Any] | None) -> dict[str, Any]:
    meta = raw_meta or {}
    aliases_raw = meta.get("aliases", [])
    aliases: list[str] = []
    if isinstance(aliases_raw, list):
        for item in aliases_raw:
            if not isinstance(item, str):
                continue
            alias = item.strip().lower()
            if not alias:
                continue
            if GUIDE_ID_RE.match(alias):
                aliases.append(alias)

    default_label = " ".join(part for part in re.split(r"[_-]+", guide_id) if part).title() or guide_id
    return {
        "label": str(meta.get("label") or default_label),
        "default_pdf": str(meta.get("default_pdf") or ""),
        "version_hint": str(meta.get("version_hint") or "请确认你的文档版本与当前索引一致"),
        "aliases": sorted(set(aliases)),
    }


def _load_runtime_guides() -> tuple[dict[str, dict[str, Any]], list[str]]:
    warnings: list[str] = []
    _ensure_path_within_workspace(GUIDES_REGISTRY_PATH)

    if not GUIDES_REGISTRY_PATH.exists():
        return {}, warnings

    try:
        payload = json.loads(GUIDES_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(f"E2002: guides 注册表解析失败，已回退默认 guide。detail={exc}")
        return {}, warnings

    if not isinstance(payload, dict):
        warnings.append("E2002: guides 注册表格式错误（需为 JSON object），已回退默认 guide。")
        return {}, warnings

    raw_guides = payload.get("guides", {})
    if not isinstance(raw_guides, dict):
        warnings.append("E2002: guides 注册表字段 guides 非 object，已回退默认 guide。")
        return {}, warnings

    runtime_guides: dict[str, dict[str, Any]] = {}
    for raw_id, raw_meta in raw_guides.items():
        if not isinstance(raw_id, str):
            warnings.append("E2002: guides 注册表包含非法 guide_id（非字符串），已忽略。")
            continue
        try:
            guide_id = _sanitize_guide_id(raw_id)
        except ValueError:
            warnings.append(f"E2002: guides 注册表包含非法 guide_id={raw_id}，已忽略。")
            continue

        if raw_meta is None:
            raw_meta_dict: dict[str, Any] = {}
        elif isinstance(raw_meta, dict):
            raw_meta_dict = raw_meta
        else:
            warnings.append(f"E2002: guide={guide_id} 的元数据不是 object，已忽略。")
            continue

        runtime_guides[guide_id] = _normalize_guide_meta(guide_id, raw_meta_dict)

    return runtime_guides, warnings


def _build_merged_guides(runtime_guides: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    merged: dict[str, dict[str, Any]] = {
        gid: _normalize_guide_meta(gid, meta)
        for gid, meta in DEFAULT_GUIDES.items()
    }

    for gid, meta in runtime_guides.items():
        normalized_meta = _normalize_guide_meta(gid, meta)
        if gid in merged:
            base = merged[gid]
            merged[gid] = {
                "label": normalized_meta["label"] or base["label"],
                "default_pdf": normalized_meta["default_pdf"] or base["default_pdf"],
                "version_hint": normalized_meta["version_hint"] or base["version_hint"],
                "aliases": sorted(set(base.get("aliases", []) + normalized_meta.get("aliases", []))),
            }
        else:
            merged[gid] = normalized_meta

    alias_map: dict[str, str] = {}
    for gid, meta in merged.items():
        candidate_aliases = [gid] + list(meta.get("aliases", []))
        for alias in candidate_aliases:
            owner = alias_map.get(alias)
            if owner and owner != gid:
                raise ValueError(f"E1001: guide alias 冲突 alias={alias} 被 {owner} 与 {gid} 同时占用")
            alias_map[alias] = gid

    return merged, alias_map


def _load_guide_registry() -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    runtime_guides, warnings = _load_runtime_guides()
    merged_guides, alias_map = _build_merged_guides(runtime_guides)
    return merged_guides, alias_map, warnings


def _save_runtime_guides(runtime_guides: dict[str, dict[str, Any]]) -> None:
    _ensure_path_within_workspace(DATA_ROOT)
    _ensure_path_within_workspace(GUIDES_REGISTRY_PATH)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {"guides": runtime_guides}
    GUIDES_REGISTRY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _register_guide(guide_id: str, meta: dict[str, Any]) -> None:
    normalized_id = _sanitize_guide_id(guide_id)
    runtime_guides, warnings = _load_runtime_guides()
    if warnings:
        raise ValueError("; ".join(warnings))

    runtime_guides[normalized_id] = _normalize_guide_meta(normalized_id, meta)
    _build_merged_guides(runtime_guides)
    _save_runtime_guides(runtime_guides)


def _normalize_guide(guide: str | None, allow_missing: bool = False) -> str:
    raw = _sanitize_guide_id(guide)
    guides, aliases, _ = _load_guide_registry()
    normalized = aliases.get(raw, raw)

    if normalized not in guides and not allow_missing:
        supported = ", ".join(sorted(guides.keys()))
        raise ValueError(
            f"E1001: 不支持的 guide={guide}，可选值: {supported}。"
            "如需新增 guide，请在 build_vcs_index 时提供 --guide <id> 与 --pdf-path <file.pdf>。"
        )
    return normalized


def _guide_meta(guide: str) -> dict[str, Any]:
    normalized = _normalize_guide(guide)
    guides, _, _ = _load_guide_registry()
    return guides[normalized]


def _default_pdf_for_guide(guide: str) -> Path:
    normalized = _normalize_guide(guide)
    default_pdf = str(_guide_meta(normalized).get("default_pdf", "")).strip()
    if not default_pdf:
        raise ValueError(
            f"E1001: guide={normalized} 未配置 default_pdf，请在 build_vcs_index 时显式提供 --pdf-path。"
        )
    path = (WORKSPACE_ROOT / default_pdf).resolve()
    _ensure_path_within_workspace(path)
    return path


def _index_path_for_guide(guide: str) -> Path:
    normalized = _normalize_guide(guide)
    path = (INDEXES_DIR / f"{normalized}.json").resolve()
    _ensure_path_within_workspace(path)
    return path


def _legacy_index_path_for_guide(guide: str) -> Path | None:
    normalized = _normalize_guide(guide)
    if normalized != "vcs":
        return None
    _ensure_path_within_workspace(LEGACY_VCS_INDEX_PATH)
    return LEGACY_VCS_INDEX_PATH


def _requests_dir_for_guide(guide: str) -> Path:
    normalized = _normalize_guide(guide)
    path = (REQUESTS_ROOT / normalized).resolve()
    _ensure_path_within_workspace(path)
    return path


def _ensure_storage_dirs(guide: str) -> None:
    _ensure_path_within_workspace(DATA_ROOT)
    _ensure_path_within_workspace(INDEXES_DIR)
    _ensure_path_within_workspace(REQUESTS_ROOT)
    _ensure_path_within_workspace(GUIDES_REGISTRY_PATH)
    requests_dir = _requests_dir_for_guide(guide)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    INDEXES_DIR.mkdir(parents=True, exist_ok=True)
    REQUESTS_ROOT.mkdir(parents=True, exist_ok=True)
    requests_dir.mkdir(parents=True, exist_ok=True)


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in TOKEN_RE.findall(text)]


def _content_tokens(text: str) -> list[str]:
    result: list[str] = []
    for token in _tokenize(text):
        if token in STOPWORDS:
            continue
        if token.isdigit():
            continue
        if len(token) <= 1:
            continue
        result.append(token)
    return result


def _extract_options(text: str) -> list[str]:
    return [opt.lower() for opt in OPTION_RE.findall(text)]


def _truncate(text: str, max_chars: int = 260) -> str:
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 3] + "..."


def _clean_heading(line: str) -> str:
    heading = re.sub(r"\.{3,}\s*\d+\s*$", "", line).strip()
    heading = re.sub(r"\s+", " ", heading)
    return heading


def _is_toc_like(chunk_text: str) -> bool:
    lines = [ln.strip() for ln in chunk_text.splitlines() if ln.strip()]
    if not lines:
        return False

    dot_lines = sum(1 for ln in lines if TOC_DOT_LINE_RE.search(ln))
    short_number_lines = sum(1 for ln in lines if TOC_SHORT_LINE_RE.match(ln))
    contents_hint = any("table of contents" in ln.lower() or ln.lower() == "contents" for ln in lines[:4])

    ratio = (dot_lines + short_number_lines) / max(1, len(lines))
    return contents_hint or dot_lines >= 2 or ratio >= 0.35


def _infer_section_path(chunk_text: str) -> str:
    for raw_line in chunk_text.splitlines()[:12]:
        line = raw_line.strip()
        if not line:
            continue
        if TOC_DOT_LINE_RE.search(line):
            continue

        candidate = _clean_heading(line)
        if not candidate:
            continue

        if HEADING_NUMBER_RE.match(candidate):
            return candidate

        if candidate.lower().startswith("chapter "):
            return candidate

        if 8 <= len(candidate) <= 120 and candidate.isascii() and candidate[0].isupper():
            return candidate

    return "Unknown"


def _extract_pdf_texts(path: Path) -> list[str]:
    if PdfReader is not None:
        reader = PdfReader(str(path))
        return [(page.extract_text() or "") for page in reader.pages]

    pdftotext_bin = shutil.which("pdftotext")
    if not pdftotext_bin:
        raise RuntimeError(
            "E2002: 缺少文本抽取依赖。请安装 pypdf（pip install pypdf）"
            "或安装 pdftotext（poppler-utils）。"
        )

    cmd = [pdftotext_bin, "-layout", str(path), "-"]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    raw = result.stdout

    pages = [part.strip() for part in raw.split("\f")]
    if pages and pages[-1] == "":
        pages.pop()
    return pages


def _split_text_to_chunks(page_text: str, chunk_chars: int = 1200, overlap: int = 120) -> list[str]:
    text = page_text.replace("\u00a0", " ").strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= chunk_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(paragraph) <= chunk_chars:
            current = paragraph
            continue

        step = max(1, chunk_chars - overlap)
        for idx in range(0, len(paragraph), step):
            chunks.append(paragraph[idx : idx + chunk_chars])

    if current:
        chunks.append(current)

    return chunks


def _load_index(guide: str) -> dict[str, Any]:
    normalized = _normalize_guide(guide)
    index_path = _index_path_for_guide(normalized)
    if index_path.exists():
        return json.loads(index_path.read_text(encoding="utf-8"))

    legacy_path = _legacy_index_path_for_guide(normalized)
    if legacy_path and legacy_path.exists():
        return json.loads(legacy_path.read_text(encoding="utf-8"))

    raise ValueError(f"E2001: 索引不存在，请先调用 build_vcs_index（guide={normalized}）")


def _save_request_payload(guide: str, request_id: str, payload: dict[str, Any]) -> None:
    normalized = _normalize_guide(guide)
    _ensure_storage_dirs(normalized)
    requests_dir = _requests_dir_for_guide(normalized)
    path = (requests_dir / f"{request_id}.json").resolve()
    _ensure_path_within_workspace(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _compute_index_statistics(chunks: list[dict[str, Any]]) -> tuple[dict[str, float], float, float]:
    total_chunks = max(1, len(chunks))
    doc_freq: Counter[str] = Counter()
    total_len = 0

    for chunk in chunks:
        token_freq = chunk.get("token_freq", {})
        if token_freq:
            doc_freq.update(token_freq.keys())
        total_len += int(chunk.get("chunk_len", 0))

    idf: dict[str, float] = {}
    for token, df in doc_freq.items():
        idf[token] = math.log((1 + total_chunks) / (1 + df)) + 1.0

    avg_chunk_len = (total_len / total_chunks) if total_chunks else 1.0
    default_idf = math.log((1 + total_chunks) / 1) + 1.0
    return idf, max(1.0, avg_chunk_len), default_idf


def _build_query_context(question: str, index_data: dict[str, Any]) -> dict[str, Any]:
    query_tokens = _content_tokens(question)
    if not query_tokens:
        query_tokens = _tokenize(question)

    query_freq = Counter(query_tokens)
    idf_map = index_data.get("idf", {})
    default_idf = float(index_data.get("default_idf", 1.0))

    query_tfidf: dict[str, float] = {}
    for token, tf in query_freq.items():
        query_tfidf[token] = float(tf) * float(idf_map.get(token, default_idf))

    qnorm = math.sqrt(sum(v * v for v in query_tfidf.values()))

    return {
        "question": question,
        "question_lower": question.lower(),
        "query_tokens": list(query_freq.keys()),
        "query_freq": query_freq,
        "query_options": _extract_options(question),
        "query_tfidf": query_tfidf,
        "query_norm": qnorm,
    }


def _score_chunk(
    query_ctx: dict[str, Any],
    chunk: dict[str, Any],
    index_data: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    token_freq: dict[str, int] = chunk.get("token_freq", {})
    if not token_freq:
        token_freq = Counter(_content_tokens(chunk.get("text", "")))

    if not token_freq:
        return 0.0, {}

    query_tokens: list[str] = query_ctx["query_tokens"]
    overlap_tokens = [token for token in query_tokens if token in token_freq]
    if not overlap_tokens:
        return 0.0, {}

    idf_map: dict[str, float] = index_data.get("idf", {})
    default_idf = float(index_data.get("default_idf", 1.0))
    avg_chunk_len = float(index_data.get("avg_chunk_len", 120.0))

    chunk_len = int(chunk.get("chunk_len", 0)) or sum(token_freq.values())
    k1, b = 1.2, 0.75

    bm25 = 0.0
    for token in overlap_tokens:
        tf = float(token_freq.get(token, 0))
        idf = float(idf_map.get(token, default_idf))
        denom = tf + k1 * (1 - b + b * (chunk_len / max(1.0, avg_chunk_len)))
        if denom <= 0:
            continue
        bm25 += idf * ((tf * (k1 + 1)) / denom)

    bm25_norm = bm25 / (bm25 + 3.0) if bm25 > 0 else 0.0

    query_tfidf: dict[str, float] = query_ctx["query_tfidf"]
    query_norm = float(query_ctx["query_norm"])

    dot = 0.0
    for token in overlap_tokens:
        idf = float(idf_map.get(token, default_idf))
        dot += query_tfidf.get(token, 0.0) * (float(token_freq.get(token, 0)) * idf)

    chunk_norm = float(chunk.get("tfidf_norm", 0.0))
    if chunk_norm <= 0:
        chunk_norm = math.sqrt(
            sum((float(tf) * float(idf_map.get(tok, default_idf))) ** 2 for tok, tf in token_freq.items())
        )

    cosine = dot / (query_norm * chunk_norm) if query_norm > 0 and chunk_norm > 0 else 0.0

    text_lower = str(chunk.get("text", "")).lower()
    chunk_options = set(chunk.get("options", []))
    option_hits = [opt for opt in query_ctx["query_options"] if opt in chunk_options or opt in text_lower]
    option_boost = min(0.7, 0.25 * len(option_hits))

    phrase_boost = 0.0
    question_lower = str(query_ctx.get("question_lower", ""))
    if len(question_lower) >= 8 and question_lower in text_lower:
        phrase_boost = 0.15

    section_path = str(chunk.get("section_path", "Unknown"))
    heading_boost = 0.08 if section_path != "Unknown" else 0.0

    toc_penalty = -0.45 if bool(chunk.get("is_toc_like", False)) else 0.0

    score = (bm25_norm * 0.55) + (cosine * 0.45) + option_boost + phrase_boost + heading_boost + toc_penalty

    details = {
        "bm25": round(bm25_norm, 4),
        "cosine": round(cosine, 4),
        "option_boost": round(option_boost, 4),
        "phrase_boost": round(phrase_boost, 4),
        "heading_boost": round(heading_boost, 4),
        "toc_penalty": round(toc_penalty, 4),
        "overlap_terms": overlap_tokens[:10],
    }

    return max(0.0, score), details


def _retrieve(index_data: dict[str, Any], question: str, top_k: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query_ctx = _build_query_context(question=question, index_data=index_data)

    ranked: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    toc_candidates = 0

    for chunk in index_data.get("chunks", []):
        score, details = _score_chunk(query_ctx=query_ctx, chunk=chunk, index_data=index_data)
        if score <= 0:
            continue
        if bool(chunk.get("is_toc_like", False)):
            toc_candidates += 1
        ranked.append((score, chunk, details))

    ranked.sort(key=lambda item: item[0], reverse=True)

    non_toc_ranked = [item for item in ranked if not bool(item[1].get("is_toc_like", False))]

    selected: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    selected_ids: set[str] = set()

    for item in non_toc_ranked:
        if len(selected) >= max(1, top_k):
            break
        selected.append(item)
        selected_ids.add(item[1]["chunk_id"])

    if len(selected) < max(1, top_k):
        for item in ranked:
            if len(selected) >= max(1, top_k):
                break
            cid = item[1]["chunk_id"]
            if cid in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(cid)

    results: list[dict[str, Any]] = []
    for score, chunk, details in selected:
        results.append(
            {
                "score": round(score, 4),
                "chunk_id": chunk["chunk_id"],
                "page": chunk["page"],
                "section_path": chunk.get("section_path", "Unknown"),
                "snippet": _truncate(chunk.get("text", ""), 320),
                "is_toc_like": bool(chunk.get("is_toc_like", False)),
                "score_breakdown": details,
            }
        )

    retrieval_debug = {
        "retrieval_version": index_data.get("retrieval_version", "v1_lexical"),
        "query_tokens": query_ctx["query_tokens"][:20],
        "query_options": query_ctx["query_options"],
        "candidate_count": len(ranked),
        "toc_candidates": toc_candidates,
        "non_toc_candidates": len(non_toc_ranked),
        "top_score": results[0]["score"] if results else 0.0,
        "toc_like_in_top_k": sum(1 for item in results if item["is_toc_like"]),
    }

    return results, retrieval_debug


def _compose_answer(question: str, evidence: list[dict[str, Any]], guide: str) -> tuple[str, float, list[str]]:
    _ = question
    normalized = _normalize_guide(guide)
    guide_meta = _guide_meta(normalized)

    if not evidence:
        return (
            "当前索引中没有检索到足够证据来回答该问题。请补充更具体的命令、选项或报错文本。",
            0.0,
            ["未命中足够证据，已触发保守回答策略"],
        )

    lines = [f"基于 {guide_meta['label']} 检索到的相关内容，建议先关注以下证据："]
    for idx, item in enumerate(evidence[:3], start=1):
        lines.append(
            f"- 证据{idx}（第 {item['page']} 页，{item['section_path']}）：{item['snippet']}"
        )

    lines.append("如果你愿意，我可以继续基于这些证据整理成“可直接执行”的命令步骤。")

    confidence = min(0.95, max(0.35, evidence[0]["score"] / 1.8 + 0.30))
    limitations = [
        "Demo V2 使用本地 Hybrid 检索（BM25-like + TF-IDF cosine）",
        guide_meta["version_hint"],
    ]

    if evidence[0].get("is_toc_like"):
        limitations.append("Top1 证据疑似目录页，建议继续追问以获取正文片段")

    return ("\n".join(lines), round(confidence, 3), limitations)


@mcp.tool
def build_vcs_index(
    pdf_path: str | None = None,
    force_rebuild: bool = False,
    max_pages: int = 0,
    guide: str = "vcs",
) -> dict[str, Any]:
    """构建 User Guide 索引（Demo V2）。"""
    normalized = _normalize_guide(guide, allow_missing=True)

    if normalized not in _load_guide_registry()[0]:
        if not pdf_path:
            raise ValueError(
                f"E1001: guide={normalized} 尚未注册，首次构建请提供 --pdf-path。"
                "示例：--build-index --guide <new_guide_id> --pdf-path ./<file>.pdf"
            )

        seed_path = Path(pdf_path).expanduser().resolve()
        _ensure_path_within_workspace(seed_path)
        if seed_path.suffix.lower() != ".pdf":
            raise ValueError("E1001: 输入文件必须是 .pdf")

        _register_guide(
            normalized,
            {
                "label": " ".join(part for part in re.split(r"[_-]+", normalized) if part).title() or normalized,
                "default_pdf": seed_path.name,
                "version_hint": "请确认你的文档版本与当前索引一致",
                "aliases": [normalized],
            },
        )

    guide_meta = _guide_meta(normalized)
    _ensure_storage_dirs(normalized)

    path = Path(pdf_path).expanduser().resolve() if pdf_path else _default_pdf_for_guide(normalized)
    _ensure_path_within_workspace(path)

    if not path.exists():
        raise ValueError(f"E1001: PDF 文件不存在 {path}")

    if path.suffix.lower() != ".pdf":
        raise ValueError("E1001: 输入文件必须是 .pdf")

    index_path = _index_path_for_guide(normalized)
    legacy_path = _legacy_index_path_for_guide(normalized)
    has_cached = index_path.exists() or (legacy_path.exists() if legacy_path else False)

    if has_cached and not force_rebuild:
        cached = _load_index(normalized)
        if (
            cached.get("pdf_path") == str(path)
            and float(cached.get("pdf_mtime", 0.0)) == path.stat().st_mtime
            and cached.get("retrieval_version") == "v2_hybrid_bm25_tfidf"
        ):
            return {
                "ok": True,
                "reused": True,
                "guide": normalized,
                "guide_label": guide_meta["label"],
                "index_id": cached.get("index_id"),
                "pdf_path": cached.get("pdf_path"),
                "page_count": cached.get("page_count", 0),
                "chunk_count": cached.get("chunk_count", 0),
                "toc_like_chunks": cached.get("toc_like_chunks", 0),
                "retrieval_version": cached.get("retrieval_version"),
                "created_at": cached.get("created_at"),
            }

    page_texts = _extract_pdf_texts(path)
    page_count = len(page_texts)
    page_limit = page_count if max_pages <= 0 else min(max_pages, page_count)

    chunks: list[dict[str, Any]] = []
    failed_pages: list[int] = []

    for page_idx in range(page_limit):
        page_no = page_idx + 1
        try:
            page_text = page_texts[page_idx] or ""
        except Exception:
            failed_pages.append(page_no)
            continue

        page_chunks = _split_text_to_chunks(page_text)
        if not page_chunks:
            continue

        for local_idx, chunk_text in enumerate(page_chunks, start=1):
            token_list = _content_tokens(chunk_text)
            token_freq = dict(Counter(token_list))
            options = sorted(set(_extract_options(chunk_text)))
            is_toc_like = _is_toc_like(chunk_text)

            chunks.append(
                {
                    "chunk_id": f"p{page_no:04d}_c{local_idx:03d}",
                    "page": page_no,
                    "section_path": _infer_section_path(chunk_text),
                    "text": chunk_text,
                    "token_freq": token_freq,
                    "chunk_len": sum(token_freq.values()),
                    "options": options,
                    "is_toc_like": is_toc_like,
                }
            )

    idf_map, avg_chunk_len, default_idf = _compute_index_statistics(chunks)

    for chunk in chunks:
        token_freq = chunk.get("token_freq", {})
        norm = math.sqrt(sum((float(tf) * float(idf_map.get(tok, default_idf))) ** 2 for tok, tf in token_freq.items()))
        chunk["tfidf_norm"] = round(norm, 6)
        chunk["tokens"] = sorted(token_freq.keys())

    toc_like_chunks = sum(1 for chunk in chunks if bool(chunk.get("is_toc_like", False)))

    index_data = {
        "guide": normalized,
        "guide_label": guide_meta["label"],
        "index_id": f"{normalized}_guide_demo_{uuid.uuid4().hex[:8]}",
        "retrieval_version": "v2_hybrid_bm25_tfidf",
        "pdf_path": str(path),
        "pdf_mtime": path.stat().st_mtime,
        "created_at": _utc_now(),
        "page_count": page_limit,
        "chunk_count": len(chunks),
        "toc_like_chunks": toc_like_chunks,
        "failed_pages": failed_pages,
        "avg_chunk_len": avg_chunk_len,
        "default_idf": default_idf,
        "idf": idf_map,
        "chunks": chunks,
    }

    index_path.write_text(json.dumps(index_data, ensure_ascii=False), encoding="utf-8")

    return {
        "ok": True,
        "reused": False,
        "guide": normalized,
        "guide_label": guide_meta["label"],
        "index_id": index_data["index_id"],
        "pdf_path": index_data["pdf_path"],
        "page_count": index_data["page_count"],
        "chunk_count": index_data["chunk_count"],
        "toc_like_chunks": index_data["toc_like_chunks"],
        "failed_pages": index_data["failed_pages"],
        "retrieval_version": index_data["retrieval_version"],
        "created_at": index_data["created_at"],
    }


@mcp.tool
def ask_vcs_guide(
    question: str,
    language: str = "zh-CN",
    top_k: int = 6,
    use_codex_review: bool = False,
    guide: str = "vcs",
) -> dict[str, Any]:
    """问答入口：返回答案、证据引用、置信度与限制信息。"""
    if not question.strip():
        raise ValueError("E1001: question 不能为空")

    normalized = _normalize_guide(guide)
    guide_meta = _guide_meta(normalized)
    index_data = _load_index(normalized)
    evidence, retrieval_debug = _retrieve(index_data=index_data, question=question, top_k=max(1, top_k))
    answer, confidence, limitations = _compose_answer(question=question, evidence=evidence, guide=normalized)

    if use_codex_review:
        limitations.append("Demo 未内置 GPT-5.3 Codex 调用，仅保留接口参数")

    request_id = f"req_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    response = {
        "ok": True,
        "guide": normalized,
        "guide_label": guide_meta["label"],
        "request_id": request_id,
        "language": language,
        "question": question,
        "answer": answer,
        "citations": [
            {
                "chunk_id": item["chunk_id"],
                "page": item["page"],
                "section_path": item["section_path"],
                "snippet": item["snippet"],
                "score": item["score"],
                "is_toc_like": item["is_toc_like"],
            }
            for item in evidence
        ],
        "confidence": confidence,
        "limitations": limitations,
        "index_id": index_data.get("index_id"),
        "retrieval_debug": retrieval_debug,
        "created_at": _utc_now(),
    }

    _save_request_payload(guide=normalized, request_id=request_id, payload=response)
    return response


@mcp.tool
def get_vcs_evidence(request_id: str, limit: int = 10, guide: str = "vcs") -> dict[str, Any]:
    """按 request_id 拉取证据与回答快照。"""
    if not request_id.strip():
        raise ValueError("E1001: request_id 不能为空")

    normalized = _normalize_guide(guide)
    _ensure_storage_dirs(normalized)
    requests_dir = _requests_dir_for_guide(normalized)
    path = (requests_dir / f"{request_id}.json").resolve()
    _ensure_path_within_workspace(path)

    if not path.exists():
        raise ValueError(f"E1001: 未找到 request_id={request_id} 的证据记录（guide={normalized}）")

    payload = json.loads(path.read_text(encoding="utf-8"))
    citations = payload.get("citations", [])

    return {
        "ok": True,
        "guide": normalized,
        "request_id": request_id,
        "question": payload.get("question", ""),
        "answer": payload.get("answer", ""),
        "citations": citations[: max(1, limit)],
        "confidence": payload.get("confidence", 0.0),
        "retrieval_debug": payload.get("retrieval_debug", {}),
        "created_at": payload.get("created_at"),
    }


@mcp.tool
def health_check() -> dict[str, Any]:
    """返回服务与索引健康状态。"""
    _ensure_storage_dirs("vcs")
    guides_registry, _, registry_warnings = _load_guide_registry()

    status = {
        "ok": True,
        "service": "vcs-guide-mcp-demo",
        "workspace": str(WORKSPACE_ROOT),
        "fastmcp_installed": FastMCP is not None,
        "pypdf_installed": PdfReader is not None,
        "pdftotext_available": shutil.which("pdftotext") is not None,
        "index_exists": _index_path_for_guide("vcs").exists() or LEGACY_VCS_INDEX_PATH.exists(),
        "requests_dir": str(_requests_dir_for_guide("vcs")),
        "indexes_dir": str(INDEXES_DIR),
        "checked_at": _utc_now(),
    }

    if registry_warnings:
        status["registry_warnings"] = registry_warnings

    if status["index_exists"]:
        data = _load_index("vcs")
        status["index"] = {
            "guide": "vcs",
            "guide_label": _guide_meta("vcs")["label"],
            "index_id": data.get("index_id"),
            "retrieval_version": data.get("retrieval_version", "v1_lexical"),
            "pdf_path": data.get("pdf_path"),
            "page_count": data.get("page_count"),
            "chunk_count": data.get("chunk_count"),
            "toc_like_chunks": data.get("toc_like_chunks", 0),
            "created_at": data.get("created_at"),
        }

    guides: dict[str, Any] = {}
    for guide, meta in guides_registry.items():
        index_path = _index_path_for_guide(guide)
        fallback_path = _legacy_index_path_for_guide(guide)
        using_legacy = bool(fallback_path and not index_path.exists() and fallback_path.exists())
        exists = index_path.exists() or bool(fallback_path and fallback_path.exists())

        guide_status: dict[str, Any] = {
            "guide": guide,
            "guide_label": meta["label"],
            "index_exists": exists,
            "index_path": str(fallback_path if using_legacy and fallback_path else index_path),
            "requests_dir": str(_requests_dir_for_guide(guide)),
        }
        if exists:
            data = _load_index(guide)
            guide_status["index"] = {
                "index_id": data.get("index_id"),
                "retrieval_version": data.get("retrieval_version", "v1_lexical"),
                "pdf_path": data.get("pdf_path"),
                "page_count": data.get("page_count"),
                "chunk_count": data.get("chunk_count"),
                "toc_like_chunks": data.get("toc_like_chunks", 0),
                "created_at": data.get("created_at"),
            }
        guides[guide] = guide_status

    status["guides"] = guides
    return status


def _run_cli_demo(args: argparse.Namespace) -> int:
    if args.build_index:
        result = build_vcs_index(
            pdf_path=args.pdf_path,
            force_rebuild=args.force_rebuild,
            max_pages=args.max_pages,
            guide=args.guide,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.ask:
        result = ask_vcs_guide(
            question=args.ask,
            top_k=args.top_k,
            language=args.language,
            use_codex_review=args.use_codex_review,
            guide=args.guide,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.get_evidence:
        result = get_vcs_evidence(request_id=args.get_evidence, limit=args.limit, guide=args.guide)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.health:
        result = health_check()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="VCS/VC Formal User Guide MCP Demo Server")
    parser.add_argument("--build-index", action="store_true", help="Build local PDF index")
    parser.add_argument("--guide", type=str, default="vcs", help="Guide key, defaults to vcs")
    parser.add_argument("--pdf-path", type=str, default=None, help="PDF path, defaults by selected guide")
    parser.add_argument("--force-rebuild", action="store_true", help="Force rebuild even if cached")
    parser.add_argument("--max-pages", type=int, default=0, help="Limit indexing pages for quick demo")
    parser.add_argument("--ask", type=str, default="", help="Ask a natural language question")
    parser.add_argument("--top-k", type=int, default=6, help="Top-K evidence chunks")
    parser.add_argument("--language", type=str, default="zh-CN", help="Response language label")
    parser.add_argument("--use-codex-review", action="store_true", help="Reserved flag for optional codex review")
    parser.add_argument("--get-evidence", type=str, default="", help="Fetch saved evidence by request_id")
    parser.add_argument("--limit", type=int, default=10, help="Citation limit for --get-evidence")
    parser.add_argument("--health", action="store_true", help="Show health status")

    args = parser.parse_args()

    has_cli_action = any([args.build_index, bool(args.ask), bool(args.get_evidence), args.health])
    if has_cli_action:
        sys.exit(_run_cli_demo(args))

    mcp.run()


if __name__ == "__main__":
    main()
