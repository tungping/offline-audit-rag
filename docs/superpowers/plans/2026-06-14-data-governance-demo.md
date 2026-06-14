# Data Governance Demo Audit Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a demo-ready data governance audit layer that detects sensitive data, vague wording, SOP gaps, and cross-department risk, then writes a risk CSV and enhanced Markdown report.

**Architecture:** Keep `app.py` as the workflow owner and add `audit_rules.py` as a deterministic local rules module. The existing RAG and Ollama flow continues to extract tasks and audit summaries, while the new rules module produces stable risk items that are merged into the final CSV and Markdown outputs.

**Tech Stack:** Python 3.10+, standard library regex, pandas, existing Ollama/ChromaDB workflow, unittest/pytest via `uv run pytest`.

---

## File Structure

- Create: `audit_rules.py`
  - Owns deterministic governance checks and masking.
  - Exposes `build_risk_items(text, tasks, source_file)` as the main integration point.
- Modify: `app.py`
  - Imports `audit_rules`.
  - Calls `build_risk_items(...)` after task extraction.
  - Writes `*_tasks.csv`, `*_risk_items.csv`, and `*_audit_report.md`.
  - Adds a Markdown section for data governance risk items.
- Modify: `tests/test_app.py`
  - Adds focused unit tests for deterministic rules.
  - Updates the `process_file` integration test to expect three outputs and masked risk evidence.
- Modify: `README.md`
  - Updates feature and output descriptions so the demo story matches the new behavior.

## Task 1: Add Deterministic Rule Tests

**Files:**
- Modify: `tests/test_app.py`
- Create later: `audit_rules.py`

- [ ] **Step 1: Add import for the new rules module**

Add this import near the existing imports in `tests/test_app.py`:

```python
import audit_rules
```

- [ ] **Step 2: Write failing tests for sensitive-data masking**

Add this method to `AppTests`:

```python
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
```

- [ ] **Step 3: Write failing tests for vague wording**

Add this method to `AppTests`:

```python
    def test_build_risk_items_detects_ambiguous_phrases(self):
        text = "研发后续跟进数据导出脚本，相关人员尽快处理。"

        risks = audit_rules.build_risk_items(text, [], "demo.txt")

        vague_risks = [item for item in risks if item["risk_type"] == "模糊表述"]
        evidence = {item["evidence_masked"] for item in vague_risks}
        self.assertIn("后续跟进", evidence)
        self.assertIn("相关人员", evidence)
        self.assertIn("尽快", evidence)
        self.assertTrue(all(item["severity"] == "Medium" for item in vague_risks))
```

- [ ] **Step 4: Write failing tests for SOP gaps**

Add this method to `AppTests`:

```python
    def test_build_risk_items_detects_sop_gaps_for_unassigned_task(self):
        text = "数据导出脚本需要处理，完成后同步业务方。"
        tasks = [{"task_name": "处理数据导出脚本", "owner": "Unassigned", "priority": "High"}]

        risks = audit_rules.build_risk_items(text, tasks, "demo.txt")

        sop_risks = [item for item in risks if item["risk_type"] == "SOP缺失"]
        evidence = {item["evidence_masked"] for item in sop_risks}
        self.assertIn("任务缺少明确负责人: 处理数据导出脚本", evidence)
        self.assertIn("缺少明确截止时间", evidence)
        self.assertIn("缺少验收标准或交付物定义", evidence)
```

- [ ] **Step 5: Write failing tests for cross-department risk**

Add this method to `AppTests`:

```python
    def test_build_risk_items_detects_cross_department_risk_without_confirmer(self):
        text = "产品、法务、研发和销售一起看一下客户数据导出方案，没问题就上线。"

        risks = audit_rules.build_risk_items(text, [], "demo.txt")

        collaboration_risks = [item for item in risks if item["risk_type"] == "跨部门协作风险"]
        self.assertEqual(len(collaboration_risks), 1)
        self.assertIn("产品、研发、法务、销售", collaboration_risks[0]["evidence_masked"])
        self.assertEqual(collaboration_risks[0]["severity"], "Medium")
```

