# Offline Auto Audit — Local Agent Demo

> One bounded local agent runtime with two isolated workspaces:
> Technical Project Meeting Audit and Synthetic SiC Patent Research.

这是一个本地、串行、资源受限的 agent demo，同时保留原有的 Classic Audit 工作流。用户必须明确选择 `meeting_audit` 或 `patent_research`；运行时不会根据输入自行跨 workspace，也不会动态注册工具、执行 shell 或修改知识库。

## Agent Demo：能力边界

### Live 与 Replay

- **Live**：在用户批准计划后，调用本机 Ollama、受限工具和确定性 playbook，实时创建 `sessions/<session-id>/`。它是真实执行，可能完成、等待澄清、失败、中止或因预算耗尽而标记为 `INCOMPLETE`。
- **Replay**：只读取已有 session bundle 中的事件、证据和产物清单。它不调用模型、embedding、planner 或工具，也不是“重演推理”。
- 两种模式始终显示不同标识；Replay 不会在 Live 失败时被静默替代。

### 两条黄金路径

`meeting_audit` 面向技术项目会议：

1. 提取决策和任务，并验证引用确实存在于会议原文。
2. 检索本地规则，运行敏感信息、模糊表述、SOP 和发布流程检查。
3. 对缺失负责人、截止日期或验收标准发起一次合并澄清；用户可以回答或明确跳过。
4. 写出 `meeting_audit_report.md`、`tasks.csv` 和 `risk_items.csv`。

`patent_research` 只面向项目内的 synthetic SiC 功率器件语料：

1. 从产品 brief 提取有原文证据的技术特征和查询。
2. 结合可解释关键词评分、语义检索和 reciprocal-rank fusion 排序候选。
3. 只允许读取本 session 已检索到的 synthetic document ID，并生成 claim chart。
4. 两轮仍无结果时诚实结束为 `INCOMPLETE`，不编造专利或完成报告。
5. 写出 `patent_research_report.md`、`patent_retrieval_results.csv` 和 `claim_chart.csv`。

专利 corpus 固定为 10 条项目自建的合成记录。它们不是公开专利、没有真实申请人或公开号，仅用于验证检索、证据和报告链路。输出不构成新颖性、有效性、侵权、不侵权或 FTO 法律意见。

### 资源预算

| Workspace | 最大模型调用 | 最大工具调用 | 最大查询轮次 | 最大澄清次数 |
|---|---:|---:|---:|---:|
| `meeting_audit` | 4 | 10 | 2 | 1 |
| `patent_research` | 5 | 14 | 2 | 1 |

这是一组 hard budget，不是延迟或质量 SLA。runtime 只串行执行，不运行多 agent 或并行工具。

### CLI

会议 Live：

```bash
uv run python agent_cli.py live \
  --workspace meeting_audit \
  --goal "检查会议中的发布流程和任务完整性" \
  --input examples/agent_demo/meeting_with_gaps.txt
```

合成专利 Live：

```bash
uv run python agent_cli.py live \
  --workspace patent_research \
  --goal "检索与沟槽底部屏蔽结构相关的 synthetic patents" \
  --input examples/agent_demo/sic_trench_product_brief.txt
```

两条命令都会先打印 Proposed Plan，并默认要求输入 `y`；自动化环境可显式使用 `--approve-plan`。查看 session 时使用：

```bash
uv run python agent_cli.py replay --session sessions/<session-id>
```

### Streamlit

```bash
uv run streamlit run webui.py
```

页面首先显示 **Agent Demo**，其次是 **Classic Audit**。Agent Demo 提供 workspace、Live/Replay、目标与材料、计划审批、执行时间线、一次性澄清、取消以及证据/产物清单。规则编辑、corpus 重建和音频处理不在运行中的 agent session 内开放；这些 Classic 功能仍保留在原页面。

### Session bundle 与证据

每个 Live session 使用原子写入和 append-only 事件日志：

```text
sessions/<session-id>/
├── request.json
├── session.json
├── events.jsonl
├── evidence.json
└── artifacts/
```

关键报告引用使用 `evidence_id` 连接到 `evidence.json`。会议原始文本不会写入 session；持久化字符串会经过敏感信息遮蔽。Replay 保留创建 session 时的模型、知识版本和历史产物路径，并可提示当前知识版本不同。

