const el = {
  question: document.getElementById("question"),
  guide: document.getElementById("guide"),
  topk: document.getElementById("topk"),
  maxPages: document.getElementById("maxPages"),
  askBtn: document.getElementById("askBtn"),
  status: document.getElementById("status"),
  error: document.getElementById("error"),
  answerFinal: document.getElementById("answerFinal"),
  answerRetrieval: document.getElementById("answerRetrieval"),
  meta: document.getElementById("meta"),
  ranges: document.getElementById("ranges"),
  citations: document.getElementById("citations"),
};

function setBusy(busy) {
  el.askBtn.disabled = busy;
  el.status.textContent = busy ? "处理中，请稍候..." : "就绪。";
}

function clearOutput() {
  el.error.textContent = "";
  el.answerFinal.textContent = "";
  el.answerRetrieval.textContent = "";
  el.meta.textContent = "";
  el.ranges.innerHTML = "";
  el.citations.innerHTML = "";
}

function render(result) {
  el.answerFinal.textContent = result.answer_final || "";
  el.answerRetrieval.textContent = result.answer_retrieval || "";

  const ranges = (result.page_selection && result.page_selection.ranges) || [];
  const requestId = result.request_id || "";
  const guide = result.guide || "";
  const llmUsed = result.llm && result.llm.used ? "yes" : "no";
  el.meta.textContent = `request_id: ${requestId}\nguide: ${guide}\nllm_used: ${llmUsed}`;

  for (const r of ranges) {
    const span = document.createElement("span");
    span.className = "chip";
    span.textContent = r;
    el.ranges.appendChild(span);
  }

  const citations = result.citations || [];
  for (const c of citations) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${c.page ?? ""}</td><td>${c.section_path ?? ""}</td><td>${c.score ?? ""}</td><td>${c.snippet ?? ""}</td>`;
    el.citations.appendChild(tr);
  }
}

async function ask() {
  clearOutput();
  const question = (el.question.value || "").trim();
  if (!question) {
    el.error.textContent = "请输入问题";
    return;
  }

  const payload = {
    question,
    guide: el.guide.value,
    top_k: Number(el.topk.value || 6),
    max_pages_for_llm: Number(el.maxPages.value || 8),
    language: "zh-CN",
  };

  setBusy(true);
  try {
    const resp = await fetch("/api/qa", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }
    render(data);
  } catch (err) {
    el.error.textContent = err.message || String(err);
  } finally {
    setBusy(false);
  }
}

document.getElementById("askBtn").addEventListener("click", ask);