- [ ] **Step 6: Run the new rule tests and verify they fail**

Run:

```bash
uv run pytest tests/test_app.py -k "risk_items or ambiguous or sop_gaps or cross_department" -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'audit_rules'`.

## Task 2: Implement `audit_rules.py`

**Files:**
- Create: `audit_rules.py`
- Test: `tests/test_app.py`

- [ ] **Step 1: Create the deterministic rules module**

Create `audit_rules.py` with this implementation:

```python
import re
from typing import Any

RiskItem = dict[str, Any]

MOBILE_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
EMAIL_PATTERN = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
ID_CARD_PATTERN = re.compile(r"(?<!\d)(\d{6}\d{8}\d{3}[\dXx])(?!\d)")

AMBIGUOUS_PHRASES = ["尽快", "后续跟进", "相关人员", "看情况", "有空处理", "ASAP"]
DEPARTMENT_KEYWORDS = ["产品", "研发", "法务", "销售", "财务", "运营", "客服"]
CONFIRMATION_KEYWORDS = ["确认人", "审批人", "负责人", "Owner", "owner"]
ACCEPTANCE_KEYWORDS = ["验收", "标准", "完成定义", "交付物"]
DATE_PATTERN = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|"
    r"\d{1,2}月\d{1,2}日|"
    r"今天|明天|后天|本周|下周|月底|下月底|"
    r"周[一二三四五六日天])"
)


def mask_sensitive_value(value: str, risk_type: str) -> str:
    if risk_type == "手机号":
        return f"{value[:3]}****{value[-4:]}"
    if risk_type == "邮箱":
        local, domain = value.split("@", 1)
        if len(local) <= 2:
            masked_local = local[0] + "*"
        else:
            masked_local = local[0] + ("*" * (len(local) - 2)) + local[-1]
        return f"{masked_local}@{domain}"
    if risk_type == "身份证":
        return f"{value[:6]}********{value[-4:]}"
    return value


def _risk_item(
    risk_type: str,
    severity: str,
    evidence_masked: str,
    recommendation: str,
    manual_review_required: bool,
    source_file: str,
) -> RiskItem:
    return {
        "risk_type": risk_type,
        "severity": severity,
        "evidence_masked": evidence_masked,
        "recommendation": recommendation,
        "manual_review_required": manual_review_required,
        "source_file": source_file,
    }


def detect_sensitive_info(text: str, source_file: str) -> list[RiskItem]:
    risks: list[RiskItem] = []
    patterns = [
        ("手机号", MOBILE_PATTERN),
        ("邮箱", EMAIL_PATTERN),
        ("身份证", ID_CARD_PATTERN),
    ]
    for label, pattern in patterns:
        seen: set[str] = set()
        for match in pattern.finditer(text):
            raw_value = match.group(1)
            if raw_value in seen:
                continue
            seen.add(raw_value)
            risks.append(
                _risk_item(
                    risk_type="敏感信息",
                    severity="High",
                    evidence_masked=mask_sensitive_value(raw_value, label),
                    recommendation="删除或脱敏后再流转，并确认共享范围是否合规。",
                    manual_review_required=True,
                    source_file=source_file,
                )
            )
    return risks


def detect_ambiguous_phrases(text: str, source_file: str) -> list[RiskItem]:
    risks: list[RiskItem] = []
    for phrase in AMBIGUOUS_PHRASES:
        if phrase in text:
            risks.append(
                _risk_item(
                    risk_type="模糊表述",
                    severity="Medium",
                    evidence_masked=phrase,
                    recommendation="将模糊表述改为明确负责人、截止时间和交付标准。",
                    manual_review_required=False,
                    source_file=source_file,
                )
            )
    return risks


def detect_sop_gaps(tasks: list[dict[str, Any]], text: str, source_file: str) -> list[RiskItem]:
    risks: list[RiskItem] = []
    for task in tasks:
        owner = str(task.get("owner", "")).strip()
        task_name = str(task.get("task_name", "未知任务")).strip() or "未知任务"
        if not owner or owner == "Unassigned":
            risks.append(
                _risk_item(
                    risk_type="SOP缺失",
                    severity="Medium",
                    evidence_masked=f"任务缺少明确负责人: {task_name}",
                    recommendation="为任务补充唯一负责人，并在任务系统中完成指派。",
                    manual_review_required=False,
                    source_file=source_file,
                )
            )

    if not DATE_PATTERN.search(text):
        risks.append(
            _risk_item(
                risk_type="SOP缺失",
                severity="Medium",
                evidence_masked="缺少明确截止时间",
                recommendation="补充明确日期或相对时间窗口，例如本周五或2026-06-30。",
                manual_review_required=False,
                source_file=source_file,
            )
        )

    if not any(keyword in text for keyword in ACCEPTANCE_KEYWORDS):
        risks.append(
            _risk_item(
                risk_type="SOP缺失",
                severity="Medium",
                evidence_masked="缺少验收标准或交付物定义",
                recommendation="补充验收标准、完成定义或交付物清单。",
                manual_review_required=False,
                source_file=source_file,
            )
        )
    return risks


def detect_cross_department_risks(text: str, source_file: str) -> list[RiskItem]:
    matched_departments = [keyword for keyword in DEPARTMENT_KEYWORDS if keyword in text]
    if len(matched_departments) < 2:
        return []
    if any(keyword in text for keyword in CONFIRMATION_KEYWORDS):
        return []
    return [
        _risk_item(
            risk_type="跨部门协作风险",
            severity="Medium",
            evidence_masked="涉及多个部门但缺少确认人: " + "、".join(matched_departments),
            recommendation="指定跨部门确认人或审批人，并记录确认结论。",
            manual_review_required=False,
            source_file=source_file,
        )
    ]


def build_risk_items(text: str, tasks: list[dict[str, Any]], source_file: str) -> list[RiskItem]:
    risks: list[RiskItem] = []
    risks.extend(detect_sensitive_info(text, source_file))
    risks.extend(detect_ambiguous_phrases(text, source_file))
    risks.extend(detect_sop_gaps(tasks, text, source_file))
    risks.extend(detect_cross_department_risks(text, source_file))
    return risks
```

