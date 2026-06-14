import re
from typing import Any


RiskItem = dict[str, Any]

MOBILE_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+\-*])"
    r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    r"(?![A-Za-z0-9._%+\-*])"
)
ID_CARD_PATTERN = re.compile(r"(?<!\d)(\d{6}\d{8}\d{3}[\dXx])(?!\d)")
DATE_TIME_PATTERN = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|"
    r"\d{1,2}[-/月]\d{1,2}日?|"
    r"\d{1,2}:\d{2}|"
    r"\d{1,2}[点时]|"
    r"\d+\s*(天|小时|周)内|"
    r"今天|明天|后天|本周|下周|月底|下月底|"
    r"周[一二三四五六日天])"
)

AMBIGUOUS_PHRASES = ["尽快", "后续跟进", "相关人员", "看情况", "有空处理", "ASAP"]
DEPARTMENT_KEYWORDS = ["产品", "研发", "法务", "销售", "财务", "运营", "客服"]
CONFIRMATION_KEYWORDS = ["确认人", "审批人", "负责人", "Owner", "owner"]
ACCEPTANCE_KEYWORDS = ["验收", "标准", "完成定义", "交付物"]
TASK_DEADLINE_FIELDS = ["deadline", "due_date", "due_time", "截止时间", "完成时间"]


def mask_sensitive_value(value: str, risk_type: str) -> str:
    if risk_type == "手机号":
        return f"{value[:3]}****{value[-4:]}"
    if risk_type == "邮箱":
        local, domain = value.split("@", 1)
        if len(local) <= 1:
            masked_local = local[0] + "*"
        elif len(local) == 2:
            masked_local = local[0] + "*"
        else:
            masked_local = local[0] + ("*" * (len(local) - 2)) + local[-1]
        return f"{masked_local}@{domain}"
    if risk_type == "身份证":
        return f"{value[:6]}********{value[-4:]}"
    return value


def mask_sensitive_evidence(evidence: str) -> str:
    masking_patterns = [
        ("邮箱", EMAIL_PATTERN),
        ("身份证", ID_CARD_PATTERN),
        ("手机号", MOBILE_PATTERN),
    ]
    masked = evidence
    for label, pattern in masking_patterns:
        masked = pattern.sub(
            lambda match, label=label: mask_sensitive_value(match.group(1), label),
            masked,
        )
    return masked


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
        "evidence_masked": mask_sensitive_evidence(evidence_masked),
        "recommendation": recommendation,
        "manual_review_required": manual_review_required,
        "source_file": source_file,
    }


