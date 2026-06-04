#!/usr/bin/env python3
"""
transcribe.py — 离线音频自动转文字工具

使用 ffmpeg 进行音频格式预处理，whisper.cpp 进行语音识别，
输出纯文本文件。设计为与 app.py 审计流程解耦的独立工具。

用法:
    python transcribe.py <audio_file_or_dir> [options]
    python transcribe.py recording.m4a
    python transcribe.py ./recordings/ --output-dir ./inbox/ --language zh
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# ──────────────────────────────────────────────
# 常量与默认配置
# ──────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# whisper.cpp 路径（通过 symlink 反向推导出构建目录）
WHISPER_CLI = "/Users/tenan/whisper.cpp/build/bin/whisper-cli"
WHISPER_LIB_DIR = "/Users/tenan/whisper.cpp/build/src"
DEFAULT_MODEL = "/Users/tenan/whisper.cpp/models/ggml-medium.bin"

# whisper.cpp 原生支持的音频格式（无需 ffmpeg 转换）
WHISPER_NATIVE_FORMATS = {"flac", "mp3", "ogg", "wav"}

# 扫描目录时识别的音频文件扩展名
AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg",  # whisper 原生
    ".m4a", ".aac", ".wma", ".opus", ".webm",  # 常见录音格式
    ".mp4", ".mkv", ".avi", ".mov",  # 视频容器（可能含纯音频内容）
    ".amr", ".3gp",  # 手机录音常见格式
}


def check_dependencies():
    """检查 ffmpeg、ffprobe、whisper-cli 是否可用。"""
    missing = []

    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    if not shutil.which("ffprobe"):
        missing.append("ffprobe")
    if not os.path.isfile(WHISPER_CLI):
        missing.append(f"whisper-cli ({WHISPER_CLI})")

    if missing:
        print(f"❌ 缺少以下依赖: {', '.join(missing)}", file=sys.stderr)
        print("请确保 ffmpeg 和 whisper.cpp 已正确安装。", file=sys.stderr)
        sys.exit(1)


def get_audio_codec(file_path):
    """
    使用 ffprobe 探测文件的音频编解码器名称。
    返回编解码器名称字符串，失败时返回 None。
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        info = json.loads(result.stdout)
        streams = info.get("streams", [])
        if streams:
            return streams[0].get("codec_name")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        pass
    return None


def is_native_format(file_path):
    """
    判断文件是否可以直接被 whisper.cpp 读取。
    综合考虑文件扩展名和实际编解码器。
    """
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    if ext in WHISPER_NATIVE_FORMATS:
        return True

    # 扩展名不在列表中，检查实际编码是否兼容
    codec = get_audio_codec(file_path)
    if codec and codec in {"pcm_s16le", "pcm_s16be", "pcm_f32le", "flac",
                           "mp3", "vorbis", "opus"}:
        # 某些容器（如 .mkv 包裹的 flac）实际编码兼容但扩展名不在列表中
        # 保守起见仍然转换，因为 whisper.cpp 按扩展名判断格式
        pass

    return False


def convert_to_wav(input_path, tmp_dir):
    """
    使用 ffmpeg 将音频转换为 16kHz 单声道 WAV 格式（whisper 最佳输入）。
    返回转换后的临时文件路径。
    """
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(tmp_dir, f"{base_name}.wav")

    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-ar", "16000",   # 16kHz 采样率
        "-ac", "1",       # 单声道
        "-c:a", "pcm_s16le",  # 16-bit PCM
        "-y",             # 覆盖已有文件
        "-loglevel", "warning",
        output_path,
    ]

    print(f"  🔄 转换格式: {os.path.basename(input_path)} → WAV (16kHz mono)")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 转换失败:\n{result.stderr.strip()}"
        )

    return output_path


def run_whisper(audio_path, output_path, model, language, threads):
    """
    调用 whisper-cli 进行语音转录，输出纯文本文件。
    output_path 为输出文件路径（不含 .txt 扩展名，whisper 会自动追加）。
    """
    cmd = [
        WHISPER_CLI,
        "--model", model,
        "--language", language,
        "--threads", str(threads),
        "--output-txt",
        "--output-file", output_path,
        "--no-prints",
        "--file", audio_path,
    ]

    env = os.environ.copy()
    # 解决 whisper.cpp dylib 加载问题
    existing = env.get("DYLD_LIBRARY_PATH", "")
    lib_paths = WHISPER_LIB_DIR
    if existing:
        lib_paths = f"{WHISPER_LIB_DIR}:{existing}"
    env["DYLD_LIBRARY_PATH"] = lib_paths

    print(f"  🎙️  Whisper 转录中 (模型: {os.path.basename(model)}, 语言: {language})...")

    start_time = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=3600,  # 单文件最长 1 小时
    )
    elapsed = time.time() - start_time

    if result.returncode != 0:
        stderr_msg = result.stderr.strip() if result.stderr else "(无错误输出)"
        raise RuntimeError(f"whisper-cli 转录失败 (exit code {result.returncode}):\n{stderr_msg}")

    # whisper --output-txt 会生成 {output_path}.txt
    txt_file = f"{output_path}.txt"
    if not os.path.isfile(txt_file):
        raise FileNotFoundError(
            f"转录完成但未找到输出文件: {txt_file}\n"
            f"whisper stderr: {result.stderr.strip() if result.stderr else '(空)'}"
        )

    return txt_file, elapsed


