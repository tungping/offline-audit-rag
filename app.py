import argparse
import logging
import os
import queue
import sys
import threading
import time

import ollama
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from audit_core.config import (
    ARCHIVE,
    AUDIT_HISTORY_FILENAME,
    AUDIT_INPUT_TOKEN_LIMIT,
    AUDIT_MODEL,
    BASE_DIR,
    COMPLIANCE_MODE,
    CONFIG_DIR,
    EMBEDDING_CONCURRENCY,
    EMBEDDING_MAX_RETRIES,
    EMBED_MODEL,
    FAILED,
    INBOX,
    OUTPUT,
    POLL_INTERVAL,
    RELEVANCE_THRESHOLD,
    SEMICONDUCTOR_IP_CONFIG_DIR,
    SEMICONDUCTOR_IP_INPUT_TOKEN_LIMIT,
    SEMICONDUCTOR_IP_MODE,
    SEMICONDUCTOR_IP_NUM_PREDICT,
    SUPPORTED_AUDIT_MODES,
    VECTOR_STORE_DIR,
    collection_name_for_mode,
    generation_options_for_mode,
    input_token_limit_for_mode,
    normalize_audit_mode,
    rules_dir_for_mode,
)
from audit_core.file_ops import safe_move, unique_file_path
from audit_core.formatting import (
    _choice,
    _clean_text,
    _truthy,
    extract_json_object,
    markdown_quote_block,
    markdown_table_cell,
    mask_dataframe_text_columns,
    mask_markdown_text,
    mask_output_basename,
)
from audit_core.models import ProcessResult
from audit_core.pipeline import process_file, process_file_with_result
from audit_core.knowledge_base import (
    initialize_knowledge_base,
    rebuild_knowledge_base,
    retrieve_relevant_context,
)
from audit_core.model_io import check_environment, check_ollama_status
from audit_core.history import record_audit_history
from audit_core.text_processing import (
    bound_audit_prompt_content,
    count_tokens,
    recursive_split_text,
)
from capabilities.patent_research.legacy_analysis import (
    SEMICONDUCTOR_CLAIM_COLUMNS,
    SEMICONDUCTOR_IP_DISCLAIMER,
    SEMICONDUCTOR_RISK_COLUMNS,
    build_semiconductor_ip_system_prompt,
    render_semiconductor_ip_report,
    validate_semiconductor_ip_result,
    write_semiconductor_ip_outputs,
)
from capabilities.meeting_audit.legacy import SYSTEM_PROMPT_TEMPLATE

# ──────────────────────────────────────────────
# 环境初始化与日志
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 事件循环与队列
file_queue = queue.Queue()
_queued_paths: set[str] = set()
_queue_lock = threading.Lock()

# 严密的 SYSTEM PROMPT 模板
def wait_for_file_ready(file_path, timeout=5, stable_interval=0.5):
    """
    等待文件写入完成：如果在 stable_interval 内文件大小没有变化，则认为写入完成。
    """
    if not os.path.exists(file_path):
        return False
    start_time = time.time()
    last_size = -1
    while time.time() - start_time < timeout:
        try:
            current_size = os.path.getsize(file_path)
            if current_size == last_size and current_size > 0:
                return True
            last_size = current_size
        except Exception:
            pass
        time.sleep(stable_interval)
    return False


def check_exit_or_sleep(timeout=POLL_INTERVAL):
    """
    非阻塞检查用户是否按下了 ESC 键。
    """
    fd = sys.stdin.fileno()
    is_tty = os.isatty(fd)

    if not is_tty:
        time.sleep(timeout)
        return False

    try:
        import select
        import termios
        import tty
    except ImportError:
        time.sleep(timeout)
        return False

    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        start_time = time.time()
        while time.time() - start_time < timeout:
            rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
            if rlist:
                ch = sys.stdin.read(1)
                if ch == "\x1b":  # ESC 键
                    r_next, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if not r_next:
                        return True
                    else:
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


