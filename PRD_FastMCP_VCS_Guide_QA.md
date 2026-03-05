# PRD：基于 FastMCP 的 VCS/VC Formal User Guide 智能问答 MCP Server

## 0. 文档信息

| 字段 | 内容 |
|---|---|
| 文档名 | PRD_FastMCP_VCS_Guide_QA |
| 版本 | v1.1 (Draft) |
| 状态 | 待评审（已按 Demo V2 实现对齐） |
| 目标读者 | 产品经理、EDA/CAD 工程师、平台工程师、AI 工程师 |
| 运行环境 | Linux 本地终端 |
| 关键输入文档 | `vcs_user_guide.pdf`（VCS User Guide, Version X-2025.06-SP1, Sep 2025）；`VC_Formal_UserGuide.pdf`（VC Formal User Guide） |

---

## 1. 背景与问题定义

VCS User Guide 体量大、内容密集、术语多、选项复杂（如 `vlogan/vhdlan/vcs`、编译/仿真流程、race/debug/perf 等）。用户在实际使用中常遇到：

1. **定位慢**：需要在长文档中反复检索章节和选项。
2. **理解难**：英文手册 + 命令参数细节多，容易误解。
3. **上下文缺失**：用户问题通常是“场景化问题”，而不是单条命令查询。
4. **一致性差**：不同同事给出的经验答案口径不一，缺少可追溯证据。

目标是建设一个本地可用的 MCP Server，让 Claude Code 作为 MCP Client，通过自然语言提问即可得到基于手册证据的回答。

---

## 2. 产品目标与非目标

### 2.1 产品目标（Goals）

- G1：支持用户用中文自然语言提问 VCS / VC Formal 用法问题。
- G2：回答必须“**有证据可追溯**”（页码/章节路径/原文片段）。
- G3：优先覆盖高频问题：流程、选项、诊断、性能调优。
- G4：支持 Claude Code 主流程，并可选接入 GPT-5.3 Codex 做二次审校。

### 2.2 非目标（Non-Goals）

- NG1：V1.1 不做跨来源联邦检索（如联网/内部知识库）；仅本地已授权 PDF。
- NG2：V1 不做自动执行 shell 修复或仿真脚本改写。
- NG3：V1 不做企业级权限系统（仅本地最小可用控制）。
- NG4：V1 不替代官方文档，仅做“检索+解释+引用”。

---

## 3. 用户角色与典型场景

### 3.1 用户角色

- **DV 工程师**：查选项、流程、debug/性能建议。
- **验证新人**：快速理解 two-step / three-step flow、常见命令。
- **CAD/方法学支持**：定位报错相关章节与官方建议。

### 3.2 典型用户故事（User Stories）

- US-01：作为 DV 工程师，我问“`-debug_access` 怎么配才能兼顾可见性和性能？”，系统返回建议并给出手册出处。
- US-02：作为新人，我问“VCS two-step 和 three-step 有什么区别？怎么用？”，系统返回流程对比和命令模板。
- US-03：作为 CAD 支持，我问“race detection 报告怎么看？”，系统返回解释步骤与引用章节。

---

## 4. 产品范围（Scope）

### 4.1 In Scope（V1.1 / Demo V2）

1. 本地读取并索引多 Guide PDF：`vcs_user_guide.pdf`、`VC_Formal_UserGuide.pdf`。
2. 自然语言问答（中文提问，答案中文为主，保留英文命令/参数）。
3. 检索增强回答（RAG）：返回答案 + citation。
4. MCP tool 接口（FastMCP）供 Claude Code 调用，支持 `guide` 路由（`vcs` / `vc_formal`）。
5. 基础可观测性（request_id、命中证据数、retrieval_debug）。

### 4.2 Out of Scope（V1.1 / Demo V2）

1. 多版本手册自动对齐与版本差异融合。
2. 联网搜索、论坛经验合并。
3. 自动执行 EDA 命令。
4. GUI 产品化界面（先 CLI + MCP）。

---

## 5. 端到端业务流程

1. 用户在 Claude Code 中自然语言提问。
2. Claude Code 调用 MCP tool `ask_vcs_guide`。
3. MCP Server 解析问题中的关键词与命令选项（如 `-debug_access`）。
4. 执行混合检索（BM25-like + TF-IDF cosine）并重排。
5. 生成“结论 + 证据引用 + 限制说明”。
6. 若证据不足，返回澄清问题或拒答。
7. Claude Code 将结构化结果组织为最终回复。