def get_audio_duration(file_path):
    """获取音频时长（秒），用于进度显示。"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            duration = info.get("format", {}).get("duration")
            if duration:
                return float(duration)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass
    return None


def format_duration(seconds):
    """将秒数格式化为可读的时间字符串。"""
    if seconds is None:
        return "未知"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    elif minutes > 0:
        return f"{minutes}m{secs:02d}s"
    else:
        return f"{secs}s"


def transcribe_file(file_path, output_dir, model, language, threads):
    """
    处理单个音频文件的完整转录流程。
    返回 (success: bool, output_file: str | None)。
    """
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    print(f"\n📂 处理文件: {os.path.basename(file_path)}")

    # 获取音频时长信息
    duration = get_audio_duration(file_path)
    if duration:
        print(f"  ⏱️  音频时长: {format_duration(duration)}")

    tmp_dir = None
    audio_to_process = file_path

    try:
        # 格式检测与转换
        if is_native_format(file_path):
            print(f"  ✅ 格式兼容，无需转换")
        else:
            tmp_dir = tempfile.mkdtemp(prefix="transcribe_")
            audio_to_process = convert_to_wav(file_path, tmp_dir)

        # 构建输出路径（不含 .txt 后缀，whisper 自动追加）
        output_base = os.path.join(output_dir, base_name)

        # 调用 whisper 转录
        txt_file, elapsed = run_whisper(
            audio_to_process, output_base, model, language, threads
        )

        # 输出摘要
        file_size = os.path.getsize(txt_file)
        speed_ratio = f" ({duration / elapsed:.1f}x 实时速度)" if duration else ""
        print(f"  ✅ 转录完成! 耗时 {format_duration(elapsed)}{speed_ratio}")
        print(f"  📄 输出: {txt_file} ({file_size:,} bytes)")

        return True, txt_file

    except Exception as e:
        print(f"  ❌ 处理失败: {e}", file=sys.stderr)
        return False, None

    finally:
        # 清理临时转换文件
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def collect_audio_files(path):
    """
    收集待处理的音频文件列表。
    path 可以是单个文件或目录。
    """
    if os.path.isfile(path):
        return [path]

    if os.path.isdir(path):
        files = []
        for f in sorted(os.listdir(path)):
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                files.append(os.path.join(path, f))
        return files

    print(f"❌ 路径不存在: {path}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="离线音频自动转文字工具 (ffmpeg + whisper.cpp)",
        epilog="示例: python transcribe.py meeting.m4a --language zh",
    )
    parser.add_argument(
        "input",
        help="音频文件路径或包含音频文件的目录",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(BASE_DIR, "inbox"),
        help=f"输出目录 (默认: inbox/)",
    )
    parser.add_argument(
        "--language",
        default=os.getenv("WHISPER_LANGUAGE", "auto"),
        help="语言代码, 如 zh/en/ja/auto (默认: auto)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("WHISPER_MODEL", DEFAULT_MODEL),
        help=f"whisper 模型路径 (默认: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=int(os.getenv("WHISPER_THREADS", str(os.cpu_count() or 4))),
        help=f"线程数 (默认: {os.cpu_count() or 4})",
    )

    args = parser.parse_args()

    # 前置检查
    check_dependencies()

    if not os.path.isfile(args.model):
        print(f"❌ 模型文件不存在: {args.model}", file=sys.stderr)
        print("请运行以下命令下载模型:", file=sys.stderr)
        print(f"  cd /Users/tenan/whisper.cpp && bash models/download-ggml-model.sh medium", file=sys.stderr)
        sys.exit(1)

    # 确保输出目录存在
    os.makedirs(args.output_dir, exist_ok=True)

    # 收集音频文件
    audio_files = collect_audio_files(args.input)
    if not audio_files:
        print("⚠️  未找到任何音频文件。", file=sys.stderr)
        print(f"支持的格式: {', '.join(sorted(AUDIO_EXTENSIONS))}", file=sys.stderr)
        sys.exit(1)

    # 批量处理
    total = len(audio_files)
    print(f"🎤 共发现 {total} 个音频文件，开始转录...")
    print(f"   模型: {os.path.basename(args.model)}")
    print(f"   语言: {args.language}")
    print(f"   线程: {args.threads}")
    print(f"   输出: {args.output_dir}")

    success_count = 0
    failed_count = 0
    total_elapsed = time.time()

    for idx, file_path in enumerate(audio_files):
        if total > 1:
            print(f"\n{'─' * 50}")
            print(f"[{idx + 1}/{total}]")

        ok, _ = transcribe_file(
            file_path, args.output_dir, args.model, args.language, args.threads
        )
        if ok:
            success_count += 1
        else:
            failed_count += 1

    # 最终汇总
    total_time = time.time() - total_elapsed
    print(f"\n{'═' * 50}")
    print(f"🏁 全部完成! 总耗时: {format_duration(total_time)}")
    print(f"   ✅ 成功: {success_count}")
    if failed_count:
        print(f"   ❌ 失败: {failed_count}")
    print(f"   📁 输出目录: {args.output_dir}")


if __name__ == "__main__":
    main()
