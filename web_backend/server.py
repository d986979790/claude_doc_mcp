from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    anthropic = None  # type: ignore[assignment]

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

import vcs_mcp_demo_server as mcp_server
WEBUI_ROOT = (WORKSPACE_ROOT / "webui").resolve()
MAX_QUESTION_LEN = 4000
MAX_TOP_K = 12
MAX_PAGES_FOR_LLM = 12
RATE_LIMIT_PER_MIN = 30

RATE_LIMIT_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
RATE_LIMIT_LOCK = threading.Lock()


@dataclass
class QARequest:
    question: str
    guide: str = "vcs"
    language: str = "zh-CN"
    top_k: int = 6
    use_codex_review: bool = False
    max_pages_for_llm: int = 8


class ValidationError(Exception):
    pass


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    if content_length <= 0:
        raise ValidationError("E1001: 请求体不能为空")
    body = handler.rfile.read(content_length)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ValidationError(f"E1001: JSON 解析失败: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValidationError("E1001: 请求体必须是 JSON Object")
    return parsed


def _require_rate_limit(handler: BaseHTTPRequestHandler) -> None:
    ip = handler.client_address[0] if handler.client_address else "unknown"
    now = time.time()
    with RATE_LIMIT_LOCK:
        bucket = RATE_LIMIT_BUCKETS[ip]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_PER_MIN:
            raise ValidationError("E4290: 请求过于频繁，请稍后重试")
        bucket.append(now)


def _validate_qa_request(payload: dict[str, Any]) -> QARequest:
    question = str(payload.get("question", "")).strip()
    if not question:
        raise ValidationError("E1001: question 不能为空")
    if len(question) > MAX_QUESTION_LEN:
        raise ValidationError(f"E1001: question 过长，最大长度 {MAX_QUESTION_LEN}")

    guide = str(payload.get("guide", "vcs")).strip() or "vcs"
    guide = mcp_server._normalize_guide(guide)

    language = str(payload.get("language", "zh-CN")).strip() or "zh-CN"

    try:
        top_k = int(payload.get("top_k", 6))
    except Exception as exc:
        raise ValidationError("E1001: top_k 必须是整数") from exc
    top_k = max(1, min(MAX_TOP_K, top_k))

    use_codex_review = bool(payload.get("use_codex_review", False))

    try:
        max_pages_for_llm = int(payload.get("max_pages_for_llm", 8))
    except Exception as exc:
        raise ValidationError("E1001: max_pages_for_llm 必须是整数") from exc
    max_pages_for_llm = max(1, min(MAX_PAGES_FOR_LLM, max_pages_for_llm))

    return QARequest(
        question=question,
        guide=guide,
        language=language,
        top_k=top_k,
        use_codex_review=use_codex_review,
        max_pages_for_llm=max_pages_for_llm,
    )


def _merge_page_ranges(pages: list[int]) -> list[tuple[int, int]]:
    if not pages:
        return []
    uniq = sorted(set(p for p in pages if p > 0))
    if not uniq:
        return []

    ranges: list[tuple[int, int]] = []
    start = uniq[0]
    prev = uniq[0]

    for page in uniq[1:]:
        if page == prev + 1:
            prev = page
            continue
        ranges.append((start, prev))
        start = page
        prev = page

    ranges.append((start, prev))
    return ranges


def _range_labels(ranges: list[tuple[int, int]]) -> list[str]:
    labels: list[str] = []
    for start, end in ranges:
        labels.append(str(start) if start == end else f"{start}-{end}")
    return labels


def _collect_evidence_pages(citations: list[dict[str, Any]], max_pages: int) -> list[int]:
    pages: list[int] = []
    for item in citations:
        page = item.get("page")
        if isinstance(page, int) and page > 0:
            pages.append(page)
            continue
        if isinstance(page, str) and page.isdigit():
            pages.append(int(page))

    if len(pages) < max_pages:
        range_pattern = re.compile(r"\b(\d{1,4})\s*[-–]\s*(\d{1,4})\b")
        text_fields = [
            str(item.get("snippet", "")) for item in citations
        ] + [
            str(item.get("section_path", "")) for item in citations
        ]

        for field in text_fields:
            for left, right in range_pattern.findall(field):
                start = int(left)
                end = int(right)
                if start <= 0 or end <= 0:
                    continue
                if start > end:
                    start, end = end, start
                span = end - start + 1
                if span > 30:
                    continue
                for p in range(start, end + 1):
                    pages.append(p)

    uniq = sorted(set(pages))
    return uniq[:max_pages]


def _extract_pdf_pages(guide: str, pages: list[int]) -> list[dict[str, Any]]:
    if not pages:
        return []

    pdf_path = mcp_server._default_pdf_for_guide(guide)
    mcp_server._ensure_path_within_workspace(pdf_path)

    page_texts: list[dict[str, Any]] = []

    if mcp_server.PdfReader is not None:
        reader = mcp_server.PdfReader(str(pdf_path))
        total = len(reader.pages)
        for p in pages:
            if p < 1 or p > total:
                continue
            txt = reader.pages[p - 1].extract_text() or ""
            if txt.strip():
                page_texts.append({"page": p, "text": txt.strip()})
        return page_texts

    all_pages = mcp_server._extract_pdf_texts(pdf_path)
    total = len(all_pages)
    for p in pages:
        if p < 1 or p > total:
            continue
        txt = (all_pages[p - 1] or "").strip()
        if txt:
            page_texts.append({"page": p, "text": txt})
    return page_texts


def _build_llm_messages(req: QARequest, retrieval: dict[str, Any], page_texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citation_lines = []
    for c in retrieval.get("citations", [])[:8]:
        citation_lines.append(
            f"- page={c.get('page')} section={c.get('section_path')} score={c.get('score')} snippet={c.get('snippet')}"
        )

    page_blocks = []
    for item in page_texts:
        page_blocks.append(f"### PAGE {item['page']}\n{item['text']}")

    citations_text = "\n".join(citation_lines)
    page_text_block = "\n\n".join(page_blocks)

    user_prompt = (
        f"问题：{req.question}\n\n"
        "这是检索系统给出的候选答案（可能含目录噪声）：\n"
        f"{retrieval.get('answer', '')}\n\n"
        "这是检索证据摘要：\n"
        f"{citations_text}\n\n"
        "这是从 PDF 抽取的目标页正文，请优先基于这些正文回答：\n"
        f"{page_text_block}\n\n"
        "请输出：\n"
        "1) 直接回答问题（中文）\n"
        "2) 关键依据（列出页码）\n"
        "3) 如果证据不足，明确说明限制\n"
    )

    return [{"role": "user", "content": user_prompt}]


def _extract_text_from_message(message: Any) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []):
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(part for part in parts if part).strip()


def _call_claude_final_answer(req: QARequest, retrieval: dict[str, Any], page_texts: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    if anthropic is None:
        raise RuntimeError("anthropic SDK 未安装，请先执行: .venv/bin/pip install anthropic")

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 ANTHROPIC_API_KEY")

    client = anthropic.Anthropic(api_key=api_key)
    messages = _build_llm_messages(req=req, retrieval=retrieval, page_texts=page_texts)

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        messages=messages,
    ) as stream:
        final_message = stream.get_final_message()

    final_text = _extract_text_from_message(final_message)
    if not final_text:
        raise RuntimeError("Claude API 返回为空")

    usage = getattr(final_message, "usage", None)
    usage_payload = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }

    return final_text, usage_payload