---

## 6. 功能需求（FR）

### FR-01 文档索引构建（P0）
- 输入：`pdf_path`。
- 输出：索引状态、分块数量、失败页统计。
- 要求：支持增量重建或强制重建；保留章节层级元数据。

### FR-02 问题理解与检索路由（P0）
- 识别问题意图（option/flow/error/perf/concept）。
- 对命令参数（如 `-debug_access`）做精确 token 匹配增强。

### FR-03 混合检索（P0）
- 关键词检索（BM25-like/词法）+ TF-IDF cosine 融合（Demo V2 当前实现）。
- 支持 top-k 与重排，输出证据候选列表。
- 对命令选项（例如 `-debug_access`）提供额外加权，降低参数类问题漏召回。

### FR-04 回答生成与引用（P0）
- 输出必须包含：
  - 回答内容
  - 引用证据列表（页码、章节路径、原文片段）
  - 置信度/限制说明
- 无证据时不得“编造式回答”。

### FR-05 证据追溯查询（P1）
- 支持按 `request_id` 获取完整证据包，便于审阅和复核。

### FR-06 可选双模型协同（P1）
- 默认：Claude Code + MCP Server。
- 可选：对高复杂问题启用 GPT-5.3 Codex 二次审校（差异检查/表述优化），不改变“证据优先”原则。
- 说明：Demo V2 当前仅保留 `use_codex_review` 接口参数，尚未在服务内置真实二次审校调用链路。

### FR-07 健康检查与运维接口（P1）
- 提供 `health_check`（索引状态、模型可用性、版本信息）。

---

## 7. 非功能需求（NFR）

- **准确性**：回答需与证据一致，不可与引用冲突。
- **可追溯性**：100% 响应包含 citation 或明确“无证据”。
- **性能**：本地查询保持可交互延迟（P95 可用级别）。
- **可靠性**：索引损坏可恢复，服务异常可重启。
- **可观测性**：请求日志、检索耗时、命中率、拒答率可统计。
- **可维护性**：模块化（解析/索引/检索/生成/接口）。
- **安全性**：路径白名单、最小权限、日志脱敏。

---

## 8. 技术架构（推荐）

### 8.1 组件划分

1. **MCP 接入层（FastMCP Server）**
   - 暴露工具接口，接收 Claude Code 调用。
2. **文档处理层**
   - PDF 解析、结构化清洗、分块（保留章节层级）。
3. **索引层**
   - 词法/统计索引（BM25-like + TF-IDF）+ 元数据存储。
4. **检索与重排层**
   - 混合检索、参数精确匹配增强、结果重排。
5. **回答生成层**
   - 依据证据生成回答，附带 citation 与置信度。
6. **可选审校层（GPT-5.3 Codex）**
   - 对复杂问题进行二次表达审校（可开关）。
7. **观测与审计层**
   - 请求追踪、性能指标、错误码统计。

### 8.2 关键设计决策

- D1：采用“分层分块”（章/节/小节）而非固定长度切块。
- D2：命令行参数类问题优先词法精确匹配，再由 TF-IDF cosine 补充排序。
- D3：回答必须证据绑定（answer span -> evidence id）。
- D4：证据不足时优先澄清/拒答，不给无依据建议。

---

## 9. MCP Tool 接口草案（PRD 级）

### Tool-01 `build_vcs_index`
**用途**：构建或重建文档索引。

**输入示例**：
```json
{
  "guide": "vcs",
  "pdf_path": "/home/yimings/claude_vcs_doc_test1/vcs_user_guide.pdf",
  "force_rebuild": false,
  "max_pages": 0
}
```

**输出示例**：
```json
{
  "ok": true,
  "guide": "vcs",
  "guide_label": "VCS User Guide",
  "index_id": "vcs_guide_x2025_06_sp1",
  "chunk_count": 12840,
  "failed_pages": []
}
```

### Tool-02 `ask_vcs_guide`
**用途**：回答用户问题并返回证据。

**输入示例**：
```json
{
  "guide": "vc_formal",
  "question": "Connectivity Checking 的自动黑盒功能作用是什么？",
  "language": "zh-CN",
  "top_k": 8,
  "use_codex_review": false
}
```

