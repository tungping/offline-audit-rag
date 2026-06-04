# 🎙️ transcribe.py — 离线音频自动转文字工具

> **100% 离线、零成本**的本地语音转文字工具。  
> 使用 ffmpeg 自动处理格式兼容性，调用 whisper.cpp 进行高质量语音识别，输出纯文本文件。  
> 可直接与 `app.py` 审计流程串联，实现 **音频录音 → 转文字 → 自动合规审计** 的完整端到端工作流。

---

## ✨ 功能特性

- 🔒 **完全离线**：所有处理均在本地完成，音频数据不上传任何云端
- 🔄 **智能格式处理**：自动检测音频格式，兼容格式直接处理，不兼容格式通过 ffmpeg 自动转换
- 📁 **批量处理**：支持单文件或整个目录的批量转录
- 🌐 **多语言支持**：支持中文、英文、日文等 90+ 种语言，或开启自动语言检测
- ⏱️ **进度可视化**：显示音频时长、转录耗时与实时速度比
- 🧹 **自动清理**：转换产生的临时文件在处理后自动删除
- 🔗 **流水线集成**：默认输出到 `inbox/`，可直接触发 `app.py` 的合规审计流程

---

## 🏗️ 工作流程

```
音频输入 (任意格式)
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
  ggml-medium 模型
        │
        ▼
   output/*.txt
```

| 组件 | 说明 |
|------|------|
| **ffprobe** | 探测输入文件的编解码器与时长信息 |
| **ffmpeg** | 将非兼容格式转换为 16kHz 单声道 PCM WAV |
| **whisper-cli** | 基于 whisper.cpp 的本地语音识别引擎 |
| **ggml-medium.bin** | 默认使用的 Whisper medium 模型，中文识别效果好 |

---

## 🚀 快速开始

### 前置要求