def _run_with_timeout(fn: Any, timeout_sec: int, *args: Any, **kwargs: Any) -> Any:
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError as exc:
            raise TimeoutError(f"操作超时（>{timeout_sec}s）") from exc


def _orchestrate_qa(req: QARequest) -> dict[str, Any]:
    retrieval = _run_with_timeout(
        mcp_server.ask_vcs_guide,
        45,
        question=req.question,
        language=req.language,
        top_k=req.top_k,
        use_codex_review=req.use_codex_review,
        guide=req.guide,
    )

    citations = retrieval.get("citations", [])
    selected_pages = _collect_evidence_pages(citations=citations, max_pages=req.max_pages_for_llm)
    ranges = _merge_page_ranges(selected_pages)
    labels = _range_labels(ranges)
    page_texts = _run_with_timeout(_extract_pdf_pages, 45, req.guide, selected_pages)

    llm_used = False
    llm_error = ""
    llm_usage: dict[str, Any] = {}
    final_answer = retrieval.get("answer", "")

    try:
        if page_texts:
            final_answer, llm_usage = _run_with_timeout(_call_claude_final_answer, 120, req, retrieval, page_texts)
            llm_used = True
        else:
            llm_error = "未抽取到可用的 PDF 正文页，已回退到检索答案"
    except Exception as exc:
        llm_error = str(exc)

    limitations = list(retrieval.get("limitations", []))
    if llm_error:
        limitations.append(f"LLM 回退：{llm_error}")

    return {
        "ok": True,
        "request_id": retrieval.get("request_id"),
        "guide": retrieval.get("guide", req.guide),
        "question": req.question,
        "answer_retrieval": retrieval.get("answer", ""),
        "answer_final": final_answer,
        "confidence": retrieval.get("confidence", 0.0),
        "citations": citations,
        "page_selection": {
            "pages": selected_pages,
            "ranges": labels,
            "extracted_pages": [p["page"] for p in page_texts],
        },
        "llm": {
            "used": llm_used,
            "model": "claude-opus-4-6" if llm_used else None,
            "usage": llm_usage,
            "error": llm_error or None,
        },
        "retrieval_debug": retrieval.get("retrieval_debug", {}),
        "limitations": limitations,
    }


