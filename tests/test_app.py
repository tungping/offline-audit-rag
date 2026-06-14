import os
import shutil
import tempfile
import unittest
from pathlib import Path
import unittest.mock as mock
import pandas as pd

import audit_rules
import app
import transcribe
import webui


class AppTests(unittest.TestCase):
    def test_recursive_split_text_handles_long_text_without_separators(self):
        chunks = app.recursive_split_text("测" * 600, chunk_size=500, chunk_overlap=200)

        self.assertTrue(chunks)
        self.assertTrue("".join(chunk.replace("\n", "") for chunk in chunks).startswith("测" * 500))
        self.assertTrue(all(app.count_tokens(chunk) <= 500 for chunk in chunks))

    def test_extract_json_object_ignores_wrappers_and_preamble(self):
        text = '说明文字\n```json\n{"tasks": [], "audit_summary": "ok"}\n```\n结尾'

        self.assertEqual(app.extract_json_object(text), '{"tasks": [], "audit_summary": "ok"}')

    def test_markdown_table_cell_escapes_pipe_and_newline(self):
        self.assertEqual(app.markdown_table_cell("负责人|A\n第二行"), "负责人\\|A 第二行")

    def test_safe_move_avoids_repeated_name_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "source" / "report.txt"
            dest = tmp_path / "dest"
            src.parent.mkdir()
            dest.mkdir()
            src.write_text("new", encoding="utf-8")
            (dest / "report.txt").write_text("old", encoding="utf-8")
            (dest / "report_20260603_120000.txt").write_text("older", encoding="utf-8")

            moved_path = app.safe_move(
                str(src),
                str(dest),
                timestamp_func=lambda: "20260603_120000",
            )

            self.assertEqual(os.path.basename(moved_path), "report_20260603_120000_1.txt")
            self.assertTrue(os.path.exists(moved_path))

    def test_unique_file_path_avoids_overwriting_existing_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "meeting_2026-06-11_10_41.csv"
            second = Path(tmp) / "meeting_2026-06-11_10_41_1.csv"
            first.write_text("old", encoding="utf-8")
            second.write_text("older", encoding="utf-8")

            unique_path = app.unique_file_path(str(first))

            self.assertEqual(os.path.basename(unique_path), "meeting_2026-06-11_10_41_2.csv")
            self.assertFalse(os.path.exists(unique_path))

    def test_transcribe_unique_output_base_avoids_overwriting_existing_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_base = os.path.join(tmp, "meeting_2026-06-11_10_41")
            Path(f"{output_base}.txt").write_text("old", encoding="utf-8")
            Path(f"{output_base}_1.txt").write_text("older", encoding="utf-8")

            unique_base = transcribe.unique_output_base(output_base)

            self.assertEqual(os.path.basename(unique_base), "meeting_2026-06-11_10_41_2")
            self.assertFalse(os.path.exists(f"{unique_base}.txt"))

    def test_transcribe_resolve_executable_supports_path_command_names(self):
        self.assertEqual(transcribe.resolve_executable("python"), shutil.which("python") or "python")
        self.assertEqual(transcribe.resolve_executable("~/bin/custom-tool"), os.path.expanduser("~/bin/custom-tool"))

    def test_build_risk_items_masks_sensitive_evidence(self):
        text = (
            "客户手机号 13812345678，邮箱 zhangsan@example.com，"
            "身份证 11010519491231002X，需要发给销售。"
        )

        risks = audit_rules.build_risk_items(text, [], "demo.txt")

        evidence = " ".join(str(item["evidence_masked"]) for item in risks)
        self.assertIn("138****5678", evidence)
        self.assertIn("z******n@example.com", evidence)
        self.assertIn("110105********002X", evidence)
        self.assertNotIn("13812345678", evidence)
        self.assertNotIn("zhangsan@example.com", evidence)
        self.assertNotIn("11010519491231002X", evidence)

        high_risks = [item for item in risks if item["severity"] == "High"]
        self.assertTrue(high_risks)
        self.assertTrue(all(item["manual_review_required"] for item in high_risks))

    def test_build_risk_items_detects_ambiguous_phrases(self):
        text = "研发后续跟进数据导出脚本，相关人员尽快处理。"

        risks = audit_rules.build_risk_items(text, [], "demo.txt")

        vague_risks = [item for item in risks if item["risk_type"] == "模糊表述"]
        evidence = {item["evidence_masked"] for item in vague_risks}
        self.assertIn("后续跟进", evidence)
        self.assertIn("相关人员", evidence)
        self.assertIn("尽快", evidence)
        self.assertTrue(all(item["severity"] == "Medium" for item in vague_risks))

    def test_build_risk_items_detects_sop_gaps_for_unassigned_task(self):
        text = "数据导出脚本需要处理，完成后同步业务方。"
        tasks = [{"task_name": "处理数据导出脚本", "owner": "Unassigned", "priority": "High"}]

        risks = audit_rules.build_risk_items(text, tasks, "demo.txt")

        sop_risks = [item for item in risks if item["risk_type"] == "SOP缺失"]
        evidence = {item["evidence_masked"] for item in sop_risks}
        self.assertIn("任务缺少明确负责人: 处理数据导出脚本", evidence)
        self.assertIn("缺少明确截止时间", evidence)
        self.assertIn("缺少验收标准或交付物定义", evidence)

    def test_build_risk_items_detects_cross_department_risk_without_confirmer(self):
        text = "产品、法务、研发和销售一起看一下客户数据导出方案，没问题就上线。"

        risks = audit_rules.build_risk_items(text, [], "demo.txt")

        collaboration_risks = [item for item in risks if item["risk_type"] == "跨部门协作风险"]
        self.assertEqual(len(collaboration_risks), 1)
        self.assertIn("产品、研发、法务、销售", collaboration_risks[0]["evidence_masked"])
        self.assertEqual(collaboration_risks[0]["severity"], "Medium")

    @mock.patch('app.ollama.embeddings')
    @mock.patch('app.ollama.generate')
    def test_process_file_success(self, mock_generate, mock_embeddings):
        # 1. 设置 mock 返回值
        mock_embeddings.return_value = {'embedding': [0.1] * 768}
        
        mock_stream = [
            {'response': '{\n'},
            {'response': '  "compliance_risk": "高",\n'},
            {'response': '  "audit_summary": "测试汇总",\n'},
            {'response': '  "tasks": [\n'},
            {'response': '    {"task_name": "联系客户 13812345678", "owner": "zhangsan@example.com", "priority": "High"}\n'},
            {'response': '  ]\n'},
            {'response': '}\n'}
        ]
        mock_generate.return_value = mock_stream
        
        mock_collection = mock.Mock()
        mock_collection.query.return_value = {
            'documents': [['【条款 1】: 严禁直接向 master 推送代码。']],
            'distances': [[0.2]]
        }
        
        # 2. 创建隔离的测试输入输出目录
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_inbox = os.path.join(tmp_dir, "inbox")
            test_output = os.path.join(tmp_dir, "output")
            os.makedirs(test_inbox)
            os.makedirs(test_output)
            
            # 补丁路径
            with mock.patch('app.OUTPUT', test_output):
                test_file = os.path.join(test_inbox, "test_meeting.txt")
                with open(test_file, 'w', encoding='utf-8') as f:
                    f.write(
                        "张三昨天直接把代码 push 到了 master，没有经过 Review。"
                        "客户手机号 13812345678 和邮箱 zhangsan@example.com 需要发给销售团队。"
                        "研发后续跟进，相关人员尽快处理。"
                    )
                
                # 3. 执行审计处理
                success = app.process_file(test_file, mock_collection)
                
                # 4. 验证成功标志与生成文件
                self.assertTrue(success)
                
                out_files = os.listdir(test_output)
                self.assertEqual(len(out_files), 3)
                
                tasks_csv_file = [f for f in out_files if f.endswith('_tasks.csv')][0]
                risk_csv_file = [f for f in out_files if f.endswith('_risk_items.csv')][0]
                md_file = [f for f in out_files if f.endswith('_audit_report.md')][0]
                
                # 验证 CSV 字段与内容 (包含新增强的源文件和截止日期字段)
                df = pd.read_csv(os.path.join(test_output, tasks_csv_file))
                self.assertEqual(len(df), 1)
                self.assertEqual(df.loc[0, 'task_name'], '联系客户 138****5678')
                self.assertEqual(df.loc[0, 'owner'], 'z******n@example.com')
                self.assertEqual(df.loc[0, 'priority'], 'High')
                self.assertEqual(df.loc[0, 'source_file'], 'test_meeting.txt')
                self.assertTrue('due_date' in df.columns)
                with open(os.path.join(test_output, tasks_csv_file), 'r', encoding='utf-8-sig') as f_csv:
                    task_csv_text = f_csv.read()
                    self.assertIn("138****5678", task_csv_text)
                    self.assertIn("z******n@example.com", task_csv_text)
                    self.assertNotIn("13812345678", task_csv_text)
                    self.assertNotIn("zhangsan@example.com", task_csv_text)

                risk_df = pd.read_csv(os.path.join(test_output, risk_csv_file))
                self.assertTrue({"risk_type", "severity", "evidence_masked", "recommendation", "manual_review_required"}.issubset(risk_df.columns))
                risk_evidence = " ".join(str(value) for value in risk_df["evidence_masked"])
                self.assertIn("138****5678", risk_evidence)
                self.assertIn("z******n@example.com", risk_evidence)
                self.assertNotIn("13812345678", risk_evidence)
                self.assertNotIn("zhangsan@example.com", risk_evidence)
                with open(os.path.join(test_output, risk_csv_file), 'r', encoding='utf-8-sig') as f_csv:
                    risk_csv_text = f_csv.read()
                    self.assertIn("138****5678", risk_csv_text)
                    self.assertIn("z******n@example.com", risk_csv_text)
                    self.assertNotIn("13812345678", risk_csv_text)
                    self.assertNotIn("zhangsan@example.com", risk_csv_text)
                
                # 验证 MD 包含的报告内容和原始片段
                with open(os.path.join(test_output, md_file), 'r', encoding='utf-8') as f_md:
                    md_text = f_md.read()
                    self.assertIn("test_meeting.txt", md_text)
                    self.assertIn("测试汇总", md_text)
                    self.assertIn("联系客户 138****5678", md_text)
                    self.assertIn("张三", md_text)
                    self.assertIn("四、数据合规与流程治理风险", md_text)
                    self.assertIn("五、会议原始文本摘要", md_text)
                    self.assertIn("138****5678", md_text)
                    self.assertIn("z******n@example.com", md_text)
                    self.assertNotIn("13812345678", md_text)
                    self.assertNotIn("zhangsan@example.com", md_text)
                    self.assertIn("张三昨天直接把代码 push 到了 master", md_text)

    @mock.patch('app.ollama.embeddings')
    @mock.patch('app.ollama.generate')
    def test_process_file_masks_malformed_model_output_preview_in_logs(self, mock_generate, mock_embeddings):
        mock_embeddings.return_value = {'embedding': [0.1] * 768}
        mock_generate.return_value = [
            {'response': '{"tasks": [{"task_name": "联系客户 13812345678", '},
            {'response': '"owner": "zhangsan@example.com", "priority": "High"}]'},
        ]

        mock_collection = mock.Mock()
        mock_collection.query.return_value = {
            'documents': [['【条款 1】: 严禁直接向 master 推送代码。']],
            'distances': [[0.2]]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_output = os.path.join(tmp_dir, "output")
            os.makedirs(test_output)
            test_file = os.path.join(tmp_dir, "malformed_model_output.txt")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("客户手机号 13812345678，邮箱 zhangsan@example.com，需要安排回访。")

            with mock.patch('app.OUTPUT', test_output):
                with self.assertLogs(level="ERROR") as log_cm:
                    success = app.process_file(test_file, mock_collection)

        self.assertFalse(success)
        logs = "\n".join(log_cm.output)
        self.assertIn("--- 原始模型输出预览 ---", logs)
        self.assertIn("138****5678", logs)
        self.assertIn("z******n@example.com", logs)
        self.assertNotIn("13812345678", logs)
        self.assertNotIn("zhangsan@example.com", logs)

    @mock.patch('app.ollama.embeddings')
    @mock.patch('app.ollama.generate')
    def test_process_file_masks_sensitive_values_in_output_filenames(self, mock_generate, mock_embeddings):
        mock_embeddings.return_value = {'embedding': [0.1] * 768}
        mock_generate.return_value = [
            {'response': '{"compliance_risk": "低", "audit_summary": "无违规", "tasks": []}'},
        ]

        mock_collection = mock.Mock()
        mock_collection.query.return_value = {
            'documents': [['【条款 1】: 严禁直接向 master 推送代码。']],
            'distances': [[0.2]]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_output = os.path.join(tmp_dir, "output")
            os.makedirs(test_output)
            test_file = os.path.join(tmp_dir, "客户_13812345678_zhangsan@example.com.txt")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("本次会议流程正常，无异常事项。")

            with mock.patch('app.OUTPUT', test_output):
                success = app.process_file(test_file, mock_collection)

            self.assertTrue(success)
            out_files = os.listdir(test_output)
            self.assertEqual(len(out_files), 3)
            self.assertTrue(any(filename.endswith("_tasks.csv") for filename in out_files))
            self.assertTrue(any(filename.endswith("_risk_items.csv") for filename in out_files))
            self.assertTrue(any(filename.endswith("_audit_report.md") for filename in out_files))

            filenames = "\n".join(out_files)
            self.assertNotIn("13812345678", filenames)
            self.assertNotIn("zhangsan@example.com", filenames)
            self.assertNotIn("*", filenames)

    @mock.patch('app.ollama.embeddings')
    @mock.patch('app.ollama.generate')
    def test_process_file_with_result_can_be_cancelled(self, mock_generate, mock_embeddings):
        mock_embeddings.return_value = {'embedding': [0.1] * 768}
        mock_generate.return_value = [
            {'response': '{"compliance_risk": "低", '},
            {'response': '"audit_summary": "无违规", "tasks": []}'},
        ]

        mock_collection = mock.Mock()
        mock_collection.query.return_value = {
            'documents': [['【条款 1】: 严禁直接向 master 推送代码。']],
            'distances': [[0.2]]
        }

        calls = {"count": 0}

        def cancel_after_first_chunk():
            calls["count"] += 1
            return calls["count"] > 1

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_output = os.path.join(tmp_dir, "output")
            os.makedirs(test_output)
            test_file = os.path.join(tmp_dir, "cancel_demo.txt")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("本次会议流程正常，无异常事项。")

            with mock.patch('app.OUTPUT', test_output):
                result = app.process_file_with_result(
                    test_file,
                    mock_collection,
                    cancel_checker=cancel_after_first_chunk,
                )

            self.assertFalse(result.success)
            self.assertTrue(result.cancelled)
            self.assertEqual(os.listdir(test_output), [])

    @mock.patch('app.ollama.embeddings')
    @mock.patch('app.ollama.generate')
    def test_process_file_with_result_returns_output_paths(self, mock_generate, mock_embeddings):
        mock_embeddings.return_value = {'embedding': [0.1] * 768}
        mock_generate.return_value = [
            {'response': '{"compliance_risk": "低", "audit_summary": "无违规", "tasks": []}'},
        ]

        mock_collection = mock.Mock()
        mock_collection.query.return_value = {
            'documents': [['【条款 1】: 严禁直接向 master 推送代码。']],
            'distances': [[0.2]]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_output = os.path.join(tmp_dir, "output")
            os.makedirs(test_output)
            test_file = os.path.join(tmp_dir, "webui_demo.txt")
            with open(test_file, 'w', encoding='utf-8') as f:
                f.write("本次会议流程正常，无异常事项。")

            with mock.patch('app.OUTPUT', test_output):
                result = app.process_file_with_result(test_file, mock_collection)

            self.assertTrue(result.success)
            self.assertTrue(result.tasks_csv_path.endswith("_tasks.csv"))
            self.assertTrue(result.risk_csv_path.endswith("_risk_items.csv"))
            self.assertTrue(result.report_path.endswith("_audit_report.md"))
            self.assertTrue(os.path.exists(result.tasks_csv_path))
            self.assertTrue(os.path.exists(result.risk_csv_path))
            self.assertTrue(os.path.exists(result.report_path))

    @mock.patch('app.ollama.embeddings')
    @mock.patch('app.ollama.generate')
    def test_process_file_with_no_rag_results(self, mock_generate, mock_embeddings):
        """RAG 全部低于阈值时，审计应仍正常完成，Markdown 报告包含警告。"""
        mock_embeddings.return_value = {'embedding': [0.1] * 768}

        mock_stream = [
            {'response': '{"compliance_risk": "低", "audit_summary": "无违规", "tasks": []}'},
        ]
        mock_generate.return_value = mock_stream

        # 所有距离均超过阈值（默认 0.5），模拟零命中
        mock_collection = mock.Mock()
        mock_collection.query.return_value = {
            'documents': [['某合规条款']],
            'distances': [[0.9]]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_output = os.path.join(tmp_dir, "output")
            os.makedirs(test_output)

            with mock.patch('app.OUTPUT', test_output):
                test_file = os.path.join(tmp_dir, "empty_rag.txt")
                with open(test_file, 'w', encoding='utf-8') as f:
                    f.write("本次会议流程正常，无异常事项。")

                success = app.process_file(test_file, mock_collection)
                self.assertTrue(success)

                out_files = os.listdir(test_output)
                md_file = [f for f in out_files if f.endswith('_audit_report.md')][0]

                with open(os.path.join(test_output, md_file), 'r', encoding='utf-8') as f_md:
                    md_text = f_md.read()
                    # 验证 MD 报告包含 RAG 零结果警告
                    self.assertIn("⚠️", md_text)
                    self.assertIn("RELEVANCE_THRESHOLD", md_text)
                    self.assertNotIn("参考规范 1", md_text)

    def test_extract_json_object_fixes_trailing_commas(self):
        # trailing comma in list and in dict
        malformed = '{"tasks": [{"a": 1,},], "summary": "ok",}'
        fixed = app.extract_json_object(malformed)
        import json
        data = json.loads(fixed)
        self.assertEqual(data["tasks"][0]["a"], 1)
        self.assertEqual(data["summary"], "ok")

    @mock.patch('app.ollama.Client')
    def test_check_ollama_status_offline(self, mock_client_cls):
        mock_client = mock.Mock()
        mock_client.list.side_effect = Exception("connection refused")
        mock_client_cls.return_value = mock_client
        
        status = app.check_ollama_status()
        self.assertFalse(status["connected"])
        self.assertIn("connection refused", status["error"])

    @mock.patch('app.ollama.Client')
    def test_check_ollama_status_connected(self, mock_client_cls):
        mock_client = mock.Mock()
        mock_client.list.return_value = {
            "models": [
                {"model": "qwen3.5:9b"},
                {"model": "nomic-embed-text:latest"}
            ]
        }
        mock_client_cls.return_value = mock_client
        
        status = app.check_ollama_status()
        self.assertTrue(status["connected"])
        self.assertTrue(status["audit_model_ok"])
        self.assertTrue(status["embed_model_ok"])

    @mock.patch('app.ollama.embeddings')
    def test_retrieve_relevant_context_chunks_for_long_text(self, mock_embeddings):
        mock_embeddings.return_value = {'embedding': [0.1] * 768}
        
        mock_collection = mock.Mock()
        mock_collection.query.return_value = {
            'documents': [['【条款 1】: 规则']],
            'distances': [[0.2]]
        }
        
        # A long text (over 500 tokens / words) to trigger chunking
        long_text = "test " * 600
        docs = app.retrieve_relevant_context(mock_collection, long_text, top_k=2)
        
        self.assertTrue(docs)
        self.assertTrue(mock_embeddings.call_count > 1)

    def test_webui_rule_filename_rejects_path_traversal(self):
        unsafe_names = [
            "../escape.txt",
            "nested/rules.txt",
            "/tmp/rules.txt",
            "..\\escape.txt",
            "",
            "rules.md",
        ]

        for name in unsafe_names:
            with self.subTest(name=name):
                self.assertFalse(webui.is_safe_rule_filename(name))

        self.assertTrue(webui.is_safe_rule_filename("custom_rules.txt"))

    def test_webui_sync_knowledge_base_cache_clears_cached_collection(self):
        clear_mock = mock.Mock()
        rebuild_mock = mock.Mock(return_value="collection")

        result = webui.sync_knowledge_base_cache(
            rebuild_func=rebuild_mock,
            clear_cache=clear_mock,
        )

        self.assertEqual(result, "collection")
        rebuild_mock.assert_called_once_with()
        clear_mock.assert_called_once_with()

    def test_webui_running_audit_poll_tick_does_not_block_until_completion(self):
        poll_mock = mock.Mock()
        sleep_mock = mock.Mock()

        webui.running_audit_poll_tick(
            poll_result=poll_mock,
            sleep_func=sleep_mock,
            interval=0.2,
        )

        sleep_mock.assert_called_once_with(0.2)
        poll_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
