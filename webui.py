import os
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path

import ollama
import pandas as pd
import streamlit as st

import app
import summarize_audits
import transcribe


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
def load_collection(mode: str = app.COMPLIANCE_MODE):
    app.check_environment()
    return app.initialize_knowledge_base(mode)


def read_uploaded_text(uploaded_file) -> str:
    if uploaded_file is None:
        return ""
    content = uploaded_file.getvalue()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别 TXT 文件编码，请另存为 UTF-8 后重新上传。")


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


def is_safe_rule_filename(filename: str) -> bool:
    stripped_name = filename.strip()
    if not stripped_name.endswith(".txt"):
        return False
    if not stripped_name or "/" in stripped_name or "\\" in stripped_name:
        return False
    if Path(stripped_name).name != stripped_name:
        return False
    return ".." not in Path(stripped_name).parts


def sync_knowledge_base_cache(
    rebuild_func=app.rebuild_knowledge_base,
    clear_cache=None,
):
    if clear_cache is None:
        clear_cache = load_collection.clear
    collection = rebuild_func()
    clear_cache()
    return collection


def run_audit_worker(
    input_path: str,
    collection,
    cancel_event: threading.Event,
    result_queue,
    mode: str = app.COMPLIANCE_MODE,
) -> None:
    try:
        result = app.process_file_with_result(
            input_path,
            collection,
            progress_prefix="WebUI 审计",
            cancel_checker=cancel_event.is_set,
            mode=mode,
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


def running_audit_poll_tick(
    poll_result=poll_audit_result,
    sleep_func=time.sleep,
    interval=0.5,
) -> None:
    sleep_func(interval)
    poll_result()


def render_outputs(result: app.ProcessResult) -> None:
    tasks_df = pd.read_csv(result.tasks_csv_path)
    risks_df = pd.read_csv(result.risk_csv_path)
    report_text = read_text(result.report_path)

    if result.mode == app.SEMICONDUCTOR_IP_MODE:
        high_risk_count = (
            int((risks_df["severity"] == "High").sum())
            if "severity" in risks_df.columns
            else 0
        )

        metric_cols = st.columns(3)
        metric_cols[0].metric("技术特征", len(tasks_df))
        metric_cols[1].metric("IP风险项", len(risks_df))
        metric_cols[2].metric("高风险", high_risk_count)

        risk_tab, claim_tab, report_tab, download_tab = st.tabs(
            ["IP风险项", "Claim Chart", "分析报告", "下载"]
        )

        with risk_tab:
            st.dataframe(risks_df, width="stretch", hide_index=True)

        with claim_tab:
            st.dataframe(tasks_df, width="stretch", hide_index=True)

        with report_tab:
            st.markdown(report_text)

        with download_tab:
            download_cols = st.columns(3)
            download_cols[0].download_button(
                "下载 Claim Chart CSV",
                data=read_bytes(result.tasks_csv_path),
                file_name=os.path.basename(result.tasks_csv_path),
                mime="text/csv",
                width="stretch",
            )
            download_cols[1].download_button(
                "下载 IP风险 CSV",
                data=read_bytes(result.risk_csv_path),
                file_name=os.path.basename(result.risk_csv_path),
                mime="text/csv",
                width="stretch",
            )
            download_cols[2].download_button(
                "下载分析报告",
                data=read_bytes(result.report_path),
                file_name=os.path.basename(result.report_path),
                mime="text/markdown",
                width="stretch",
            )
        return

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


def render_history() -> None:
    summary = summarize_audits.summarize_history(app.OUTPUT)
    if summary["audit_count"] == 0:
        st.info("还没有历史审计记录。完成一次审计后，这里会显示风险分布和最近记录。")
        return

    metric_cols = st.columns(5)
    metric_cols[0].metric("审计次数", summary["audit_count"])
    metric_cols[1].metric("任务数", summary["task_count"])
    metric_cols[2].metric("风险项", summary["risk_count"])
    metric_cols[3].metric("高风险", summary["high_risk_count"])
    metric_cols[4].metric("人工复核", summary["manual_review_count"])

    risk_type_df = pd.DataFrame(
        [
            {"risk_type": risk_type, "count": count}
            for risk_type, count in summary["risk_type_counts"].items()
        ]
    )
    if not risk_type_df.empty:
        st.subheader("问题类型分布")
        risk_type_df = risk_type_df.sort_values("count", ascending=False)
        st.dataframe(risk_type_df, width="stretch", hide_index=True)

    st.subheader("最近审计")
    st.dataframe(pd.DataFrame(summary["recent_entries"]), width="stretch", hide_index=True)


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

        st.markdown("---")
        st.subheader("Ollama 服务状态")
        status = app.check_ollama_status()
        if status["connected"]:
            st.success("Ollama 服务: 已连接")
            if status["audit_model_ok"]:
                st.success("审计模型: 就绪")
            else:
                st.warning("审计模型: 未拉取")
                if st.button("拉取审计模型", key="pull_audit_btn"):
                    with st.spinner("正在下载审计模型..."):
                        try:
                            ollama.pull(app.AUDIT_MODEL)
                            st.success("审计模型拉取成功！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"拉取失败: {e}")

            if status["embed_model_ok"]:
                st.success("向量模型: 就绪")
            else:
                st.warning("向量模型: 未拉取")
                if st.button("拉取向量模型", key="pull_embed_btn"):
                    with st.spinner("正在下载向量模型..."):
                        try:
                            ollama.pull(app.EMBED_MODEL)
                            st.success("向量模型拉取成功！")
                            st.rerun()
                        except Exception as e:
                            st.error(f"拉取失败: {e}")
        else:
            st.error("Ollama 服务: 离线")
            st.info("请检查 Ollama 服务是否启动 (例如运行 `ollama serve`)。")

        st.markdown("---")
        selected_mode = st.selectbox(
            "分析模式",
            options=[app.COMPLIANCE_MODE, app.SEMICONDUCTOR_IP_MODE],
            format_func=lambda value: "企业合规审计"
            if value == app.COMPLIANCE_MODE
            else "半导体专利/IP技术情报",
        )

    input_tab, upload_tab, audio_tab, history_tab, config_tab = st.tabs(
        ["粘贴文本", "上传 TXT", "上传音频", "历史统计", "合规条款管理"]
    )

    with input_tab:
        pasted_text = st.text_area(
            "会议记录 / SOP / 任务指派文本"
            if selected_mode == app.COMPLIANCE_MODE
            else "公开专利文本 / 技术交底 / 产品说明 / 论文摘要",
            value=DEMO_TEXT,
            height=220,
        )

    with upload_tab:
        uploaded_file = st.file_uploader("选择 .txt 文件", type=["txt"])
        try:
            uploaded_text = read_uploaded_text(uploaded_file)
        except ValueError as exc:
            uploaded_text = ""
            st.error(str(exc))
        if uploaded_text:
            st.text_area("文件内容预览", value=uploaded_text, height=220)

    with audio_tab:
        st.subheader("本地语音转文字审计")
        missing_deps = []
        if not shutil.which("ffmpeg"):
            missing_deps.append("ffmpeg")
        if not shutil.which("ffprobe"):
            missing_deps.append("ffprobe")
        whisper_cli = transcribe.WHISPER_CLI
        if not os.path.isfile(whisper_cli) or not os.access(whisper_cli, os.X_OK):
            missing_deps.append(f"whisper-cli ({whisper_cli})")
        if not os.path.isfile(transcribe.DEFAULT_MODEL):
            missing_deps.append(f"Whisper 模型 ggml-medium.bin (路径: {transcribe.DEFAULT_MODEL})")

        if missing_deps:
            st.warning("⚠️ 语音识别依赖未完整配置，音频转录功能暂不可用：")
            for dep in missing_deps:
                st.write(f"- 缺失 `{dep}`")
            st.info("提示：请在本地安装 ffmpeg，并检查 `.env` 中的 `WHISPER_CLI` 和 `WHISPER_MODEL` 路径是否正确。")
        else:
            uploaded_audio = st.file_uploader(
                "选择音频文件",
                type=[ext.lstrip(".") for ext in transcribe.AUDIO_EXTENSIONS],
            )
            if uploaded_audio is not None:
                st.audio(uploaded_audio)
                if st.button("开始音频转录与合规审计", type="primary", disabled=st.session_state.audit_running):
                    suffix = Path(uploaded_audio.name).suffix
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_audio:
                        temp_audio.write(uploaded_audio.getvalue())
                        temp_audio_path = temp_audio.name
                    
                    try:
                        with st.spinner("正在转换为 16kHz WAV 并使用 Whisper 离线转录..."):
                            tmp_dir = tempfile.mkdtemp(prefix="webui_transcribe_")
                            try:
                                if transcribe.is_native_format(temp_audio_path):
                                    audio_to_process = temp_audio_path
                                else:
                                    audio_to_process = transcribe.convert_to_wav(temp_audio_path, tmp_dir)
                                
                                with tempfile.NamedTemporaryFile(delete=False, suffix="") as temp_txt_base:
                                    txt_base_path = temp_txt_base.name
                                
                                txt_file, elapsed = transcribe.run_whisper(audio_to_process, txt_base_path)
                                with open(txt_file, "r", encoding="utf-8") as f_txt:
                                    transcribed_text = f_txt.read()
                                
                                try:
                                    os.remove(txt_file)
                                except OSError:
                                    pass
                            finally:
                                shutil.rmtree(tmp_dir, ignore_errors=True)
                                try:
                                    os.remove(temp_audio_path)
                                except OSError:
                                    pass
                                try:
                                    os.remove(txt_base_path)
                                except OSError:
                                    pass
                        
                        if not transcribed_text.strip():
                            st.error("转录完成，但内容为空，请检查音频文件。")
                        else:
                            st.success(f"转录成功！耗时: {transcribe.format_duration(elapsed)}")
                            input_path = write_web_input(transcribed_text, uploaded_audio.name)
                            with st.spinner("正在初始化本地知识库..."):
                                collection = load_collection(app.COMPLIANCE_MODE)
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
                    except Exception as exc:
                        st.error(f"转录失败: {exc}")

    with history_tab:
        render_history()

    with config_tab:
        st.subheader("本地合规条款管理")
        rule_dir = Path(app.CONFIG_DIR)
        rule_dir.mkdir(parents=True, exist_ok=True)
        rule_files = sorted([f.name for f in rule_dir.glob("*.txt")])
        
        if rule_files:
            selected_file = st.selectbox("选择要修改或查看的条款文件", rule_files)
            file_path = rule_dir / selected_file
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    file_content = f.read()
            except Exception:
                file_content = ""
                
            edited_content = st.text_area("文件内容编辑", value=file_content, height=250, key="edit_area")
            
            edit_cols = st.columns(2)
            if edit_cols[0].button("保存条款修改", key="save_edit_btn"):
                try:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(edited_content)
                    st.success(f"保存成功：{selected_file}")
                    with st.spinner("正在重新构建语义向量库..."):
                        sync_knowledge_base_cache()
                    st.success("语义向量库已同步重构！")
                    st.rerun()
                except Exception as e:
                    st.error(f"保存失败: {e}")
                    
            if edit_cols[1].button("删除该条款文件", type="secondary", key="del_edit_btn"):
                try:
                    os.remove(file_path)
                    st.success(f"已删除：{selected_file}")
                    with st.spinner("正在重新构建语义向量库..."):
                        sync_knowledge_base_cache()
                    st.success("语义向量库已同步重构！")
                    st.rerun()
                except Exception as e:
                    st.error(f"删除失败: {e}")
        else:
            st.info("当前 compliance_rules 目录下没有规则条款文本文件。")
            
        st.markdown("---")
        st.subheader("新建合规条款文件")
        new_file_name = st.text_input("文件名 (例如: custom_rules.txt)", placeholder="必须以 .txt 结尾")
        new_file_content = st.text_area("内容", height=150, placeholder="在此输入合规检查基准条款...")
        
        if st.button("创建并加载规则", type="primary", key="create_rule_btn"):
            if not is_safe_rule_filename(new_file_name):
                st.error("文件名必须是当前目录下的 .txt 文件名，不能包含路径或 ..")
            elif not new_file_content.strip():
                st.error("内容不能为空")
            else:
                new_file_path = rule_dir / new_file_name.strip()
                try:
                    with open(new_file_path, "w", encoding="utf-8") as f:
                        f.write(new_file_content)
                    st.success(f"创建成功：{new_file_name}")
                    with st.spinner("正在重新构建语义向量库..."):
                        sync_knowledge_base_cache()
                    st.success("语义向量库已构建！")
                    st.rerun()
                except Exception as e:
                    st.error(f"创建失败: {e}")

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
            collection = load_collection(selected_mode)
        cancel_event = threading.Event()
        result_queue = queue.Queue()
        audit_thread = threading.Thread(
            target=run_audit_worker,
            args=(input_path, collection, cancel_event, result_queue, selected_mode),
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
        with st.spinner("正在执行本地审计..."):
            st.info("正在审计中，可以点击“停止审计”中断本次任务。")
            running_audit_poll_tick()
            thread = st.session_state.get("audit_thread")
            if thread is not None and not thread.is_alive():
                poll_audit_result()
                if st.session_state.audit_running:
                    st.session_state.audit_running = False
                    st.session_state.audit_error = "审计线程意外终止。"
                    cleanup_input_file()
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
