# Data Governance Demo Audit Module Design

## Purpose

Add a small, demo-focused data compliance and process governance layer to the existing offline RAG meeting audit workflow.

The goal is not to build a complete enterprise governance platform. The goal is to make the project demo better by turning an input meeting note, SOP, or task assignment text into a polished audit package:

- compliance audit report
- task assignment CSV
- risk item CSV
- remediation suggestions
- manual review flags

This keeps the current project positioning, but upgrades the story from "RAG technical demo" to "offline AI audit workflow prototype for internal meetings, workflows, and data compliance."

## Current Project Fit

The current project already has the right foundation:

- `app.py` watches `inbox/`, retrieves relevant compliance rules from ChromaDB, calls a local Ollama model, and writes CSV plus Markdown outputs.
- `config/compliance_rules/` stores local compliance standards.
- `tests/test_app.py` covers text splitting, JSON extraction, output generation, and RAG no-result behavior.

The new module should be added as a narrow enhancement instead of a broad rewrite.

## Recommended Approach

Use a hybrid audit design:

1. Deterministic local rule checks find obvious, demo-stable issues.
2. The existing RAG and LLM path continues to provide compliance summary and task extraction.
3. The final report merges model output with deterministic risk items.

This is better for a demo because sensitive-field detection, vague wording, and missing ownership should work consistently without depending fully on model behavior.

## Scope

### In Scope

- Detect sensitive data patterns:
  - mainland China mobile numbers
  - email addresses
  - 18-digit mainland China ID-card-like values
- Mask detected sensitive evidence before writing reports or CSV files.
- Detect vague expressions:
  - `尽快`
  - `后续跟进`
  - `相关人员`
  - `看情况`
  - `有空处理`
  - `ASAP`
- Detect basic SOP gaps:
  - task owner is missing or `Unassigned`
  - no obvious due date or time expression in the source text
  - no acceptance criteria wording, such as `验收`, `标准`, `完成定义`, or `交付物`
- Detect simple cross-department collaboration risk:
  - multiple department keywords appear, such as `研发`, `产品`, `法务`, `销售`, `财务`, `运营`, `客服`
  - no confirmation or approval owner is mentioned, such as `确认人`, `审批人`, `负责人`, or `Owner`
- Add manual review flags for high-risk items.
- Write a risk CSV beside the existing task CSV.
- Add a data governance section to the Markdown report.
- Add focused tests for masking, risk detection, and output generation.

### Out of Scope

- Full entity recognition for all customer and employee names.
- Database-backed risk history or dashboards.
- Fine-grained confidence scoring.
- UI changes.
- Multi-language compliance taxonomy.
- Replacing the current RAG prompt architecture.

## Architecture

Add one focused module:

```text
audit_rules.py
```

Suggested public functions:

```text
detect_sensitive_info(text)
detect_ambiguous_phrases(text)
detect_sop_gaps(tasks, text)
detect_cross_department_risks(text)
build_risk_items(text, tasks, source_file)
mask_sensitive_value(value, risk_type)
```

`app.py` should remain the workflow owner. It will:

1. Read input text.
2. Run the existing RAG retrieval and model generation.
3. Parse model JSON into `data`.
4. Build the task DataFrame as it does today.
5. Call `audit_rules.build_risk_items(...)`.
6. Write:
   - task CSV
   - risk item CSV
   - Markdown audit report

## Risk Item Shape

Each risk item should use simple dictionaries so it can be converted directly into a pandas DataFrame.

```json
{
  "risk_type": "敏感信息",
  "severity": "High",
  "evidence_masked": "138****5678",
  "recommendation": "删除或脱敏后再流转，并确认共享范围是否合规。",
  "manual_review_required": true,
  "source_file": "demo_meeting.txt"
}
```

Recommended CSV columns:

```text
risk_type,severity,evidence_masked,recommendation,manual_review_required,source_file,audit_time
```

## Output Design

The existing output naming can be adjusted from a generic pair to a clearer demo package:

```text
output/<base>_<timestamp>_tasks.csv
output/<base>_<timestamp>_risk_items.csv
output/<base>_<timestamp>_audit_report.md
```

The Markdown report should keep the existing RAG and task sections, then add:

```text
## 三、数据合规与流程治理风险

| 序号 | 风险类型 | 等级 | 证据 | 整改建议 | 人工复核 |
| :--- | :--- | :--- | :--- | :--- | :--- |
```

If no risk items are found, the section should state that no deterministic data governance risk was detected.

## Demo Input

A useful demo input:

```text
今天会议决定把客户张女士的手机号 13812345678 和邮箱 zhang@example.com 发给销售团队。
研发后续尽快处理数据导出脚本，相关人员负责。
产品和法务一起看一下，没问题就上线。
```

Expected risk themes:

- sensitive mobile number and email, both masked in outputs
- vague wording: `后续`, `尽快`, `相关人员`
- missing clear owner, deadline, and acceptance criteria
- cross-department collaboration without confirmation owner
- manual review required for high-risk sensitive data

## Error Handling

- Risk detection should not fail the whole audit unless the code has an unexpected programming error.
- Empty task lists should still produce SOP-level risk findings based on the raw text.
- CSV outputs should always have stable columns, even when there are no rows.
- Sensitive evidence should never be written unmasked to the risk CSV or Markdown report.

## Testing

Add focused unit tests:

- mobile, email, and ID-like values are detected and masked
- ambiguous phrases create risk items
- `Unassigned` task owner creates an SOP gap risk
- cross-department wording without confirmation owner creates a collaboration risk
- `process_file` writes three outputs and includes the risk section in Markdown
- risk CSV does not contain unmasked sensitive values

Run the relevant checks with `uv run pytest` after implementation.

## Implementation Notes

- Keep changes small and avoid refactoring unrelated workflow code.
- Prefer simple regex and keyword lists for the first demo version.
- Keep all detection local and offline.
- Do not introduce new dependencies unless tests show the standard library is insufficient.
- Preserve the current model environment variables and RAG behavior.