# Watchdog 文件夹事件监听
class InboxFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            self._handle_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle_path(event.dest_path)

    def _handle_path(self, path):
        if path.endswith(".txt"):
            abs_path = os.path.abspath(path)
            with _queue_lock:
                if abs_path in _queued_paths:
                    logging.debug(f"跳过重复文本事件: {os.path.basename(path)}")
                    return
                _queued_paths.add(abs_path)
            logging.info(f"检测到新待审文本: {os.path.basename(path)}")
            file_queue.put(abs_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Offline Auto Audit.")
    parser.add_argument(
        "--mode",
        choices=sorted(SUPPORTED_AUDIT_MODES),
        default=os.getenv("AUDIT_MODE", COMPLIANCE_MODE),
        help="Analysis mode. Defaults to AUDIT_MODE or compliance.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    audit_mode = normalize_audit_mode(args.mode)
    check_environment()
    
    ollama_status = check_ollama_status()
    if not ollama_status["connected"]:
        logging.error(f"❌ 无法连接到 Ollama 服务，请确认 Ollama 正在运行！错误: {ollama_status['error']}")
    else:
        if not ollama_status["audit_model_ok"]:
            logging.warning(f"⚠️ 审计模型 {AUDIT_MODEL} 未在 Ollama 中下载，运行可能会报错，建议运行 'ollama pull {AUDIT_MODEL}'。")
        if not ollama_status["embed_model_ok"]:
            logging.warning(f"⚠️ 向量模型 {EMBED_MODEL} 未在 Ollama 中下载，运行可能会报错，建议运行 'ollama pull {EMBED_MODEL}'。")

    logging.info("正在连接本地向量库与初始化知识库...")
    collection = initialize_knowledge_base(audit_mode)

    logging.info("==========================================================")
    logging.info("🚀  本地 RAG 自动合规审计服务已就绪 (Event-Driven Edition)")
    logging.info(f"   分析模式 : {audit_mode}")
    logging.info(f"   审计模型 : {AUDIT_MODEL}")
    logging.info(f"   嵌入模型 : {EMBED_MODEL}")
    logging.info(f"   语义阈值 : {RELEVANCE_THRESHOLD}")
    logging.info("   监听目录 : inbox/")
    logging.info("==========================================================")
    logging.info("按 ESC 键安全退出，按 Ctrl+C 强制退出。")

    # 1. 扫描启动时已存在的文件并加入队列
    startup_files = sorted(
        os.path.join(INBOX, f) for f in os.listdir(INBOX) if f.endswith(".txt")
    )
    if startup_files:
        logging.info(
            f"扫描到启动时已存在的待审文件 {len(startup_files)} 个，加入待处理队列。"
        )
        for f in startup_files:
            abs_f = os.path.abspath(f)
            with _queue_lock:
                _queued_paths.add(abs_f)
            file_queue.put(abs_f)

    # 2. 启动 watchdog 监听 inbox 目录
    event_handler = InboxFileHandler()
    observer = Observer()
    observer.schedule(event_handler, path=INBOX, recursive=False)
    observer.start()

    try:
        while True:
            if not file_queue.empty():
                full_path = file_queue.get()
                try:
                    if os.path.exists(full_path):
                        if not wait_for_file_ready(full_path):
                            logging.warning(
                                f"文本文件写入不稳定，跳过审计: {os.path.basename(full_path)}"
                            )
                            continue
                        try:
                            # 对单个文件执行处理
                            success = process_file(full_path, collection, mode=audit_mode)

                            if success:
                                safe_move(full_path, ARCHIVE)
                            else:
                                safe_move(full_path, FAILED)
                                logging.warning(
                                    f"文件【{os.path.basename(full_path)}】审计失败，已移至 failed/ 目录。"
                                )
                        except Exception as process_err:
                            logging.error(
                                f"处理或移动文件 {full_path} 时发生严重错误: {process_err}"
                            )
                            try:
                                safe_move(full_path, FAILED)
                            except Exception as move_err:
                                logging.error(
                                    f"无法将已损坏文件移至 failed/ 目录: {move_err}"
                                )
                finally:
                    with _queue_lock:
                        _queued_paths.discard(os.path.abspath(full_path))
                    # 无论处理成功/失败/safe_move 本身抛异常，都必须调用 task_done
                    # 否则 queue.join() 将永久阻塞
                    file_queue.task_done()

                # 处理完后快速检查退出按键
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
        logging.info("合规审计服务已安全关闭。")
