import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

import app


DEMO_TEXT = """今天会议决定把客户张女士的手机号 13812345678 和邮箱 zhangsan@example.com 发给销售团队。
研发后续尽快处理数据导出脚本，相关人员负责。
产品和法务一起看一下，没问题就上线。
"""


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
        st.dataframe(risks_df, use_container_width=True, hide_index=True)

    with task_tab:
        st.dataframe(tasks_df, use_container_width=True, hide_index=True)

    with report_tab:
        st.markdown(report_text)

    with download_tab:
        download_cols = st.columns(3)
        download_cols[0].download_button(
            "下载任务 CSV",
            data=read_bytes(result.tasks_csv_path),
            file_name=os.path.basename(result.tasks_csv_path),
            mime="text/csv",
            use_container_width=True,
        )
        download_cols[1].download_button(
            "下载风险 CSV",
            data=read_bytes(result.risk_csv_path),
            file_name=os.path.basename(result.risk_csv_path),
            mime="text/csv",
            use_container_width=True,
        )
        download_cols[2].download_button(
            "下载审计报告",
            data=read_bytes(result.report_path),
            file_name=os.path.basename(result.report_path),
            mime="text/markdown",
            use_container_width=True,
        )


def main() -> None:
    st.set_page_config(
        page_title="Offline Auto Audit",
        page_icon="📋",
        layout="wide",
    )

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

    if st.button("开始审计", type="primary", use_container_width=True):
        if not source_text:
            st.warning("请先粘贴文本或上传 TXT 文件。")
            return

        input_path = write_web_input(source_text, source_name)
        try:
            with st.spinner("正在初始化知识库并执行本地审计..."):
                collection = load_collection()
                result = app.process_file_with_result(
                    input_path,
                    collection,
                    progress_prefix="WebUI 审计",
                )

            if not result.success:
                st.error("审计失败，请检查 Ollama 模型服务、输入文本或终端日志。")
                return

            st.success("审计完成")
            render_outputs(result)
        finally:
            try:
                os.remove(input_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
