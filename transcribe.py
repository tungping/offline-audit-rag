#!/usr/bin/env python3
"""
transcribe.py — 离线音频自动转文字工具

持续监听 recordings/ 目录，发现音频文件后自动转录：
  - 使用 ffmpeg 处理格式兼容性（非原生格式自动转为 16kHz WAV）
  - 使用 whisper.cpp 进行本地语音识别
  - 转录成功：txt 文件输出至 inbox/，音频归档至 archive/
  - 转录失败：音频移至 failed/ 隔离，等待人工处置

输出文件命名：{原始文件名}_{YYYY-MM-DD_HH_MM}.txt
"""

import os
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty
import json

# ──────────────────────────────────────────────
# 路径配置
# ──────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
INBOX_DIR      = os.path.join(BASE_DIR, "inbox")
ARCHIVE_DIR    = os.path.join(BASE_DIR, "archive")
FAILED_DIR     = os.path.join(BASE_DIR, "failed")

# ──────────────────────────────────────────────
# whisper.cpp 配置（根据实际安装路径调整）
# ──────────────────────────────────────────────

WHISPER_CLI     = "/Users/tenan/whisper.cpp/build/bin/whisper-cli"
WHISPER_LIB_DIR = "/Users/tenan/whisper.cpp/build/src"
DEFAULT_MODEL   = os.getenv(
    "WHISPER_MODEL",
    "/Users/tenan/whisper.cpp/models/ggml-medium.bin"
)
DEFAULT_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "auto")
DEFAULT_THREADS  = int(os.getenv("WHISPER_THREADS", str(os.cpu_count() or 4)))

# ──────────────────────────────────────────────
# 格式常量
# ──────────────────────────────────────────────

# whisper.cpp 可直接读取的格式（无需 ffmpeg 转换）
WHISPER_NATIVE_FORMATS = {"flac", "mp3", "ogg", "wav"}

# 扫描 recordings/ 时识别的音频文件扩展名
AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg",           # whisper 原生
    ".m4a", ".aac", ".wma", ".opus", ".webm",  # 常见录音格式
    ".mp4", ".mkv", ".avi", ".mov",            # 视频容器（含音轨）
    ".amr", ".3gp",                            # 手机录音常见格式
}

POLL_INTERVAL = 3.0  # 轮询间隔（秒）


# ──────────────────────────────────────────────
# 环境初始化
# ──────────────────────────────────────────────

def setup_dirs():
    """创建所有必需目录。"""
    for d in [RECORDINGS_DIR, INBOX_DIR, ARCHIVE_DIR, FAILED_DIR]:
        os.makedirs(d, exist_ok=True)


