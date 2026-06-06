#!/usr/bin/env python3
"""
transcribe.py — 离线音频自动转文字工具

使用 watchdog 监听 recordings/ 目录，发现音频文件后自动转录：
  - 支持 dotenv 环境变量配置
  - 使用 ffmpeg 处理格式兼容性（非原生格式自动转为 16kHz WAV）
  - 使用 whisper.cpp 进行本地语音识别
  - 转录成功：txt 文件输出至 inbox/，音频归档至 archive/
  - 转录失败：音频移至 failed/ 隔离，等待人工处置
"""

import os
import queue
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty
import json
import logging
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ──────────────────────────────────────────────
# 初始化环境与日志
# ──────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
INBOX_DIR      = os.path.join(BASE_DIR, "inbox")
ARCHIVE_DIR    = os.path.join(BASE_DIR, "archive")
FAILED_DIR     = os.path.join(BASE_DIR, "failed")

# ──────────────────────────────────────────────
# whisper.cpp 配置（支持环境配置与自动寻址）
# ──────────────────────────────────────────────
WHISPER_CLI = os.getenv(
    "WHISPER_CLI",
    shutil.which("whisper-cli") or os.path.expanduser("~/whisper.cpp/build/bin/whisper-cli")
)
WHISPER_LIB_DIR = os.getenv(
    "WHISPER_LIB_DIR",
    os.path.expanduser("~/whisper.cpp/build/src")
)
DEFAULT_MODEL = os.getenv(
    "WHISPER_MODEL",
    os.path.expanduser("~/whisper.cpp/models/ggml-medium.bin")
)
DEFAULT_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "auto")
DEFAULT_THREADS  = int(os.getenv("WHISPER_THREADS", str(os.cpu_count() or 4)))

# ──────────────────────────────────────────────
# 格式与事件队列
# ──────────────────────────────────────────────
WHISPER_NATIVE_FORMATS = {"flac", "mp3", "ogg", "wav"}

AUDIO_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg",           # whisper 原生
    ".m4a", ".aac", ".wma", ".opus", ".webm",  # 常见录音格式
    ".mp4", ".mkv", ".avi", ".mov",            # 视频容器（含音轨）
    ".amr", ".3gp",                            # 手机录音常见格式
}

POLL_INTERVAL = 0.5  # 空闲键盘检测间隔（秒）

# 待处理音频队列
file_queue = queue.Queue()

# ──────────────────────────────────────────────
# 目录与依赖初始化
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
        logging.error("缺少以下依赖，请先安装或在 .env 中正确配置：")
        for dep in missing:
            logging.error(f"   - {dep}")
        sys.exit(1)

    if not os.path.isfile(DEFAULT_MODEL):
        logging.error(f"模型文件不存在: {DEFAULT_MODEL}")
        logging.error("请运行以下命令下载模型，或在 .env 的 WHISPER_MODEL 中指定正确路径：")
        logging.error("  cd ~/whisper.cpp && bash models/download-ggml-model.sh medium")
        sys.exit(1)


# ──────────────────────────────────────────────
# 音频处理与等待逻辑
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
    except Exception as e:
        logging.debug(f"获取音频时长失败 {file_path}: {e}")
    return None


def wait_for_file_ready(file_path, timeout=10, stable_interval=1.0):
    """
    等待文件写入完成。如果在 stable_interval 内文件大小没有变化，且能够打开，则认为写入完成。
    """
    if not os.path.exists(file_path):
        return False
    start_time = time.time()
    last_size = -1
    while time.time() - start_time < timeout:
        try:
            current_size = os.path.getsize(file_path)
            if current_size == last_size and current_size > 0:
                # 尝试以读写追加模式打开文件，确认没有写锁占用
                with open(file_path, 'ab'):
                    pass
                return True
            last_size = current_size
        except Exception:
            pass
        time.sleep(stable_interval)
    return False


