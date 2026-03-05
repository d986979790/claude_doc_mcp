# VCS Guide MCP Demo（V2）

## 1) 环境准备

在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install pypdf fastmcp
```

## 2) 健康检查

```bash
.venv/bin/python vcs_mcp_demo_server.py --health
```

## 3) 构建索引

> `--guide` 默认是 `vcs`，不传时保持原有 VCS 行为。

构建 VCS 手册索引（全量，较慢）：

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide vcs --pdf-path ./vcs_user_guide.pdf --force-rebuild
```

构建 VC Formal 手册索引（全量，较慢）：

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide vc_formal --pdf-path ./VC_Formal_UserGuide.pdf --force-rebuild
```

快速演示（仅前 80 页）：

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide vcs --pdf-path ./vcs_user_guide.pdf --max-pages 80 --force-rebuild
```

快速演示 VC Formal（仅前 80 页）：

```bash
.venv/bin/python vcs_mcp_demo_server.py --build-index --guide vc_formal --pdf-path ./VC_Formal_UserGuide.pdf --max-pages 80 --force-rebuild
```

## 4) 问答

VCS 示例：

```bash
.venv/bin/python vcs_mcp_demo_server.py --ask "VCS two-step flow 和 three-step flow 有什么区别？" --guide vcs --top-k 5
```

VC Formal 示例：

```bash
.venv/bin/python vcs_mcp_demo_server.py --ask "VC Formal 某个功能如何使用？" --guide vc_formal --top-k 5
```

## 5) 拉取证据

```bash
.venv/bin/python vcs_mcp_demo_server.py --get-evidence <request_id> --guide vc_formal --limit 3
```

> 注意：`--get-evidence` 的 `--guide` 需要与当时提问使用的 guide 一致（`vcs` 或 `vc_formal`），否则会出现找不到 request_id。

## 6) 作为 MCP Server 使用（Claude Code 端到端）

### 6.1 推荐方式：使用项目 `.mcp.json`

当前项目已包含 `.mcp.json`，仅保留一个 server 名称：`vcs-doc`。

```json
{
  "mcpServers": {
    "vcs-doc": {
      "type": "stdio",
      "command": "/home/yimings/claude_vcs_doc_test1/.venv/bin/python",
      "args": [
        "/home/yimings/claude_vcs_doc_test1/vcs_mcp_demo_server.py"
      ],
      "env": {}
    }
  }
}
```

### 6.2 可选方式：用 CLI 显式注册

```bash
claude mcp add --transport stdio --scope project vcs-doc -- /home/yimings/claude_vcs_doc_test1/.venv/bin/python /home/yimings/claude_vcs_doc_test1/vcs_mcp_demo_server.py
```

查看配置：

```bash
claude mcp list
claude mcp get vcs-doc
```

删除配置：

```bash
claude mcp remove vcs-doc
```

### 6.3 在 Claude Code 中实际使用

1) 在项目目录启动：

```bash
claude
```

2) 会话内检查 MCP 连接：

```text
/mcp
```

3) 用户直接自然语言提问（不需要手动写 tool 名）：

```text
VCS two-step flow 和 three-step flow 有什么区别？
```

```text
-debug_access 相关选项在手册中如何使用？
-debug_access+f的作用？
仿真时怎么通过ucli dump fsdb波形？
```

Claude Code 会自动调用本 MCP server 的工具并整理回复。

### 6.4 端到端前置条件

- 先完成索引构建（见第 3 节）。
- `--health` 显示 `fastmcp_installed: true`、`pypdf_installed: true`。
- 若无回答或证据不足，优先检查索引是否存在、页数是否覆盖目标章节。

## 7) 当前 V2 特性

- Hybrid 检索：BM25-like + TF-IDF cosine
- 命令选项（如 `-debug_access`）加权
- TOC 噪声识别与惩罚
- 证据回查与检索调试信息（`retrieval_debug`）
- `--use-codex-review` 目前为预留参数（用于接口兼容），暂未在服务内置真实二次审校调用

## 8) 已知问题与说明

- 在“Claude Code 内再调用 `claude -p`”会触发嵌套会话限制，属于 CLI 保护机制；请在独立终端会话中执行 `claude` 进行最终交互演示。
- 如需更高答案质量，请增加 `--max-pages` 或做全量索引重建。

## 9) 产物位置

- PRD：`PRD_FastMCP_VCS_Guide_QA.md`
- 服务代码：`vcs_mcp_demo_server.py`
- MCP 配置：`.mcp.json`
- 索引/请求缓存：`.vcs_mcp_demo/`

## 10) Web GUI / API 启动与验证

### 10.1 启动 Web 服务

```bash
.venv/bin/python web_backend/server.py --host 127.0.0.1 --port 8787
```

打开浏览器访问：

```text
http://127.0.0.1:8787/
```

### 10.2 API 快速验证

健康检查：

```bash
curl -sS http://127.0.0.1:8787/api/health
```

问答接口：

```bash
curl -sS http://127.0.0.1:8787/api/qa \
  -H 'Content-Type: application/json' \
  -d '{
    "question":"VCS two-step flow 和 three-step flow 有什么区别？",
    "guide":"vcs",
    "top_k":5,
    "max_pages_for_llm":4,
    "language":"zh-CN"
  }'
```

> 若未配置 `ANTHROPIC_API_KEY`，服务会自动回退为检索答案（`llm.used=false`，并在 `limitations` 说明原因）。

### 10.3 运行 E2E 测试（Web API）

```bash
.venv/bin/python -m unittest web_backend.test_e2e -v
```

说明：
- 测试会自动拉起 `web_backend/server.py`（默认端口 `8788`）。
- 若端口冲突，可设置环境变量：`WEB_E2E_TEST_PORT=8790`。
- 若本地未构建索引，`test_qa_smoke` 会自动 `skip`（健康检查与参数校验测试仍会执行）。
