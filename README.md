# 📋 Offline Auto Audit

> **100% 离线、零成本** 的本地 RAG 企业合规审计系统。  
> 自动监听会议记录/项目日志，对照合规标准进行审计，输出结构化任务派发表与 Markdown 报告。

---

## ✨ 核心特性

- 🔒 **完全离线**：所有推理与向量化均在本地运行，数据不上传任何云端
- 🧠 **RAG 增强审计**：通过语义检索自动匹配最相关合规条款，审计精准有据可查
- 📄 **双格式输出**：同时生成 `.csv` 任务指派表（可直接导入项目管理工具）与 `.md` 审计报告
- 🔄 **持续轮询**：监听 `inbox/` 目录，放入文件即自动触发分析，无需人工干预
- 🛡️ **文件安全分流**：成功 → `archive/`，失败 → `failed/`，原始文件绝不丢失
- 🔧 **零配置启动**：首次运行自动生成默认合规规范与所需目录

---

## 🏗️ 技术架构

```
待审文件 (.txt)
    │
    ▼
[inbox/ 目录监听]
    │
    ▼
nomic-embed-text  ──►  ChromaDB (本地向量库)
    │                        │
    │                  语义检索最相关合规条款
    │                        │
    └────────────────────────┘
                 │
                 ▼
          qwen3.5:9b (8K ctx)
          合规审计 + 任务提取
                 │
         ┌───────┴────────┐
         ▼                ▼
   output/*.csv     output/*.md
   (任务指派表)     (审计报告)
         │
         ▼
    archive/ 或 failed/
```

| 组件 | 技术选型 | 说明 |
|------|----------|------|
| 大语言模型 | `qwen3.5:9b`（Ollama） | 负责合规推理与 JSON 任务提取 |
| 向量嵌入模型 | `nomic-embed-text`（Ollama） | 负责文本语义向量化 |
| 向量数据库 | ChromaDB（本地嵌入式） | 合规知识库持久化存储 |
| 数据处理 | pandas | 任务清洗、去重、CSV 导出 |

---

## 🚀 快速开始

### 1. 前置要求