def _serve_static(handler: BaseHTTPRequestHandler, request_path: str) -> bool:
    if request_path.startswith("/api/"):
        return False

    if request_path == "/":
        file_path = (WEBUI_ROOT / "index.html").resolve()
    else:
        file_path = (WEBUI_ROOT / request_path.lstrip("/")).resolve()

    if WEBUI_ROOT not in file_path.parents and file_path != WEBUI_ROOT:
        _json_response(handler, HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
        return True

    if not file_path.exists() or not file_path.is_file():
        _json_response(handler, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        return True

    raw = file_path.read_bytes()
    ctype, _ = mimetypes.guess_type(str(file_path))
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)
    return True


class AppHandler(BaseHTTPRequestHandler):
    server_version = "VCSWebGUI/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            health = mcp_server.health_check()
            health["web"] = {
                "webui_root": str(WEBUI_ROOT),
                "anthropic_sdk_installed": anthropic is not None,
                "anthropic_api_key_configured": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            }
            _json_response(self, HTTPStatus.OK, health)
            return

        if parsed.path.startswith("/api/requests/"):
            request_id = parsed.path.rsplit("/", 1)[-1].strip()
            guide = parse_qs(parsed.query).get("guide", ["vcs"])[0]
            try:
                guide = mcp_server._normalize_guide(guide)
                payload = mcp_server.get_vcs_evidence(request_id=request_id, guide=guide, limit=20)
                _json_response(self, HTTPStatus.OK, payload)
            except Exception as exc:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return

        if _serve_static(self, parsed.path):
            return

        _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/qa":
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return

        try:
            _require_rate_limit(self)
            payload = _read_json_body(self)
            req = _validate_qa_request(payload)
            result = _orchestrate_qa(req)
            _json_response(self, HTTPStatus.OK, result)
        except ValidationError as exc:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        except TimeoutError as exc:
            _json_response(self, HTTPStatus.GATEWAY_TIMEOUT, {"ok": False, "error": str(exc)})
        except Exception as exc:
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Web API + GUI for VCS MCP QA demo")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    WEBUI_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"[web] serving on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
