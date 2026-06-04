# 📋 Offline Auto Audit

> **100% 离线、零成本** 的本地企业合规审计系统。  
> 自动监听录音音频或会议文本，对照合规标准进行审计，输出结构化任务派发表与 Markdown 报告。

---

## ✨ 核心特性

- 🔒 **完全离线**：所有语音转文字、大模型推理与向量化均在本地运行，数据不上传任何云端，安全可靠。
- 🎙️ **音频自动转录 (Optional)**：支持监听 `recordings/` 目录，自动将各种格式的音频文件转为 16kHz WAV 后利用 whisper.cpp 转录为文本。
- 🧠 **RAG 增强审计**：通过语义检索自动匹配最相关合规条款，审计精准有据可查。
- 📄 **双格式输出**：审计结果同时生成 `.csv` 任务指派表（可直接导入项目管理工具）与 `.md` 审计报告。
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
output/*.csv (任务指派表) & output/*.md (审计报告)
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

---

## 📁 目录结构

```
offline_auto_audit/
├── app.py                        # 🚀 合规审计主程序
├── transcribe.py                 # 🎙️ 音频转文字守护程序
├── requirements.txt              # Python 依赖
│
├── recordings/                   # 📥 放入待转录音频文件
├── inbox/                        # 📥 投入待审文件（.txt，transcribe.py 也会自动输出到此）
├── output/                       # 📤 审计结果输出（.csv + .md）
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
uv pip install -r requirements.txt
```

---

## 🔄 完整工作流

### 端到端串联运行

你可以同时启动音频转录守护进程和合规审计守护进程。

```bash
# 终端 1：启动转录守护程序 (监听 recordings/ 并输出至 inbox/)
.venv/bin/python transcribe.py

# 终端 2：启动合规审计守护程序 (监听 inbox/ 并输出至 output/)
.venv/bin/python app.py
```

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
   - 在 `output/` 下生成同名的 `.csv` 任务指派表和 `.md` 审计报告。
   - 成功后，该 txt 文件被移入 `archive/`，失败则移入 `failed/`。

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
WHISPER_LANGUAGE=zh WHISPER_MODEL=~/whisper.cpp/models/ggml-large-v3.bin .venv/bin/python transcribe.py
```

### 2. 合规审计配置 (`app.py`)
- **合规规范**：可将自定义的合规规范文件（`.txt` 格式）放入 `config/compliance_rules/` 目录下。然后**删除 `vector_store/` 目录**以触发向量库重建：
  ```bash
  rm -rf vector_store/
  ```
  下次启动 `app.py` 时，将自动加载新规并重新建立本地向量索引。
- **并发性能**：
  ```bash
  # 向量库构建时的 embedding 并发度，默认 2。根据机器性能可调整为 1 ~ 3
  EMBEDDING_CONCURRENCY=2 .venv/bin/python app.py
  ```

---

## 🖥️ 运行时交互

这两个守护脚本都支持友好的终端交互：

| 操作 | 效果 |
|------|------|
| 放入文件到监听目录 | 自动触发处理（轮询间隔 3 秒） |
| 按 `ESC` 键 | **安全退出**：等待当前正在处理的文件/音频完成后安全退出 |
| 按 `Ctrl+C` | **强制退出**：立即退出，当前正在处理的文件不会被归档，保留在原监听目录中 |

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

MIT
