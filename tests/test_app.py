import os
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
