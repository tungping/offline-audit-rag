# 🎙️ transcribe.py — 离线音频自动转文字工具

> **100% 离线、零成本**的本地语音转文字守护工具。  
> 持续监听 `recordings/` 目录，发现音频文件后自动转录，结果输出至 `inbox/`。  
> 可与 `app.py` 审计流程无缝串联：**音频录音 → 转文字 → 自动合规审计**。

---

## ✨ 功能特性

- 🔒 **完全离线**：所有处理均在本地完成，音频数据不上传任何云端
- 👁️ **持续监听**：守护进程模式，放入录音文件即自动触发转录，无需人工干预
- 🔄 **智能格式处理**：自动检测音频格式，兼容格式直接处理，不兼容格式通过 ffmpeg 自动转换
- 📁 **批量处理**：一次性处理 `recordings/` 中所有待转录文件，按文件名排序执行
- 🛡️ **文件安全分流**：成功 → `archive/`，失败 → `failed/`，原始文件绝不丢失
- 🌐 **多语言支持**：支持中文、英文、日文等 90+ 种语言，或自动检测
- ⌨️ **安全退出**：按 ESC 键等待当前文件处理完成后优雅退出

---

## 🏗️ 工作流程

```
recordings/ 目录监听（每 3 秒轮询）
        │
        ▼
 [ffprobe 格式探测]
        │
   ┌────┴────┐
   │         │
原生格式    其他格式
(跳过)      │
            ▼
     [ffmpeg 转换]
     16kHz 单声道 WAV
            │
        ────┘
        │
        ▼
 [whisper.cpp 转录]
        │
   ┌────┴─────────┐
   │              │
  成功            失败
   │              │
   ├─ inbox/      └─ failed/
   │  {名}_{时间戳}.txt   音频文件隔离
   │
   └─ archive/
      音频文件归档
```

| 组件 | 说明 |
|------|------|
| **ffprobe** | 探测输入文件的编解码器与时长信息 |
| **ffmpeg** | 将非兼容格式转换为 16kHz 单声道 PCM WAV |
| **whisper-cli** | 基于 whisper.cpp 的本地语音识别引擎 |
| **ggml-medium.bin** | 默认使用的 Whisper medium 模型，中文识别效果好 |

---

## 📁 目录结构

```
offline_auto_audit/
├── transcribe.py             # 本工具
│
├── recordings/               # 📥 放入待转录音频文件
├── inbox/                    # 📤 转录结果 txt 输出（自动触发 app.py 审计）
├── archive/                  # 🗄️  处理成功后的音频归档
└── failed/                   # ⚠️  转录失败的音频隔离区
```

### 输出文件命名规则

```
inbox/{原始文件名}_{YYYY-MM-DD_HH_MM}.txt
```

例如：`recordings/weekly_standup.m4a` → `inbox/weekly_standup_2024-06-04_10_30.txt`

---

## 🚀 快速开始

### 前置要求