| 依赖 | 安装方式 | 验证命令 |
|------|----------|----------|
| ffmpeg + ffprobe | `brew install ffmpeg` | `ffmpeg -version` |
| whisper.cpp | 参考 [构建指南](#whisper-cpp-构建说明) | 见下文 |
| whisper 模型 | 参考 [模型下载](#模型下载) | 见下文 |

### 基本用法

```bash
# 直接使用项目内的 Python 环境
.venv/bin/python transcribe.py <音频文件或目录>

# 或使用系统 Python3（脚本无额外依赖，仅用标准库）
python3 transcribe.py <音频文件或目录>
```

---

## 📖 使用示例

```bash
# ── 单文件转录（输出到 inbox/，自动触发审计流程）──
python3 transcribe.py meeting.m4a

# ── 指定中文，提升识别准确率 ──
python3 transcribe.py meeting.mp3 --language zh

# ── 批量处理整个录音目录 ──
python3 transcribe.py ./recordings/

# ── 自定义输出目录（不触发审计）──
python3 transcribe.py call.wav --output-dir ./transcripts/

# ── 使用更小的模型以节省时间 ──
python3 transcribe.py short_clip.mp3 \
    --model /Users/tenan/whisper.cpp/models/ggml-small.bin

# ── 通过环境变量设置默认行为 ──
WHISPER_LANGUAGE=zh WHISPER_THREADS=4 python3 transcribe.py ./recordings/
```

---

## ⚙️ 参数说明

```
用法: python3 transcribe.py <input> [选项]

位置参数:
  input               音频文件路径，或包含音频文件的目录路径

选项:
  --output-dir DIR    转录结果输出目录
                      默认: inbox/（直接触发 app.py 审计流程）

  --language LANG     音频语言代码
                      常用: zh（中文）| en（英文）| ja（日文）| auto（自动检测）
                      默认: auto
                      环境变量: WHISPER_LANGUAGE

  --model PATH        whisper.cpp 模型文件路径（.bin 格式）
                      默认: /Users/tenan/whisper.cpp/models/ggml-medium.bin
                      环境变量: WHISPER_MODEL

  --threads N         转录时使用的 CPU 线程数
                      默认: 当前机器的 CPU 核心数
                      环境变量: WHISPER_THREADS

  -h, --help          显示帮助信息
```

### 环境变量

可在 shell 配置文件（如 `~/.zshrc`）中预设常用选项：

```bash
export WHISPER_LANGUAGE=zh       # 固定中文，省去每次指定
export WHISPER_MODEL=/Users/tenan/whisper.cpp/models/ggml-large-v3.bin
export WHISPER_THREADS=6
```

---

## 🎵 支持的音频格式

| 类别 | 格式 | 说明 |
|------|------|------|
| **原生支持**（直接处理） | `.wav` `.mp3` `.flac` `.ogg` | whisper.cpp 内置解码，无需转换 |
| **自动转换** | `.m4a` `.aac` `.wma` `.opus` `.webm` | 通过 ffmpeg 转为 WAV 后处理 |
| **手机录音** | `.amr` `.3gp` | 通过 ffmpeg 转为 WAV 后处理 |
| **视频容器** | `.mp4` `.mkv` `.avi` `.mov` | 提取音频轨道后处理 |

> 转换统一输出为 **16kHz 单声道 16-bit PCM WAV**，这是 Whisper 模型的最佳输入格式。

---

## 📤 输出说明

- 输出文件名格式：`{原始文件名}.txt`（去除音频扩展名后加 `.txt`）
- 默认输出目录：`inbox/`
- 文件内容：纯文本，包含带时间戳的转录片段

输出示例（`meeting_2024.txt`）：
```
[00:00:00.000 --> 00:00:05.420]  大家好，今天的会议主要讨论三个议题。
[00:00:05.420 --> 00:00:12.180]  第一个是关于下季度的产品路线图...
```

---

## 🔗 与审计流程串联

```bash
# 步骤 1：转录会议录音（输出到 inbox/）
python3 transcribe.py ./recordings/2024-06-04-standup.m4a

# 步骤 2：启动审计流程（app.py 自动检测 inbox/ 中的 .txt 文件）
python app.py
```

或一行完成：

```bash
python3 transcribe.py meeting.m4a && python app.py
```

---

## 🔧 配置说明

脚本内置路径常量可根据实际环境修改（文件顶部）：

```python
# transcribe.py 顶部
WHISPER_CLI     = "/Users/tenan/whisper.cpp/build/bin/whisper-cli"
WHISPER_LIB_DIR = "/Users/tenan/whisper.cpp/build/src"
DEFAULT_MODEL   = "/Users/tenan/whisper.cpp/models/ggml-medium.bin"
```

---

## 📦 whisper.cpp 构建说明

如需重新构建 whisper.cpp（macOS Apple Silicon）：

```bash
cd ~/whisper.cpp
cmake -B build -DGGML_METAL=ON   # 开启 Metal GPU 加速
cmake --build build --config Release -j$(sysctl -n hw.logicalcpu)
```

### 模型下载

```bash
cd ~/whisper.cpp

# 下载 medium 模型（推荐，约 1.5 GB）
bash models/download-ggml-model.sh medium

# 其他可选模型（速度与质量权衡）
bash models/download-ggml-model.sh tiny    # 最快，质量一般，约 75 MB
bash models/download-ggml-model.sh base    # 较快，质量尚可，约 142 MB
bash models/download-ggml-model.sh small   # 均衡，约 466 MB
bash models/download-ggml-model.sh large-v3  # 最佳质量，约 3 GB
```

| 模型 | 大小 | 中文速度 | 中文准确率 | 推荐场景 |
|------|------|----------|------------|----------|
| tiny | 75 MB | 极快 | 一般 | 快速预览 |
| base | 142 MB | 快 | 尚可 | 简短录音 |
| small | 466 MB | 较快 | 良好 | 日常使用 |
| **medium** | **1.5 GB** | **适中** | **优秀** | **推荐（默认）** |
| large-v3 | 3 GB | 慢 | 最佳 | 高精度场景 |

---

## ⚠️ 常见问题

**Q：运行时报 `dyld: Library not loaded: libwhisper.1.dylib`**  
A：脚本已自动设置 `DYLD_LIBRARY_PATH` 绕过此问题。如仍报错，请确认 `WHISPER_LIB_DIR` 路径中存在 `libwhisper.1.dylib`：
```bash
ls ~/whisper.cpp/build/src/libwhisper*.dylib
```

**Q：转录速度很慢（如 0.1x 实时速度）**  
A：首次调用需加载模型到内存，后续批量文件会更快。可尝试改用 `small` 模型或增加 `--threads`。Apple Silicon 用户确认构建时开启了 `-DGGML_METAL=ON`。

**Q：中文识别结果混入英文或乱码**  
A：添加 `--language zh` 显式指定中文，避免自动检测误判。

**Q：视频文件处理失败**  
A：确保视频文件包含音频轨道（`ffprobe your_video.mp4` 可验证）。纯视频（无音轨）无法转录。

**Q：想一次处理多个不同路径的文件**  
A：目前单次调用只支持一个文件或一个目录。可用 shell 循环：
```bash
for f in meeting1.m4a meeting2.mp3 call.wav; do
    python3 transcribe.py "$f"
done
```