def convert_to_wav(input_path, tmp_dir):
    """
    使用 ffmpeg 将音频转换为 16kHz 单声道 WAV 格式。
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

    logging.info(f"格式转换中: {os.path.basename(input_path)} → WAV (16kHz mono)")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 转换失败:\n{result.stderr.strip()}")

    return output_path


def run_whisper(audio_path, output_base):
    """
    调用 whisper-cli 转录，输出纯文本文件。
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

    env = os.environ.copy()
    existing_lib = env.get("DYLD_LIBRARY_PATH", "")
    env["DYLD_LIBRARY_PATH"] = (
        f"{WHISPER_LIB_DIR}:{existing_lib}" if existing_lib else WHISPER_LIB_DIR
    )

    logging.info(
        f"Whisper 转录中 (模型: {os.path.basename(DEFAULT_MODEL)}, "
        f"语言: {DEFAULT_LANGUAGE}, 线程: {DEFAULT_THREADS})..."
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


def safe_move(src_path, dest_dir):
    """
    将文件安全移动至目标目录，自动追加时间戳后缀以防止覆盖。
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


def process_audio_file(file_path):
    """
    执行单个音频文件的转录。
    成功 → txt 输出至 inbox/，音频归档至 archive/
    失败 → 音频移至 failed/ 隔离
    """
    if not os.path.exists(file_path):
        return False

    stem = os.path.splitext(os.path.basename(file_path))[0]
    timestamp = time.strftime("%Y-%m-%d_%H_%M")
    output_name = f"{stem}_{timestamp}"

    logging.info(f"开始处理音频文件: {os.path.basename(file_path)}")

    duration = get_audio_duration(file_path)
    if duration:
        logging.info(f"音频时长: {format_duration(duration)}")

    tmp_dir = None
    try:
        if is_native_format(file_path):
            logging.info("格式原生支持，无需转换")
            audio_to_process = file_path
        else:
            tmp_dir = tempfile.mkdtemp(prefix="transcribe_")
            audio_to_process = convert_to_wav(file_path, tmp_dir)

        output_base = os.path.join(INBOX_DIR, output_name)
        txt_file, elapsed = run_whisper(audio_to_process, output_base)

        file_size = os.path.getsize(txt_file)
        speed_ratio = (
            f" ({duration / elapsed:.1f}x 实时速度)" if duration and elapsed > 0 else ""
        )
        logging.info(f"转录成功！耗时: {format_duration(elapsed)}{speed_ratio}")
        logging.info(f"输出文件: {os.path.basename(txt_file)} ({file_size:,} 字节)")

        archived = safe_move(file_path, ARCHIVE_DIR)
        logging.info(f"音频已安全归档: archive/{os.path.basename(archived)}")
        return True

    except Exception as e:
        logging.exception(f"处理音频时发生异常: {e}")
        try:
            isolated = safe_move(file_path, FAILED_DIR)
            logging.warning(f"音频已隔离至: failed/{os.path.basename(isolated)}")
        except Exception as move_err:
            logging.error(f"无法移动至 failed/ 隔离区: {move_err}")
        return False

    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ──────────────────────────────────────────────
# Watchdog 文件事件监听
# ──────────────────────────────────────────────
class AudioFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            self._handle_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle_path(event.dest_path)

    def _handle_path(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in AUDIO_EXTENSIONS:
            # 避免对同一文件重复排队
            logging.info(f"检测到新音频文件: {os.path.basename(path)}")
            file_queue.put(path)


# ──────────────────────────────────────────────
# 键盘监听（非阻塞 ESC）
# ──────────────────────────────────────────────
def check_exit_or_sleep(timeout=POLL_INTERVAL):
    """
    非阻塞检测键盘输入，如果按下 ESC 则返回 True。
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
                    r_next, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if not r_next:
                        return True
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
# 主入口
# ──────────────────────────────────────────────
def main():
    setup_dirs()
    check_dependencies()

    print("==========================================================")
    print("🎙️  离线音频转文字工具已就绪 (Event-Driven Edition)")
    print(f"   模型   : {os.path.basename(DEFAULT_MODEL)}")
    print(f"   语言   : {DEFAULT_LANGUAGE}")
    print(f"   线程   : {DEFAULT_THREADS}")
    print(f"   监听   : recordings/")
    print(f"   输出   : inbox/  （txt 文件）")
    print("==========================================================")
    print("按 ESC 键安全退出，按 Ctrl+C 强制退出。\n")

    # 1. 扫描启动时已存在的文件并加入队列
    startup_files = sorted(
        os.path.join(RECORDINGS_DIR, f)
        for f in os.listdir(RECORDINGS_DIR)
        if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
    )
    if startup_files:
        logging.info(f"扫描到启动时已存在的音频文件 {len(startup_files)} 个，加入待处理队列。")
        for f in startup_files:
            file_queue.put(f)

    # 2. 启动 watchdog 监听 recordings 目录
    event_handler = AudioFileHandler()
    observer = Observer()
    observer.schedule(event_handler, path=RECORDINGS_DIR, recursive=False)
    observer.start()

    try:
        while True:
            # 如果队列中有文件，提取并处理
            if not file_queue.empty():
                file_path = file_queue.get()
                if os.path.exists(file_path):
                    # 等待写入稳定
                    logging.info(f"等待文件写入完成... {os.path.basename(file_path)}")
                    if wait_for_file_ready(file_path):
                        process_audio_file(file_path)
                    else:
                        logging.warning(f"文件写入不稳定，跳过处理: {file_path}")
                file_queue.task_done()

                # 每处理完一个文件，检查一次退出按键
                if check_exit_or_sleep(0.1):
                    logging.info("检测到 ESC 键，正在退出...")
                    break
            else:
                # 队列空闲时轮询检测 ESC
                if check_exit_or_sleep(POLL_INTERVAL):
                    logging.info("检测到 ESC 键，正在退出...")
                    break
    except KeyboardInterrupt:
        logging.info("收到 Ctrl+C 信号，强制退出。")
    finally:
        observer.stop()
        observer.join()
        logging.info("转录监视器已安全关闭。")


if __name__ == "__main__":
    main()