| 依赖 | 安装方式 | 验证命令 |
|------|----------|----------|
| ffmpeg + ffprobe | `brew install ffmpeg` | `ffmpeg -version` |
| whisper.cpp | 参考 [构建说明](#whisper-cpp-构建说明) | 见下文 |
| whisper 模型 | 参考 [模型下载](#模型下载) | 见下文 |

### 启动

```bash
# 使用项目虚拟环境（推荐）
.venv/bin/python transcribe.py

# 或使用系统 Python3（脚本仅用标准库，无额外依赖）
python3 transcribe.py
```

启动后，脚本会自动创建所有必要目录并开始监听 `recordings/`：

```
🎙️  离线音频转文字工具已就绪
   模型   : ggml-medium.bin
   语言   : auto
   线程   : 8
   监听   : recordings/
   输出   : inbox/  （txt 文件）
   归档   : archive/（处理成功的音频）
   隔离   : failed/ （处理失败的音频）

将音频文件放入 recordings/ 目录即可自动触发转录。
按 ESC 键安全退出，按 Ctrl+C 强制退出。
```

---

## 🎵 支持的音频格式

| 类别 | 格式 | 处理方式 |
|------|------|----------|
| **原生支持** | `.wav` `.mp3` `.flac` `.ogg` | 直接转录，无需转换 |
| **自动转换** | `.m4a` `.aac` `.wma` `.opus` `.webm` | ffmpeg → 16kHz WAV → 转录 |
| **手机录音** | `.amr` `.3gp` | ffmpeg → 16kHz WAV → 转录 |
| **视频容器** | `.mp4` `.mkv` `.avi` `.mov` | 提取音轨 → 16kHz WAV → 转录 |

---

## ⚙️ 配置

所有配置通过环境变量调整，无需修改代码：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WHISPER_MODEL` | `~/whisper.cpp/models/ggml-medium.bin` | 模型文件路径 |
| `WHISPER_LANGUAGE` | `auto` | 语言代码（`zh`/`en`/`ja`/`auto`） |
| `WHISPER_THREADS` | CPU 核心数 | 转录线程数 |

```bash
# 示例：固定中文 + 使用 large 模型
WHISPER_LANGUAGE=zh \
WHISPER_MODEL=~/whisper.cpp/models/ggml-large-v3.bin \
python3 transcribe.py
```

如需永久生效，在 `~/.zshrc` 中添加：

```bash
export WHISPER_LANGUAGE=zh
export WHISPER_THREADS=6
```

若需修改监听目录、输出目录等路径，直接编辑 [transcribe.py](file://./transcribe.py) 顶部的路径常量：

```python
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
INBOX_DIR      = os.path.join(BASE_DIR, "inbox")
ARCHIVE_DIR    = os.path.join(BASE_DIR, "archive")
FAILED_DIR     = os.path.join(BASE_DIR, "failed")
```

---

## 🖥️ 运行时交互

| 操作 | 效果 |
|------|------|
| 放入音频文件至 `recordings/` | 自动触发转录（轮询间隔 3 秒） |
| 按 `ESC` 键 | 等待当前文件处理完成后安全退出 |
| 按 `Ctrl+C` | 立即强制退出（当前文件保留在 `recordings/`，不归档） |

---

## 🔗 与审计流程串联

`transcribe.py` 的 txt 输出默认写入 `inbox/`，`app.py` 同样监听此目录——两个脚本**分开运行**即可自动串联：

```bash
# 终端 1：启动转录工具
python3 transcribe.py

# 终端 2：启动审计工具
python app.py
```

```
录音文件放入 recordings/
        │
        ▼ (transcribe.py)
  inbox/*.txt
        │
        ▼ (app.py)
  output/*.csv + output/*.md
```

---

## 📦 whisper.cpp 构建说明

```bash
cd ~/whisper.cpp
cmake -B build -DGGML_METAL=ON        # 开启 Metal GPU 加速（Apple Silicon）
cmake --build build --config Release -j$(sysctl -n hw.logicalcpu)

# 创建全局 symlink（可选）
sudo ln -sf ~/whisper.cpp/build/bin/whisper-cli /usr/local/bin/whisper
```

### 模型下载

```bash
cd ~/whisper.cpp

bash models/download-ggml-model.sh medium    # 推荐，约 1.5 GB
bash models/download-ggml-model.sh small     # 较快，约 466 MB
bash models/download-ggml-model.sh large-v3  # 最佳质量，约 3 GB
```

| 模型 | 大小 | 速度 | 中文准确率 | 推荐场景 |
|------|------|------|------------|----------|
| tiny | 75 MB | 极快 | 一般 | 快速预览 |
| base | 142 MB | 快 | 尚可 | 简短录音 |
| small | 466 MB | 较快 | 良好 | 日常使用 |
| **medium** | **1.5 GB** | **适中** | **优秀** | **推荐（默认）** |
| large-v3 | 3 GB | 慢 | 最佳 | 高精度场景 |

---

## ⚠️ 常见问题

**Q：报 `dyld: Library not loaded: libwhisper.1.dylib`**  
A：脚本已自动设置 `DYLD_LIBRARY_PATH` 绕过此问题，正常情况下无需手动处理。

**Q：文件被移入了 `failed/` 目录**  
A：通常是格式转换失败或 whisper 转录异常。可将文件移回 `recordings/` 重试，或用 `ffprobe` 检查文件是否损坏：
```bash
ffprobe your_audio.m4a
```

**Q：中文识别混入英文或出现乱码**  
A：设置环境变量 `WHISPER_LANGUAGE=zh` 显式指定中文。

**Q：转录速度慢**  
A：首次加载模型需要时间。可尝试 `small` 模型或增大 `WHISPER_THREADS`。Apple Silicon 用户确认构建时开启了 `-DGGML_METAL=ON`。
