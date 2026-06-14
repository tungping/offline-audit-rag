import re
from typing import Any


RiskItem = dict[str, Any]

MOBILE_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
EMAIL_PATTERN = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
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


def detect_sensitive_info(text: str, source_file: str) -> list[dict[str, Any]]:
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

    if not DATE_TIME_PATTERN.search(text):
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


def build_risk_items(
    text: str,
    tasks: list[dict[str, Any]],
    source_file: str,
) -> list[dict[str, Any]]:
    return [
        *detect_sensitive_info(text, source_file),
        *detect_ambiguous_phrases(text, source_file),
        *detect_sop_gaps(tasks, text, source_file),
        *detect_cross_department_risks(text, source_file),
    ]