**输出示例**：
```json
{
  "ok": true,
  "guide": "vc_formal",
  "request_id": "req_20260304_001",
  "answer": "自动黑盒（fml_cc_autobbox）的目的是在 SoC 级 Connectivity Checking 中自动抽象/黑盒化与当前连通性证明无关的模块，以降低形式验证复杂度并提升收敛效率。",
  "citations": [
    {
      "page": 344,
      "section_path": "12.7.2 The fml_cc_autobbox Application Variable",
      "snippet": "The CC application of VC Formal is mostly used at the SOC level... optimization techniques are introduced under the application variable fml_cc_autobbox..."
    }
  ],
  "confidence": 0.86,
  "limitations": [
    "请确认你的 VC Formal 版本与手册版本一致"
  ]
}
```

### Tool-03 `get_vcs_evidence`
**用途**：按 request_id 拉取证据包。

**输入**：`request_id`、`guide`、`limit`（`guide` 必须与提问时一致）

**输出**：证据明细、排序分数、命中元数据。

### Tool-04 `health_check`
**用途**：服务健康与版本检查。

**输出**：服务状态、索引版本、最近构建时间、依赖状态。

### 错误码（示例）

- `E1001`：参数缺失/非法
- `E2001`：索引不存在
- `E2002`：索引构建失败
- `E3001`：证据不足（拒答或需澄清）
- `E3002`：低置信度结果
- `E9001`：路径不在白名单/权限拒绝

---

## 10. 安全与合规要求

1. **文档许可边界**：仅处理已授权本地文档，不外传原文。
2. **专有信息保护**：日志默认不落完整段落，仅落摘要与指纹。
3. **路径安全**：仅允许配置白名单路径读文件。
4. **最小权限运行**：服务进程限制文件系统访问范围。
5. **出口与合规提醒**：遵循文档中声明的法规与公司策略。
6. **防幻觉策略**：无证据不得给确定性结论。

---

## 11. 评测与验收标准

### 11.1 评测集

- 构建 VCS 高频问题集（建议 100+）：
  - 选项用法
  - 流程对比
  - 报错诊断
  - 性能调优
  - 概念解释

### 11.2 指标

- Retrieval Recall@K（证据命中率）
- Citation 完整率（页码+章节+片段）
- Grounded Accuracy（答案与证据一致性）
- Hallucination Rate（无依据结论占比）
- 拒答正确率（应拒答场景下的正确拒答）
- P95 响应时延

### 11.3 Go/No-Go（V1）

- citation 完整率达到上线阈值；
- 幻觉率控制在可接受范围；
- 高频问题集通过率达到目标；
- 安全检查（路径、日志、权限）全部通过。

---

## 12. 里程碑（阶段制）

- **M1：PRD 定稿**
  - 输出：评审通过的需求与验收口径。
- **M2：技术设计完成**
  - 输出：模块设计、数据模型、接口 schema。
- **M3：V1 开发联调**
  - 输出：可在 Claude Code 中调用的 MCP Server。
- **M4：UAT 与上线准备**
  - 输出：评测报告、运维手册、发布基线。

---

## 13. 风险与缓解

1. **PDF 解析质量风险**（表格/代码块丢格式）
   - 缓解：解析回退策略 + 关键章节人工抽检。

2. **参数歧义风险**（相似选项误命中）
   - 缓解：exact match boosting + section-path 约束。

3. **幻觉风险**
   - 缓解：证据绑定生成 + 无证据拒答策略。

4. **合规风险（专有文档）**
   - 缓解：本地处理、最小日志、访问控制。

5. **双模型协同复杂度风险**
   - 缓解：V1 默认单链路，双模型仅可选并可关闭。

---

## 14. 附录：回答格式规范（建议）

```json
{
  "answer": "先给结论，再给操作建议。",
  "citations": [
    {
      "page": 85,
      "section_path": "2.VCS Flow > Simulation > Commonly Used Runtime Options",
      "snippet": "..."
    }
  ],
  "confidence": 0.82,
  "limitations": [
    "该结论基于当前版本手册，需确认你的VCS版本一致。"
  ],
  "next_question": "是否需要我给出 two-step 的最小命令模板？"
}
```