- [ ] **Step 2: Run deterministic rule tests**

Run:

```bash
uv run pytest tests/test_app.py -k "risk_items or ambiguous or sop_gaps or cross_department" -v
```

Expected: PASS for the new rule tests.

- [ ] **Step 3: Commit deterministic rules**

Run:

```bash
git add audit_rules.py tests/test_app.py
git commit -m "feat: add deterministic governance risk rules"
```

## Task 3: Integrate Risk CSV and Markdown Report

**Files:**
- Modify: `app.py`
- Modify: `tests/test_app.py`

- [ ] **Step 1: Update the process-file success test to expect the demo audit package**

In `test_process_file_success`, update the mocked model task stream so the input includes governance risk text:

```python
f.write(
    "张三昨天直接把代码 push 到了 master，没有经过 Review。"
    "客户手机号 13812345678 和邮箱 zhangsan@example.com 需要发给销售团队。"
    "研发后续跟进，相关人员尽快处理。"
)
```

Then replace the output-file assertions with:

```python
                out_files = os.listdir(test_output)
                self.assertEqual(len(out_files), 3)

                csv_file = [f for f in out_files if f.endswith('_tasks.csv')][0]
                risk_csv_file = [f for f in out_files if f.endswith('_risk_items.csv')][0]
                md_file = [f for f in out_files if f.endswith('_audit_report.md')][0]
```

After the task CSV assertions, add:

```python
                risk_df = pd.read_csv(os.path.join(test_output, risk_csv_file))
                self.assertTrue({"risk_type", "severity", "evidence_masked", "recommendation", "manual_review_required"}.issubset(risk_df.columns))
                risk_evidence = " ".join(str(value) for value in risk_df["evidence_masked"])
                self.assertIn("138****5678", risk_evidence)
                self.assertIn("z******n@example.com", risk_evidence)
                self.assertNotIn("13812345678", risk_evidence)
                self.assertNotIn("zhangsan@example.com", risk_evidence)
```

Update the Markdown assertions with:

```python
                    self.assertIn("四、数据合规与流程治理风险", md_text)
                    self.assertIn("五、会议原始文本摘要", md_text)
                    self.assertIn("138****5678", md_text)
                    self.assertIn("z******n@example.com", md_text)
                    self.assertNotIn("13812345678", md_text)
                    self.assertNotIn("zhangsan@example.com", md_text)
```

- [ ] **Step 2: Run the updated integration test and verify it fails**

Run:

```bash
uv run pytest tests/test_app.py::AppTests::test_process_file_success -v
```

Expected: FAIL because `process_file` still writes two files and does not create `*_risk_items.csv`.

- [ ] **Step 3: Import the rules module in `app.py`**

Add this import near the existing third-party imports in `app.py`:

```python
import audit_rules
```

- [ ] **Step 4: Build risk items after task cleanup**

After `df["source_file"] = os.path.basename(file_path)`, add:

```python
        source_file = os.path.basename(file_path)
        risk_items = audit_rules.build_risk_items(
            content,
            data["tasks"],
            source_file,
        )
        risk_df = pd.DataFrame(risk_items)
        for column, default in {
            "risk_type": "",
            "severity": "Low",
            "evidence_masked": "",
            "recommendation": "",
            "manual_review_required": False,
            "source_file": source_file,
        }.items():
            if column not in risk_df.columns:
                risk_df[column] = default
        risk_df["audit_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
```

Then replace the existing `df["source_file"]` assignment with:

```python
        source_file = os.path.basename(file_path)
        df["source_file"] = source_file
```

- [ ] **Step 5: Rename task CSV output and add risk CSV output**

Replace the existing CSV path block with:

```python
        task_csv_path = unique_file_path(
            os.path.join(OUTPUT, f"{base_name}_{time_suffix}_tasks.csv")
        )
        df.to_csv(task_csv_path, index=False, encoding="utf-8-sig")

        risk_csv_path = unique_file_path(
            os.path.join(OUTPUT, f"{base_name}_{time_suffix}_risk_items.csv")
        )
        risk_df.to_csv(risk_csv_path, index=False, encoding="utf-8-sig")
```

- [ ] **Step 6: Add the governance-risk Markdown section and final report numbering**

After the task table loop and before the raw text excerpt, add:

```python
        md_content += """
## 四、数据合规与流程治理风险

"""
        if risk_df.empty:
            md_content += "> 未检测到确定性数据治理风险项。\n"
        else:
            md_content += """| 序号 | 风险类型 | 等级 | 证据 | 整改建议 | 人工复核 |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""
            for idx, (_, row) in enumerate(risk_df.iterrows(), 1):
                manual_review = "是" if bool(row.get("manual_review_required")) else "否"
                md_content += (
                    f"| {idx} | {markdown_table_cell(row.get('risk_type'))} | "
                    f"{markdown_table_cell(row.get('severity'))} | "
                    f"{markdown_table_cell(row.get('evidence_masked'))} | "
                    f"{markdown_table_cell(row.get('recommendation'))} | "
                    f"{manual_review} |\n"
                )
```

Then rename the existing raw-text section heading from:

```markdown
## 四、会议原始文本摘要
```

to:

```markdown
## 五、会议原始文本摘要
```

- [ ] **Step 7: Rename Markdown report output**

Replace the existing Markdown path block with:

```python
        md_path = unique_file_path(
            os.path.join(OUTPUT, f"{base_name}_{time_suffix}_audit_report.md")
        )
```