- macOS（Apple Silicon M 系列推荐，16GB 统一内存以上）
- [Ollama.app](https://ollama.com/download) 已安装并**以 App 形式运行**（非 Homebrew 版本）
- Python 3.10+
- `uv`（Python 包管理器）

### 2. 拉取所需模型

```bash
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
```

> 首次拉取约需下载 6–7 GB，请确保有足够磁盘空间。

### 3. 安装依赖

```bash
# 创建虚拟环境并安装
uv venv
uv pip install -r requirements.txt
```

### 4. 启动脚本

```bash
.venv/bin/python app.py
```

首次启动时，脚本会自动：
- 创建所有必要目录（`inbox/`、`output/`、`archive/`、`failed/`、`config/compliance_rules/`、`vector_store/`）
- 生成默认 PMO 合规规范文件并写入向量库

### 5. 可选性能参数

向量库首次构建时，脚本默认同时发起 2 个 embedding 请求，适合 M1 Pro 16GB 这类本地 Ollama 环境：

```bash
EMBEDDING_CONCURRENCY=2 .venv/bin/python app.py
```

如果初始化稳定但希望更快，可以尝试 `EMBEDDING_CONCURRENCY=3`；如果同时运行 IDE、浏览器或其他大模型任务，建议降为 `1`。

### 6. 开始审计

将待审计的会议记录或项目日志（`.txt` 格式）放入 `inbox/` 目录，脚本将自动开始分析。

---

## 🎙️ 音频转文字（可选前置步骤）

如果你的输入是音频文件（会议录音、通话录音等），可以使用内置的转录工具将音频自动转为文字：

```bash
# 单个文件转录（输出 .txt 自动存入 inbox/，触发审计）
.venv/bin/python transcribe.py recording.m4a

# 指定语言为中文（默认 auto 自动检测）
.venv/bin/python transcribe.py meeting.mp3 --language zh

# 批量处理整个目录
.venv/bin/python transcribe.py ./recordings/

# 自定义输出目录（不自动触发审计）
.venv/bin/python transcribe.py call.wav --output-dir ./transcripts/
```

**前置要求**：
- [ffmpeg](https://formulae.brew.sh/formula/ffmpeg) 已安装（`brew install ffmpeg`）
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) 已构建，且 `ggml-medium.bin` 模型已下载

**支持的音频格式**：`.wav`、`.mp3`、`.flac`、`.ogg`（直接处理），`.m4a`、`.aac`、`.wma`、`.opus`、`.webm`、`.amr`、`.3gp`（自动转换后处理）

**完整端到端流程**：
```
音频文件 → transcribe.py → inbox/*.txt → app.py 自动审计 → output/
```

---

## 📁 目录结构

```
offline_auto_audit/
├── app.py                        # 主程序
├── requirements.txt              # Python 依赖
│
├── inbox/                        # 📥 投入待审文件（.txt）
├── output/                       # 📤 审计结果输出（.csv + .md）
├── archive/                      # 🗄️  处理成功后的原文件归档
├── failed/                       # ⚠️  解析失败的文件隔离区
│
├── config/
│   └── compliance_rules/         # 📚 合规规范文档（.txt）
│       └── standard_pmo_...txt   # 首次运行自动生成的默认规范
│
└── vector_store/                 # 🔮 ChromaDB 本地向量库（自动管理）
```

---

## 🔄 完整工作流

```
放入 inbox/
    │
    ├─ 语义检索匹配合规条款（RAG）
    ├─ qwen3.5:9b 推理审计（8K 上下文）
    ├─ JSON 响应清洗与解析
    ├─ pandas 任务去重与字段标准化
    └─ 生成 CSV + Markdown 报告至 output/
         │
         ├─ 成功 ──► 原文件移入 archive/
         └─ 失败 ──► 原文件移入 failed/（不重试，等待人工处置）
```

### 输出文件命名规则

```
output/{原文件名}_{YYYY-MM-DD_HH_MM}.csv
output/{原文件名}_{YYYY-MM-DD_HH_MM}.md
```

---

## ⚙️ 自定义合规规范

将自定义合规文档（`.txt` 格式）放入 `config/compliance_rules/` 目录，**然后删除 `vector_store/` 目录**触发重建：

```bash
rm -rf vector_store/
```

下次启动脚本时，系统会自动读取所有规范文档、切片向量化并写入新的向量库。

> **支持多文件**：可放入多份规范文档（按文件名字母序加载），系统会合并为统一知识库。

---

## 🖥️ 运行时交互

| 操作 | 效果 |
|------|------|
| 放入 `.txt` 文件至 `inbox/` | 自动触发分析（轮询间隔 3 秒） |
| 按 `ESC` 键 | 等待当前文件分析完成后安全退出 |
| 按 `Ctrl+C` | 立即强制退出（当前文件不归档，保留在 inbox） |

---

## ⚠️ 常见问题

**Q：启动时报 `ConnectionError: Failed to connect to Ollama`**  
A：请确认使用的是 [Ollama 官方 App](https://ollama.com/download)（`/Applications/Ollama.app`），而非通过 `brew install ollama` 安装的版本。Homebrew 版在 Apple Silicon 上缺少完整后端。

**Q：文件被移入了 `failed/` 目录**  
A：通常是模型单次输出被截断（未能生成完整 JSON）。可将该文件移回 `inbox/` 重试，若持续失败说明文件内容本身不适合当前 Prompt 格式。

**Q：想更换更大/更小的模型**  
A：修改 `app.py` 第 247 行的 `model='qwen3.5:9b'` 为目标模型名称，确保已通过 `ollama pull` 拉取对应模型。

**Q：向量库初始化很慢**  
A：首次构建时会并发调用 `nomic-embed-text` 对所有合规规范切片进行向量化（已使用 `asyncio.gather` 并发优化）。后续启动会跳过此步骤，直接加载已有向量库。

---

## 📄 License

MIT