输入材料和模型输出始终按不可信数据处理。会议中的“忽略规则”“切换 workspace”“读取 `.env`”或“调用 shell”等文字不会改变 workspace、工具 allowlist 或文件边界。专利模型在检索前不得提供 document ID，候选读取也受本 session 的检索结果约束。

### 本机模型与测量状态

默认模型为 `qwen3.5:9b`，embedding 为 `nomic-embed-text`。目标机器为 Apple M1 Pro（8 CPU cores、14 GPU cores、16 GB RAM）。这套硬件说明不是对其他电脑的兼容性承诺。

Agent Demo 的结构化 JSON 调用会关闭 thinking、启用 JSON mode，并把单次输出限制为 512 token，避免 9B 模型在本机进入长时间无界生成；这不改变 Classic Audit 的模型调用配置。

2026-07-12 在上述 M1 Pro 上各执行了一次真实 Live benchmark；结果是单次实测，不是 SLA：

| Workspace | 耗时 | 模型调用 | 工具调用 | Query rounds | 状态 |
|---|---:|---:|---:|---:|---|
| `meeting_audit` | 26.627 秒 | 1 | 5 | 0 | `COMPLETED` |
| `patent_research` | 23.993 秒 | 1 | 8 | 1 | `COMPLETED` |

两个 session bundle 均通过证据、预算和产物校验；meeting session 也通过 Replay CLI 读取验证。benchmark JSON 和可 Replay session 保留在被 Git 忽略的本机 `output/` 与 `sessions/`，不会作为通用样例提交。复现实测可运行：

```bash
uv run python scripts/benchmark_agent.py \
  --workspace meeting_audit \
  --goal "检查发布流程、任务完整性和敏感信息" \
  --input examples/agent_demo/meeting_with_gaps.txt

uv run python scripts/benchmark_agent.py \
  --workspace patent_research \
  --goal "检索与沟槽底部屏蔽结构相关的 synthetic patents" \
  --input examples/agent_demo/sic_trench_product_brief.txt
```

脚本记录实际 elapsed time、调用次数、终态和 session ID 到被 Git 忽略的 `output/`。如果 Ollama 或模型不可用，它只报告错误，不启动服务、不下载模型，也不硬编码成功状态。

真实模型 smoke tests 默认跳过，不影响离线测试门禁。只有 Ollama 已由用户启动且两个模型已经安装时，才显式运行：

```bash
RUN_OLLAMA_SMOKE=1 uv run pytest -m ollama -q
```

测试不会启动 Ollama、执行 `ollama pull` 或下载模型。普通确定性门禁可排除这些测试：

```bash
uv run pytest -m "not ollama" -q
```

### 自动化验证层

校验任意已保存 session 的格式、预算、证据、脱敏和产物契约：

```bash
uv run python scripts/validate_agent_session.py sessions/<session-id>
uv run python scripts/validate_agent_session.py sessions/<session-id> --json
```

Streamlit 的导航、workspace、计划审批、会议澄清、Replay 和 Classic 切换由 `streamlit.testing.v1.AppTest` 在默认 pytest 中使用 deterministic adapters 自动覆盖。适配器只替换模型和检索结果；runtime、状态机、工具、证据持久化和产物写入仍使用生产实现。只有环境变量严格等于 `AGENT_DEMO_TEST_MODE=1` 时才允许启用，正常 UI 不会静默进入 fake mode。

真实浏览器 smoke 是可选门禁，需要 Node.js/npm、`npx`、可执行的 Playwright CLI wrapper 和可用的 Playwright 浏览器：

```bash
bash scripts/playwright_agent_smoke.sh --check
bash scripts/playwright_agent_smoke.sh
bash scripts/playwright_agent_smoke.sh --live-cancel
```

默认脚本启动隔离端口上的 fake-backed Streamlit，操作会议审批、澄清、Replay 和 Classic 页面，并通过退出 trap 清理服务。`--live-cancel` 改用本机 Ollama，点击 `Cancel Agent` 并等待页面显示 `Session status: CANCELLED`；该路径要求 Ollama 已启动且 README 中的默认模型可用。脚本会优先使用本机 Brave，其次使用 Chrome，也可通过 `PLAYWRIGHT_BROWSER_EXECUTABLE` 指定其他 Chromium 可执行文件；npm、Playwright daemon 和浏览器 cache 均隔离在被 Git 忽略的 `output/playwright/`。日志、snapshot 和 screenshot 也只写入该目录。脚本不会执行全局 npm 安装、修复 npm 权限或下载浏览器；缺少这些外部条件时会非零退出并保留准确错误信息。