- [ ] **Step 8: Run the integration test**

Run:

```bash
uv run pytest tests/test_app.py::AppTests::test_process_file_success -v
```

Expected: PASS.

- [ ] **Step 9: Run all tests**

Run:

```bash
uv run pytest
```

Expected: PASS.

- [ ] **Step 10: Commit integration**

Run:

```bash
git add app.py tests/test_app.py
git commit -m "feat: output governance risk audit package"
```

## Task 4: Update README Demo Story

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the feature list**

Add this bullet after the RAG feature bullet:

```markdown
- 🧭 **数据治理风险检查**：自动识别手机号、邮箱、身份证等敏感信息，以及模糊表述、SOP 缺口、跨部门责任不清和人工复核项。
```

- [ ] **Step 2: Update the output description**

Replace this bullet:

```markdown
- 📄 **双格式输出**：审计结果同时生成 `.csv` 任务指派表（可直接导入项目管理工具）与 `.md` 审计报告。
```

with:

```markdown
- 📄 **审计包输出**：同时生成任务指派 CSV、风险项 CSV 与 Markdown 审计报告，方便演示企业内部数据合规和流程治理场景。
```

- [ ] **Step 3: Update the workflow output paragraph**

Replace this sentence:

```markdown
   - 结合 `qwen3.5:9b` 模型推理进行合规审计并提取具体待办任务。
   - 在 `output/` 下生成同名的 `.csv` 任务指派表和 `.md` 审计报告。
```

with:

```markdown
   - 结合 `qwen3.5:9b` 模型推理进行合规审计并提取具体待办任务。
   - 使用本地规则模块识别敏感信息、模糊表述、SOP 缺口和跨部门协作风险。
   - 在 `output/` 下生成任务指派 CSV、风险项 CSV 和 Markdown 审计报告。
```

- [ ] **Step 4: Add demo input section**

Add this section before `## 🎵 支持的音频格式`:

````markdown
## 🧪 数据治理演示样例

可将以下文本保存为 `inbox/demo_meeting.txt` 触发审计：

```text
今天会议决定把客户张女士的手机号 13812345678 和邮箱 zhangsan@example.com 发给销售团队。
研发后续尽快处理数据导出脚本，相关人员负责。
产品和法务一起看一下，没问题就上线。
```

系统会在报告和风险 CSV 中标出明文敏感信息、模糊表述、SOP 缺口、跨部门协作风险，并对手机号和邮箱做脱敏展示。
````

- [ ] **Step 5: Run README-focused smoke check**

Run:

```bash
uv run pytest
```

Expected: PASS.

- [ ] **Step 6: Commit README update**

Run:

```bash
git add README.md
git commit -m "docs: describe governance audit demo"
```

## Task 5: Final Verification

**Files:**
- Read: `git status`
- Test: all touched Python files

- [ ] **Step 1: Run the full test suite**

Run:

```bash
uv run pytest
```

Expected: PASS.

- [ ] **Step 2: Check formatting-relevant diagnostics with existing toolchain**

Run:

```bash
uv run python -m py_compile app.py audit_rules.py tests/test_app.py
```

Expected: exit code 0.

- [ ] **Step 3: Check working tree**

Run:

```bash
git status --short
```

Expected: no unstaged implementation changes after the planned commits.

## Self-Review

- Spec coverage: The plan covers deterministic sensitive-data detection, masking, vague wording, SOP gaps, cross-department risk, manual review flags, risk CSV output, Markdown report enhancement, README demo documentation, and tests.
- Scope: The plan stays within the demo version and does not add dashboards, databases, UI changes, or new dependencies.
- Type consistency: `build_risk_items(text, tasks, source_file)` returns `list[dict[str, Any]]`; `app.py` converts this directly into `risk_df`.
- Output consistency: The implementation plan standardizes names as `*_tasks.csv`, `*_risk_items.csv`, and `*_audit_report.md`.