def check_dependencies():
    """启动前检查 ffmpeg、ffprobe、whisper-cli 是否可用。"""
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg（brew install ffmpeg）")
    if not shutil.which("ffprobe"):
        missing.append("ffprobe（随 ffmpeg 一起安装）")
    if not os.path.isfile(WHISPER_CLI):
        missing.append(f"whisper-cli（路径: {WHISPER_CLI}）")
    if missing:
        print("❌ 缺少以下依赖，请先安装后再运行：", file=sys.stderr)
        for dep in missing:
            print(f"   - {dep}", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(DEFAULT_MODEL):
        print(f"❌ 模型文件不存在: {DEFAULT_MODEL}", file=sys.stderr)
        print("请运行以下命令下载模型：", file=sys.stderr)
        print("  cd ~/whisper.cpp && bash models/download-ggml-model.sh medium", file=sys.stderr)
        sys.exit(1)


# ──────────────────────────────────────────────
# 音频处理
# ──────────────────────────────────────────────

def is_native_format(file_path):
    """判断文件扩展名是否为 whisper.cpp 原生支持格式。"""
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    return ext in WHISPER_NATIVE_FORMATS


def get_audio_duration(file_path):
    """用 ffprobe 获取音频时长（秒），获取失败返回 None。"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "json",
                file_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            duration = info.get("format", {}).get("duration")
            if duration:
                return float(duration)
    except Exception:
        pass
    return None


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
        "-ar", "16000",       # 16kHz 采样率
        "-ac", "1",           # 单声道
        "-c:a", "pcm_s16le",  # 16-bit PCM
        "-y",                 # 覆盖已有文件
        "-loglevel", "warning",
        output_path,
    ]

    print(f"  🔄 格式转换: {os.path.basename(input_path)} → WAV (16kHz mono)")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 转换失败:\n{result.stderr.strip()}")

    return output_path


def run_whisper(audio_path, output_base):
    """
    调用 whisper-cli 转录，输出纯文本文件。
    output_base 为输出路径（不含 .txt 后缀，whisper 自动追加）。
    返回 (txt_file_path, elapsed_seconds)。
    """
    cmd = [
        WHISPER_CLI,
        "--model",       DEFAULT_MODEL,
        "--language",    DEFAULT_LANGUAGE,
        "--threads",     str(DEFAULT_THREADS),
        "--output-txt",
        "--output-file", output_base,
        "--no-prints",
        "--file",        audio_path,
    ]

    # 设置 DYLD_LIBRARY_PATH 解决 whisper.cpp dylib rpath 问题
    env = os.environ.copy()
    existing_lib = env.get("DYLD_LIBRARY_PATH", "")
    env["DYLD_LIBRARY_PATH"] = (
        f"{WHISPER_LIB_DIR}:{existing_lib}" if existing_lib else WHISPER_LIB_DIR
    )

    print(
        f"  🎙️  Whisper 转录中 "
        f"(模型: {os.path.basename(DEFAULT_MODEL)}, 语言: {DEFAULT_LANGUAGE}, "
        f"线程: {DEFAULT_THREADS})..."
    )

    start = time.time()
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=3600,
    )
    elapsed = time.time() - start

    if result.returncode != 0:
        stderr_msg = result.stderr.strip() or "(无错误输出)"
        raise RuntimeError(
            f"whisper-cli 失败 (exit {result.returncode}):\n{stderr_msg}"
        )

    txt_file = f"{output_base}.txt"
    if not os.path.isfile(txt_file):
        raise FileNotFoundError(
            f"转录完成但未找到输出文件: {txt_file}\n"
            f"stderr: {result.stderr.strip() or '(空)'}"
        )

    return txt_file, elapsed


# ──────────────────────────────────────────────
# 文件管理
# ──────────────────────────────────────────────

def safe_move(src_path, dest_dir):
    """
    将文件安全移动至目标目录。
    若目标已有同名文件，自动追加时间戳后缀，避免静默覆盖。
    """
    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_dir, filename)

    if os.path.exists(dest_path):
        base, ext = os.path.splitext(filename)
        suffix = time.strftime("%Y%m%d_%H%M%S")
        dest_path = os.path.join(dest_dir, f"{base}_{suffix}{ext}")
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base}_{suffix}_{counter}{ext}")
            counter += 1

    shutil.move(src_path, dest_path)
    return dest_path


def format_duration(seconds):
    """将秒数格式化为可读字符串。"""
    if seconds is None:
        return "未知"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    elif m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ──────────────────────────────────────────────
# 单文件处理
# ──────────────────────────────────────────────

def process_audio_file(file_path):
    """
    对单个音频文件执行完整转录流程。
      成功 → txt 输出至 inbox/，音频归档至 archive/
      失败 → 音频移至 failed/ 隔离
    返回 True 表示成功，False 表示失败。
    """
    stem = os.path.splitext(os.path.basename(file_path))[0]
    timestamp = time.strftime("%Y-%m-%d_%H_%M")
    output_name = f"{stem}_{timestamp}"   # whisper 会追加 .txt

    print(f"\n📂 处理文件: {os.path.basename(file_path)}")

    # 显示音频时长
    duration = get_audio_duration(file_path)
    if duration:
        print(f"  ⏱️  音频时长: {format_duration(duration)}")

    tmp_dir = None
    try:
        # 1. 格式检测与转换
        if is_native_format(file_path):
            print("  ✅ 格式兼容，无需转换")
            audio_to_process = file_path
        else:
            tmp_dir = tempfile.mkdtemp(prefix="transcribe_")
            audio_to_process = convert_to_wav(file_path, tmp_dir)

        # 2. 转录（输出到 inbox/）
        output_base = os.path.join(INBOX_DIR, output_name)
        txt_file, elapsed = run_whisper(audio_to_process, output_base)

        # 3. 输出摘要
        file_size = os.path.getsize(txt_file)
        speed_ratio = (
            f" ({duration / elapsed:.1f}x 实时速度)" if duration and elapsed > 0 else ""
        )
        print(f"  ✅ 转录完成！耗时 {format_duration(elapsed)}{speed_ratio}")
        print(f"  📄 输出: {os.path.basename(txt_file)} ({file_size:,} bytes)")

        # 4. 音频归档
        archived = safe_move(file_path, ARCHIVE_DIR)
        print(f"  🗄️  音频已归档: archive/{os.path.basename(archived)}")

        return True

    except Exception as e:
        print(f"  ❌ 处理失败: {e}", file=sys.stderr)
        # 失败时将音频移至 failed/ 隔离
        try:
            isolated = safe_move(file_path, FAILED_DIR)
            print(f"  ⚠️  音频已隔离: failed/{os.path.basename(isolated)}", file=sys.stderr)
        except Exception as move_err:
            print(f"  ⚠️  无法移动至 failed/: {move_err}", file=sys.stderr)
        return False

    finally:
        # 清理临时转换文件
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# 键盘监听（ESC 退出，与 app.py 保持一致）
# ──────────────────────────────────────────────

def check_exit_or_sleep(timeout=POLL_INTERVAL):
    """
    非阻塞等待：在 timeout 秒内轮询 stdin。
    检测到 ESC 键返回 True，否则返回 False。
    非 TTY 环境（如管道/cron）直接 sleep。
    """
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        time.sleep(timeout)
        return False

    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        start = time.time()
        while time.time() - start < timeout:
            rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
            if rlist:
                ch = sys.stdin.read(1)
                if ch == '\x1b':  # ESC
                    # 区分 ESC 单键与 ANSI 转义序列（方向键等）
                    r_next, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if not r_next:
                        return True
                    # 消费掉后续 ANSI 序列字符
                    while True:
                        rn, _, _ = select.select([sys.stdin], [], [], 0.01)
                        if rn:
                            sys.stdin.read(1)
                        else:
                            break
            time.sleep(0.05)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return False


# ──────────────────────────────────────────────
# 主循环
# ──────────────────────────────────────────────

def main():
    setup_dirs()
    check_dependencies()

    print("🎙️  离线音频转文字工具已就绪")
    print(f"   模型   : {os.path.basename(DEFAULT_MODEL)}")
    print(f"   语言   : {DEFAULT_LANGUAGE}")
    print(f"   线程   : {DEFAULT_THREADS}")
    print(f"   监听   : recordings/")
    print(f"   输出   : inbox/  （txt 文件）")
    print(f"   归档   : archive/（处理成功的音频）")
    print(f"   隔离   : failed/ （处理失败的音频）")
    print("\n将音频文件放入 recordings/ 目录即可自动触发转录。")
    print("按 ESC 键安全退出，按 Ctrl+C 强制退出。\n")

    while True:
        try:
            # 按文件名排序，保证处理顺序稳定
            audio_files = sorted(
                f for f in os.listdir(RECORDINGS_DIR)
                if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
            )

            if audio_files:
                total = len(audio_files)
                success_count = 0
                failed_count = 0

                for idx, filename in enumerate(audio_files):
                    file_path = os.path.join(RECORDINGS_DIR, filename)

                    if total > 1:
                        print(f"{'─' * 52}")
                        print(f"[{idx + 1}/{total}] {filename}")

                    if process_audio_file(file_path):
                        success_count += 1
                    else:
                        failed_count += 1

                    # 每处理完一个文件都检查一次退出信号
                    if check_exit_or_sleep(0.1):
                        print("\n检测到 ESC 键，当前文件已处理完成，安全退出。")
                        sys.exit(0)

                print(f"\n{'═' * 52}")
                print(
                    f"当前批次完成：✅ 成功 {success_count} 个"
                    + (f"，❌ 失败 {failed_count} 个" if failed_count else "")
                )
                print("继续监听 recordings/ 目录，或按 ESC 键退出...\n")

            # 空闲时等待，同时监听 ESC
            if check_exit_or_sleep(POLL_INTERVAL):
                print("\n检测到 ESC 键，转录工具已安全退出。")
                break

        except KeyboardInterrupt:
            print("\n\n转录工具已被用户终止（Ctrl+C）。")
            break
        except Exception as e:
            print(f"\n轮询异常: {e}", file=sys.stderr)
            if check_exit_or_sleep(POLL_INTERVAL):
                break


if __name__ == "__main__":
    main()