### 限制

- 这是单机 demo，没有认证、多用户、服务端部署、跨 session 语义记忆或 autonomous knowledge mutation。
- Agent Demo 只接受粘贴文本或 TXT；Classic Audit 继续提供可选音频转录。
- 专利研究不连接 Google Patents、Espacenet、WIPO、CNIPA 或任何真实专利 API，也不解析真实 PDF/OCR。
- 模型生成仍可能失败；证据验证、确定性规则和预算用于限制失败范围，而不是保证结论正确。
- Live 需要本机 Ollama 和对应模型；Replay 只需要一个完整、可信的 session bundle。

---

## 🎯 Portfolio Demo

这个项目面向 **AI 应用开发** 与 **数据分析自动化** 场景：把非结构化会议记录或录音转成可审计、可汇总、可交付的结构化治理数据。

不用安装本地模型也可以先看脱敏样例：

| 内容 | 文件 |
|------|------|
| 样例会议输入 | [`examples/sample_meeting.txt`](examples/sample_meeting.txt) |
| 任务指派 CSV | [`examples/sample_tasks.csv`](examples/sample_tasks.csv) |
| 风险项 CSV | [`examples/sample_risk_items.csv`](examples/sample_risk_items.csv) |
| Markdown 审计报告 | [`examples/sample_audit_report.md`](examples/sample_audit_report.md) |
| 批量汇总报告 | [`examples/sample_portfolio_summary.md`](examples/sample_portfolio_summary.md) |
| 半导体 IP synthetic demo | [`examples/semiconductor/sample_sic_patent.txt`](examples/semiconductor/sample_sic_patent.txt) |
| 半导体 Claim Chart | [`examples/semiconductor/sample_ip_claim_chart.csv`](examples/semiconductor/sample_ip_claim_chart.csv) |
| 半导体 IP 风险项 | [`examples/semiconductor/sample_ip_risk_items.csv`](examples/semiconductor/sample_ip_risk_items.csv) |
| 半导体 IP 分析报告 | [`examples/semiconductor/sample_ip_analysis_report.md`](examples/semiconductor/sample_ip_analysis_report.md) |
| 半导体 IP 作品说明 | [`docs/portfolio_semiconductor_ip.md`](docs/portfolio_semiconductor_ip.md) |

适合在简历或面试中强调的能力点：

- **本地 AI 工作流**：Ollama + ChromaDB + whisper.cpp，适合隐私敏感的企业内网场景。
- **RAG 审计**：从本地合规条款中检索相关基准，再交给本地大模型完成审计与任务提取。
- **数据治理自动化**：自动识别手机号、邮箱、身份证、客户名称、员工信息、SOP 缺口和跨部门协作风险。
- **结构化分析输出**：生成任务 CSV、风险 CSV、Markdown 报告，并可进一步汇总成指标报告。
- **半导体 IP 技术情报模式**：对 synthetic SiC MOSFET 专利/产品文本做权利要求拆解、FTO 初筛提示和可审校报告生成。
- **审计历史统计**：每次审计完成后自动追加 `output/audit_history.jsonl`，便于查看分析模式、风险类型、高风险和人工复核趋势。
- **可测试工程实现**：核心清洗、脱敏、取消、汇总和输出逻辑有自动化测试覆盖。

批量汇总已有审计输出：

```bash
uv run python summarize_audits.py --output-dir output --write output/portfolio_summary.md
```

汇总审计历史索引：

```bash
uv run python summarize_audits.py --output-dir output --history --write output/audit_history_summary.md
```

---

## ✨ 核心特性