def detect_sensitive_info(
    text: str,
    source_file: str,
    sensitive_entities: list[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
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

    if sensitive_entities:
        seen_entities: set[str] = set()
        for entity in sensitive_entities:
            e_type = entity.get("entity_type", "敏感信息")
            e_val = str(entity.get("entity_value", "")).strip()
            if not e_val or e_val in seen_entities:
                continue
            seen_entities.add(e_val)

            masked_val = e_val
            if len(e_val) >= 2:
                masked_val = e_val[0] + "*" * (len(e_val) - 1)

            risks.append(
                _risk_item(
                    risk_type="敏感信息",
                    severity="High",
                    evidence_masked=f"{e_type}: {masked_val}",
                    recommendation=f"涉及{e_type}，请评估是否符合内部安全脱敏规范。",
                    manual_review_required=True,
                    source_file=source_file,
                )
            )

    return risks


def detect_ambiguous_phrases(text: str, source_file: str) -> list[dict[str, Any]]:
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


def _split_sentences(text: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", text)
        if sentence.strip()
    ]


def _task_contexts(task: dict[str, Any], text: str) -> list[str]:
    owner = str(task.get("owner", "")).strip()
    task_name = str(task.get("task_name", "")).strip()
    anchors = [anchor for anchor in (owner, task_name) if anchor and anchor != "Unassigned"]
    task_fragments = [
        fragment
        for fragment in re.split(r"[\s,，。！？!?；;、/]+", task_name)
        if len(fragment) >= 2
    ]
    anchors.extend(task_fragments)

    contexts = []
    for sentence in _split_sentences(text):
        if any(anchor and anchor in sentence for anchor in anchors):
            contexts.append(sentence)
    return contexts


def _task_has_deadline(task: dict[str, Any], text: str) -> bool:
    for field in TASK_DEADLINE_FIELDS:
        value = str(task.get(field, "")).strip()
        if value and value.lower() not in {"nan", "none", "null", "无", "未定"}:
            return True

    return any(DATE_TIME_PATTERN.search(context) for context in _task_contexts(task, text))


def detect_sop_gaps(
    tasks: list[dict[str, Any]],
    text: str,
    source_file: str,
) -> list[dict[str, Any]]:
    risks: list[RiskItem] = []

    for task in tasks:
        owner = str(task.get("owner", "")).strip()
        if not owner or owner == "Unassigned":
            task_name = str(task.get("task_name", "")).strip() or "未命名任务"
            risks.append(
                _risk_item(
                    risk_type="SOP缺失",
                    severity="Medium",
                    evidence_masked=f"任务缺少明确负责人: {task_name}",
                    recommendation="补充明确负责人、截止时间和验收标准。",
                    manual_review_required=False,
                    source_file=source_file,
                )
            )

    if tasks:
        missing_deadline_tasks = []
        for task in tasks:
            if not _task_has_deadline(task, text):
                task_name = str(task.get("task_name", "")).strip() or "未命名任务"
                missing_deadline_tasks.append(task_name)
                risks.append(
                    _risk_item(
                        risk_type="SOP缺失",
                        severity="Medium",
                        evidence_masked=f"任务缺少明确截止时间: {task_name}",
                        recommendation="补充明确负责人、截止时间和验收标准。",
                        manual_review_required=False,
                        source_file=source_file,
                    )
                )
        if missing_deadline_tasks and not DATE_TIME_PATTERN.search(text):
            risks.append(
                _risk_item(
                    risk_type="SOP缺失",
                    severity="Medium",
                    evidence_masked="缺少明确截止时间",
                    recommendation="补充明确负责人、截止时间和验收标准。",
                    manual_review_required=False,
                    source_file=source_file,
                )
            )
    elif not DATE_TIME_PATTERN.search(text):
        risks.append(
            _risk_item(
                risk_type="SOP缺失",
                severity="Medium",
                evidence_masked="缺少明确截止时间",
                recommendation="补充明确负责人、截止时间和验收标准。",
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
                recommendation="补充明确负责人、截止时间和验收标准。",
                manual_review_required=False,
                source_file=source_file,
            )
        )

    return risks


def detect_cross_department_risks(text: str, source_file: str) -> list[dict[str, Any]]:
    matched_departments = [
        department for department in DEPARTMENT_KEYWORDS if department in text
    ]
    has_confirmation = any(keyword in text for keyword in CONFIRMATION_KEYWORDS)

    if len(matched_departments) < 2 or has_confirmation:
        return []

    return [
        _risk_item(
            risk_type="跨部门协作风险",
            severity="Medium",
            evidence_masked="涉及多个部门但缺少确认人: "
            + "、".join(matched_departments),
            recommendation="指定跨部门确认人或审批人，并记录确认结论。",
            manual_review_required=False,
            source_file=source_file,
        )
    ]


def detect_model_uncertainty(
    model_confidence: str,
    uncertainty_reason: str,
    source_file: str,
) -> list[dict[str, Any]]:
    if str(model_confidence).lower() in ("medium", "low"):
        reason = uncertainty_reason or "模型在审计或提取时置信度较低，可能存在信息模糊。"
        return [
            _risk_item(
                risk_type="模型判定不确定",
                severity="Medium",
                evidence_masked=reason,
                recommendation="由于上下文信息不完整或条款冲突，建议人工复核大模型审计结果的准确性。",
                manual_review_required=True,
                source_file=source_file,
            )
        ]
    return []


def build_risk_items(
    text: str,
    tasks: list[dict[str, Any]],
    source_file: str,
    sensitive_entities: list[dict[str, Any]] = None,
    model_confidence: str = "High",
    uncertainty_reason: str = "",
) -> list[dict[str, Any]]:
    return [
        *detect_sensitive_info(text, source_file, sensitive_entities),
        *detect_ambiguous_phrases(text, source_file),
        *detect_sop_gaps(tasks, text, source_file),
        *detect_cross_department_risks(text, source_file),
        *detect_model_uncertainty(model_confidence, uncertainty_reason, source_file),
    ]
