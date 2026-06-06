import os
import tempfile
import unittest
from pathlib import Path
import unittest.mock as mock
import pandas as pd

import app


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
            {'response': '    {"task_name": "测试任务", "owner": "张三", "priority": "High"}\n'},
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
                    f.write("张三昨天直接把代码 push 到了 master，没有经过 Review。")
                
                # 3. 执行审计处理
                success = app.process_file(test_file, mock_collection)
                
                # 4. 验证成功标志与生成文件
                self.assertTrue(success)
                
                out_files = os.listdir(test_output)
                self.assertEqual(len(out_files), 2)  # 包含一个 .csv 与一个 .md
                
                csv_file = [f for f in out_files if f.endswith('.csv')][0]
                md_file = [f for f in out_files if f.endswith('.md')][0]
                
                # 验证 CSV 字段与内容 (包含新增强的源文件和截止日期字段)
                df = pd.read_csv(os.path.join(test_output, csv_file))
                self.assertEqual(len(df), 1)
                self.assertEqual(df.loc[0, 'task_name'], '测试任务')
                self.assertEqual(df.loc[0, 'owner'], '张三')
                self.assertEqual(df.loc[0, 'priority'], 'High')
                self.assertEqual(df.loc[0, 'source_file'], 'test_meeting.txt')
                self.assertTrue('due_date' in df.columns)
                
                # 验证 MD 包含的报告内容和原始片段
                with open(os.path.join(test_output, md_file), 'r', encoding='utf-8') as f_md:
                    md_text = f_md.read()
                    self.assertIn("test_meeting.txt", md_text)
                    self.assertIn("测试汇总", md_text)
                    self.assertIn("测试任务", md_text)
                    self.assertIn("张三", md_text)
                    self.assertIn("四、会议原始文本摘要", md_text)
                    self.assertIn("张三昨天直接把代码 push 到了 master", md_text)


if __name__ == "__main__":
    unittest.main()