- 🔒 **完全离线**：所有语音转文字、大模型推理与向量化均在本地运行，数据不上传任何云端，安全可靠。
- 🎙️ **音频自动转录 (Optional)**：支持监听 `recordings/` 目录，自动将各种格式的音频文件转为 16kHz WAV 后利用 whisper.cpp 转录为文本。
- 🧠 **RAG 增强审计**：通过语义检索自动匹配最相关合规条款，审计精准有据可查。
- 🧭 **数据治理风险检查**：自动识别身份证、手机号、邮箱、客户名称、员工信息等敏感信息，以及符合 SOP（负责人/截止时间/验收标准）校验、模糊表述、跨部门协作风险和模型不确定性的人工复核项。
- 📄 **审计包输出**：同时生成任务指派 CSV、风险项 CSV 与 Markdown 审计报告，方便演示企业内部数据合规和流程治理场景。
- 🧩 **半导体 IP Intelligence Mode**：支持 `semiconductor_ip` 模式，面向公开专利文本、技术交底、产品说明和论文摘要生成 claim chart、IP 风险项和技术情报报告。
- 🖥️ **WebUI 演示界面**：支持粘贴文本、上传 TXT/音频离线转录、按分析模式切换默认样例、在侧边栏校验与一键拉取 Ollama 模型，以及在线编辑与重构合规条款数据库。
- 🔄 **端到端持续轮询**：监听目录自动触发。放入音频文件 -> 自动转录至 `inbox/` -> 自动触发合规分析，无需人工干预。
- 🛡️ **文件安全分流**：成功 -> `archive/`，失败 -> `failed/`，原始文件绝不丢失。
- ⌨️ **优雅/安全退出**：运行期间均可随时按 `ESC` 键等待当前文件处理完成后安全退出。
- 🔧 **零配置启动**：首次运行自动生成默认合规规范与所需目录。

---

## 🏗️ 技术架构

```
待转录音频 (.mp3/.m4a/...)
    │
    ▼ [recordings/ 目录监听]
[transcribe.py (whisper.cpp)] ──► 自动转换 & 转录
    │
    ▼
待审文本 (.txt)
    │
    ▼ [inbox/ 目录监听]
[app.py (RAG 审计核心)]
    │
    ▼
nomic-embed-text  ──►  ChromaDB (本地向量库)
    │                        │
    │                  语义检索最相关合规条款
    │                        │
    │                  qwen3.5:9b (8K ctx)
    │                  合规审计 + 任务提取
    │                        │
    ├────────────────────────┘
    │
    ▼
output/*_tasks.csv + *_risk_items.csv + *_audit_report.md
    │
    ▼
archive/ (成功归档) 或 failed/ (失败隔离)
```

| 模块/组件 | 技术选型 | 说明 |
|------|----------|------|
| **音频转录脚本** | `transcribe.py` | 监听 `recordings/`，负责音频探测、转换与 Whisper 转录 |
| **合规审计脚本** | `app.py` | 监听 `inbox/`，负责 RAG 检索、大模型推理与结果输出 |
| **语音识别引擎** | `whisper-cli`（whisper.cpp） | 本地极速语音识别，推荐使用 `ggml-medium.bin` 模型 |
| **大语言模型** | `qwen3.5:9b`（Ollama） | 负责合规推理与 JSON 任务提取 |
| **向量嵌入模型** | `nomic-embed-text`（Ollama） | 负责文本语义向量化 |
| **向量数据库** | ChromaDB（本地嵌入式） | 合规知识库持久化存储 |
| **格式转换工具** | `ffmpeg` + `ffprobe` | 自动探测并将各类音频/视频转为 16kHz 单声道 WAV |
| **数据处理** | pandas | 任务清洗、去重、CSV 导出 |
| **WebUI** | Streamlit | 浏览器内粘贴/上传文本、查看风险表和下载审计包 |
| **批量汇总** | `summarize_audits.py` | 汇总多次审计输出，生成风险分布和人工复核指标 |

---

## 📁 目录结构

```
offline_auto_audit/
├── app.py                        # 🚀 合规审计主程序
├── webui.py                      # 🖥️ 浏览器审计界面
├── transcribe.py                 # 🎙️ 音频转文字守护程序
├── summarize_audits.py           # 📊 批量审计结果汇总脚本
├── pyproject.toml                # Python 依赖与测试配置
├── examples/                     # 🧪 脱敏演示输入与输出样例
│
├── recordings/                   # 📥 放入待转录音频文件
├── inbox/                        # 📥 投入待审文件（.txt，transcribe.py 也会自动输出到此）
├── output/                       # 📤 审计包输出（任务 CSV + 风险 CSV + 审计报告）
├── archive/                      # 🗄️  处理成功后的原文件/音频归档（在 .gitignore 中）
├── failed/                       # ⚠️  解析/转录失败的文件隔离区（在 .gitignore 中）
│
├── config/
│   └── compliance_rules/         # 📚 合规规范文档（.txt）
│       └── standard_pmo_...txt   # 首次运行自动生成的默认规范
│
└── vector_store/                 # 🔮 ChromaDB 本地向量库（自动管理，在 .gitignore 中）
```

