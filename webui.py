import os
import queue
import tempfile
import threading
import time
from pathlib import Path

import pandas as pd
import streamlit as st

import app


DEMO_TEXT = """今天会议决定把客户张女士的手机号 13812345678 和邮箱 zhangsan@example.com 发给销售团队。
研发后续尽快处理数据导出脚本，相关人员负责。
产品和法务一起看一下，没问题就上线。
"""


def init_audit_state() -> None:
    defaults = {
        "audit_running": False,
        "audit_result": None,
        "audit_error": "",
        "audit_input_path": "",
        "audit_cancel_event": None,
        "audit_queue": None,
        "audit_thread": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


@st.cache_resource(show_spinner=False)
def load_collection():
    app.check_environment()
    return app.initialize_knowledge_base()


def read_uploaded_text(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    return uploaded_file.getvalue().decode("utf-8-sig")


def write_web_input(text: str, filename: str) -> str:
    safe_name = app.mask_output_basename(Path(filename).stem or "webui_input")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        prefix=f"{safe_name}_",
        delete=False,
    ) as temp_file:
        temp_file.write(text)
        return temp_file.name


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def run_audit_worker(
    input_path: str,
    collection,
    cancel_event: threading.Event,
    result_queue,
) -> None:
    try:
        result = app.process_file_with_result(
            input_path,
            collection,
            progress_prefix="WebUI 审计",
            cancel_checker=cancel_event.is_set,
        )
        result_queue.put(("result", result))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def cleanup_input_file() -> None:
    input_path = st.session_state.get("audit_input_path")
    if not input_path:
        return
    try:
        os.remove(input_path)
    except OSError:
        pass
    st.session_state.audit_input_path = ""


def poll_audit_result() -> None:
    result_queue = st.session_state.get("audit_queue")
    if result_queue is None:
        return
    try:
        kind, payload = result_queue.get_nowait()
    except queue.Empty:
        return

    st.session_state.audit_running = False
    if kind == "result":
        st.session_state.audit_result = payload
        st.session_state.audit_error = ""
    else:
        st.session_state.audit_result = None
        st.session_state.audit_error = payload
    cleanup_input_file()


def render_outputs(result: app.ProcessResult) -> None:
    tasks_df = pd.read_csv(result.tasks_csv_path)
    risks_df = pd.read_csv(result.risk_csv_path)
    report_text = read_text(result.report_path)

    high_risk_count = (
        int((risks_df["severity"] == "High").sum())
        if "severity" in risks_df.columns
        else 0
    )

    metric_cols = st.columns(3)
    metric_cols[0].metric("任务数", len(tasks_df))
    metric_cols[1].metric("风险项", len(risks_df))
    metric_cols[2].metric("高风险", high_risk_count)

    risk_tab, task_tab, report_tab, download_tab = st.tabs(
        ["风险项", "任务表", "审计报告", "下载"]
    )

    with risk_tab:
        st.dataframe(risks_df, width="stretch", hide_index=True)

    with task_tab:
        st.dataframe(tasks_df, width="stretch", hide_index=True)

    with report_tab:
        st.markdown(report_text)

    with download_tab:
        download_cols = st.columns(3)
        download_cols[0].download_button(
            "下载任务 CSV",
            data=read_bytes(result.tasks_csv_path),
            file_name=os.path.basename(result.tasks_csv_path),
            mime="text/csv",
            width="stretch",
        )
        download_cols[1].download_button(
            "下载风险 CSV",
            data=read_bytes(result.risk_csv_path),
            file_name=os.path.basename(result.risk_csv_path),
            mime="text/csv",
            width="stretch",
        )
        download_cols[2].download_button(
            "下载审计报告",
            data=read_bytes(result.report_path),
            file_name=os.path.basename(result.report_path),
            mime="text/markdown",
            width="stretch",
        )


def main() -> None:
    st.set_page_config(
        page_title="Offline Auto Audit",
        page_icon="📋",
        layout="wide",
    )
    init_audit_state()
    poll_audit_result()

    st.title("Offline Auto Audit")
    st.caption("本地会议合规、数据治理与任务指派审计")

    with st.sidebar:
        st.subheader("本地模型")
        st.write(f"审计模型：`{app.AUDIT_MODEL}`")
        st.write(f"向量模型：`{app.EMBED_MODEL}`")
        st.write(f"语义阈值：`{app.RELEVANCE_THRESHOLD}`")

    input_tab, upload_tab = st.tabs(["粘贴文本", "上传 TXT"])

    with input_tab:
        pasted_text = st.text_area(
            "会议记录 / SOP / 任务指派文本",
            value=DEMO_TEXT,
            height=220,
        )

    with upload_tab:
        uploaded_file = st.file_uploader("选择 .txt 文件", type=["txt"])
        uploaded_text = read_uploaded_text(uploaded_file)
        if uploaded_text:
            st.text_area("文件内容预览", value=uploaded_text, height=220)

    source_text = uploaded_text.strip() if uploaded_text else pasted_text.strip()
    source_name = uploaded_file.name if uploaded_file is not None else "webui_input.txt"

    start_disabled = st.session_state.audit_running
    button_cols = st.columns([2, 1])

    if button_cols[0].button(
        "开始审计",
        type="primary",
        width="stretch",
        disabled=start_disabled,
    ):
        if not source_text:
            st.warning("请先粘贴文本或上传 TXT 文件。")
            return

        input_path = write_web_input(source_text, source_name)
        with st.spinner("正在初始化本地知识库..."):
            collection = load_collection()
        cancel_event = threading.Event()
        result_queue = queue.Queue()
        audit_thread = threading.Thread(
            target=run_audit_worker,
            args=(input_path, collection, cancel_event, result_queue),
            daemon=True,
        )
        st.session_state.audit_running = True
        st.session_state.audit_result = None
        st.session_state.audit_error = ""
        st.session_state.audit_input_path = input_path
        st.session_state.audit_cancel_event = cancel_event
        st.session_state.audit_queue = result_queue
        st.session_state.audit_thread = audit_thread
        audit_thread.start()
        st.rerun()

    if button_cols[1].button(
        "停止审计",
        width="stretch",
        disabled=not st.session_state.audit_running,
    ):
        cancel_event = st.session_state.get("audit_cancel_event")
        if cancel_event is not None:
            cancel_event.set()
        st.warning("已发送停止请求，正在等待当前模型流式输出中断。")

    if st.session_state.audit_running:
        st.info("正在审计中，可以点击“停止审计”中断本次任务。")
        time.sleep(1)
        st.rerun()

    result = st.session_state.audit_result
    if result is not None:
        if result.cancelled:
            st.warning("本次审计已停止。")
        elif not result.success:
            st.error("审计失败，请检查 Ollama 模型服务、输入文本或终端日志。")
        else:
            st.success("审计完成")
            render_outputs(result)

    if st.session_state.audit_error:
        st.error(f"审计异常：{st.session_state.audit_error}")


if __name__ == "__main__":
    main()