---

## 🚀 快速开始

### 1. 前置要求

- **操作系统**：macOS（推荐 Apple Silicon M 系列芯片，16GB 以上统一内存）
- **Ollama**：[Ollama.app](https://ollama.com/download) 已安装并**以 App 形式运行**（非 Homebrew 命令行版本，Homebrew 版缺乏完整加速后端）
- **FFmpeg**：`brew install ffmpeg`（用于音频格式转换，确保 `ffmpeg` 和 `ffprobe` 可用）
- **Python**：Python 3.10+
- **uv**：[uv](https://github.com/astral-sh/uv) 包管理器

### 2. 准备大模型与向量模型

```bash
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
```

### 3. 构建 whisper.cpp 与模型下载

本系统使用 `whisper.cpp` 进行超轻量、高性能的本地音频转文字。

```bash
# 1. 编译 whisper.cpp (确保启用 Metal 以支持 Apple Silicon GPU 加支)
cd ~/whisper.cpp
cmake -B build -DGGML_METAL=ON
cmake --build build --config Release -j$(sysctl -n hw.logicalcpu)

# 2. 创建全局软链接（可选，transcribe.py 会自动尝试寻找常规路径）
sudo ln -sf ~/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper

# 3. 下载模型（推荐使用 medium 模型，中文识别效果好）
bash models/download-ggml-model.sh medium
```

*注：如需使用其他模型，可在运行转录时指定环境变量（见后文配置说明）。*

### 4. 安装依赖

```bash
# 创建虚拟环境并同步依赖
uv venv
uv sync
```

---

## 🖥️ WebUI 快速体验

如果只想演示会议文本、SOP 或任务指派文本的审计结果，可以直接启动浏览器界面：

```bash
uv run streamlit run webui.py
```

启动后打开 Streamlit 显示的本地地址，通常是：

```text
http://localhost:8501
```

WebUI 支持：

- 粘贴会议记录 / SOP / 任务指派文本
- 上传 `.txt` 文件
- **语音转文字审计**：直接上传音频文件，系统将检测本地 Whisper 依赖并一键转录与审计。
- **合规条款管理**：在线浏览、新建、修改或删除 `.txt` 条款文件，保存时自动触发语义向量库重构，无需手动清理数据库。
- **服务与模型看栏**：侧边栏实时展示 Ollama 连接状态及模型就绪情况，并支持在界面上一键拉取（下载）缺失的模型。
- 点击 **开始审计** 后显示运行中转圈提示（已解决每秒闪烁跳动的问题）
- 审计过程中点击 **停止审计**，中断本次任务并回到可重新开始状态
- 在线查看风险项表格、任务表格和 Markdown 审计报告
- 在 **历史统计** 页查看累计审计次数、风险类型分布和最近审计记录
- 在 **历史统计** 页查看企业合规与半导体 IP 模式的历史分布
- 下载任务 CSV、风险项 CSV 和 Markdown 审计报告

推荐演示流程：

1. 启动 Ollama，并确保 `qwen3.5:9b` 与 `nomic-embed-text` 已下载。
2. 运行 `uv run streamlit run webui.py`。
3. 使用页面默认样例，或粘贴一段包含会议记录、SOP 缺口、模糊表述和敏感信息的文本。
4. 点击 **开始审计**，观察运行中转圈提示。
5. 如需展示可控中断，点击 **停止审计**。
6. 审计完成后查看风险项、任务表和审计报告，并下载 CSV / Markdown 文件。

> WebUI 面向演示和单次审计体验；如需批量自动处理，请使用下方 `app.py` 监听 `inbox/` 的完整工作流。

---

## 🔄 完整工作流

### 端到端串联运行

你可以同时启动音频转录守护进程和合规审计守护进程。

```bash
# 终端 1：启动转录守护程序 (监听 recordings/ 并输出至 inbox/)
uv run python transcribe.py

# 终端 2：启动合规审计守护程序 (监听 inbox/ 并输出至 output/)
uv run python app.py
```

半导体专利/IP技术情报模式可用独立规则库运行：

```bash
uv run python app.py --mode semiconductor_ip
```

本模式会生成 `*_claim_chart.csv`、`*_ip_risk_items.csv` 和 `*_ip_analysis_report.md`。它只用于技术情报整理、专利文本理解和 IP 初筛，不构成法律意见、侵权结论或专利有效性判断，所有结果均需人工复核。
半导体模式默认使用较短输入窗口和输出上限，便于本地模型快速完成演示；可通过 `SEMICONDUCTOR_IP_INPUT_TOKEN_LIMIT` 与 `SEMICONDUCTOR_IP_NUM_PREDICT` 调整。

### 详细步骤：
1. **音频输入**：将会议录音（例如 `weekly_meeting.m4a`）放入 `recordings/`。
2. **转录归档**：
   - `transcribe.py` 检测到音频，使用 `ffmpeg` 转换为 16kHz WAV（如需）。
   - 调用 `whisper.cpp` 进行转录，输出 `inbox/weekly_meeting_YYYY-MM-DD_HH_MM.txt`。
   - 成功后，原始音频移入 `archive/`。若失败，移入 `failed/`。
3. **合规审计**：
   - `app.py` 监听到了 `inbox/` 中的 `weekly_meeting_YYYY-MM-DD_HH_MM.txt`。
   - 使用 RAG 从向量库中检索最相关的合规条款。
   - 结合 `qwen3.5:9b` 模型推理进行合规审计并提取具体待办任务。
   - 使用本地规则模块识别敏感信息、模糊表述、SOP 缺口和跨部门协作风险。
   - 在 `output/` 下生成任务指派 CSV、风险项 CSV 和 Markdown 审计报告。
   - 成功后，该 txt 文件被移入 `archive/`，失败则移入 `failed/`。

---

## 🧪 数据治理演示样例

可将以下文本保存为 `inbox/demo_meeting.txt` 触发审计：

```text
今天会议决定把客户张女士的手机号 13812345678 和邮箱 zhangsan@example.com 发给销售团队。
研发后续尽快处理数据导出脚本，相关人员负责。
产品和法务一起看一下，没问题就上线。
```

系统会在报告和风险 CSV 中标出明文敏感信息、模糊表述、SOP 缺口、跨部门协作风险，并对手机号和邮箱做脱敏展示。

---

## 🎵 支持的音频格式

| 类别 | 格式 | 处理方式 |
|------|------|----------|
| **原生支持** | `.wav` `.mp3` `.flac` `.ogg` | 直接转录，无需转换 |
| **自动转换** | `.m4a` `.aac` `.wma` `.opus` `.webm` | ffmpeg → 16kHz WAV → 转录 |
| **手机录音** | `.amr` `.3gp` | ffmpeg → 16kHz WAV → 转录 |
| **视频容器** | `.mp4` `.mkv` `.avi` `.mov` | 提取音轨 → 16kHz WAV → 转录 |

---

## ⚙️ 系统配置与环境变量

### 1. 音频转录配置 (`transcribe.py`)
主要通过环境变量进行配置，无需修改代码：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WHISPER_MODEL` | `~/whisper.cpp/models/ggml-medium.bin` | Whisper 模型文件路径 |
| `WHISPER_LANGUAGE` | `auto` | 识别语言代码（如 `zh`, `en`, `ja`, `auto`） |
| `WHISPER_THREADS` | CPU 核心数 | 转录线程数 |

**运行示例**：
```bash
# 强制使用中文识别，并指定 large 模型
WHISPER_LANGUAGE=zh WHISPER_MODEL=~/whisper.cpp/models/ggml-large-v3.bin uv run python transcribe.py
```

### 2. 合规审计配置 (`app.py`)
- **合规规范**：可将自定义的合规规范文件（`.txt` 格式）放入 `config/compliance_rules/` 目录下。
  - **WebUI 方式**：直接在 WebUI 的 **“合规条款管理”** 选项卡中编辑或新建，保存时系统会自动重构向量数据库。
  - **命令行方式**：手动放置文件后，**删除 `vector_store/` 目录**以触发向量库重建：
    ```bash
    rm -rf vector_store/
    ```
    下次启动 `app.py` 时，将自动加载新规并重新建立本地向量索引。
- **并发性能**：
  ```bash
  # 向量库构建时的 embedding 并发度，默认 2。根据机器性能可调整为 1 ~ 3
  EMBEDDING_CONCURRENCY=2 uv run python app.py
  ```

---

## 📦 输出文件说明

每次审计成功后，`output/` 目录会生成一组审计包文件：

| 文件 | 内容 |
|------|------|
| `*_tasks.csv` | 结构化任务清单，包括任务名称、负责人、截止日期、验收标准、优先级等字段 |
| `*_risk_items.csv` | 风险项清单，包括风险类型、严重级别、证据片段、整改建议和是否需要人工复核 |
| `*_audit_report.md` | Markdown 审计报告，整合合规结论、RAG 参考基准、任务表和风险项 |

敏感字段会在报告、CSV 和输出文件名中尽量脱敏，例如手机号、邮箱和身份证号不会以完整明文展示。

### 批量汇总报告

如需把多次审计输出整理成数据治理指标，可运行：

```bash
uv run python summarize_audits.py --output-dir output --write output/portfolio_summary.md
```

汇总报告包括审计文件数、任务数、风险数、高/中风险分布、风险类型分布和人工复核数量。

半导体 IP 输出可单独汇总 claim chart、IP 风险项、人工复核项、严重级别、风险类型和技术特征分布：

```bash
uv run python summarize_audits.py --mode semiconductor_ip --output-dir output --write output/ip_portfolio_summary.md
```

---

## 🧪 测试

项目使用 `pytest`，推荐在提交前运行：

```bash
uv run pytest
```

也可以运行标准库 `unittest` 入口：

```bash
uv run python -m unittest tests.test_app -v
```

当前测试覆盖重点包括：

- 数据治理规则识别：敏感信息、模糊表述、SOP 缺口、跨部门协作风险
- 审计输出生成：任务 CSV、风险项 CSV、Markdown 报告
- 半导体 IP 输出：claim chart、IP 风险项、Markdown 报告和 portfolio 汇总
- 脱敏逻辑：报告、CSV、日志和输出文件名不泄露完整敏感字段
- WebUI 停止审计底层取消通道

---

## 🖥️ 运行时交互

守护脚本和 WebUI 都支持友好的运行时交互：

| 操作 | 效果 |
|------|------|
| 放入文件到监听目录 | 自动触发处理（轮询间隔 3 秒） |
| WebUI 点击 **停止审计** | 中断当前 WebUI 审计任务，不生成审计包 |
| 守护脚本按 `ESC` 键 | **安全退出**：等待当前正在处理的文件/音频完成后安全退出 |
| 守护脚本按 `Ctrl+C` | **强制退出**：立即退出，当前正在处理的文件不会被归档，保留在原监听目录中 |

---

## ⚠️ 常见问题

**Q：启动 `app.py` 时报 `ConnectionError: Failed to connect to Ollama`**  
A：请确认使用的是 [Ollama 官方 App](https://ollama.com/download)（`/Applications/Ollama.app`），而非通过 `brew install ollama` 安装的后台服务版。在 Apple Silicon 上，App 才能完美调用 GPU 加速。

**Q：音频转录报 `dyld: Library not loaded: libwhisper.1.dylib`**  
A：`transcribe.py` 内部已自动设置 `DYLD_LIBRARY_PATH` 环境变量，如果仍然报错，请确保 `whisper-cli` 编译时共享库路径正确，或在 `~/.zshrc` 中手动指定：`export DYLD_LIBRARY_PATH=~/whisper.cpp/build:$DYLD_LIBRARY_PATH`。

**Q：文件被移入了 `failed/` 目录**  
- **音频转录失败**：可能是音频文件损坏或 `ffmpeg` 转换出错。可以使用 `ffprobe <音频文件>` 检查。
- **合规审计失败**：通常是由于模型推理输出被截断（没有生成完整合规 JSON）。你可以直接将文件从 `failed/` 移回 `inbox/` 重试。

**Q：想更换大语言模型**  
A：可以修改 `app.py` 中 `model='qwen3.5:9b'`（第 247 行左右）为已下载的其他 Ollama 模型名称。

**Q：中文识别结果中混入过多英文或乱码**  
A：强烈建议显式指定 `WHISPER_LANGUAGE=zh`，这样可以引导模型优先输出中文。

---

## 📄 License

This project is licensed under the terms of the GNU General Public License v3.0 (GPL-3.0). See the [LICENSE](LICENSE) file for the full license text.
