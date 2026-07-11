import json
import os

import pandas as pd

from .config import AUDIT_HISTORY_FILENAME, COMPLIANCE_MODE, OUTPUT
from .formatting import _truthy, mask_markdown_text


def record_audit_history(
    source_file: str,
    audit_time: str,
    task_output_df: pd.DataFrame,
    risk_output_df: pd.DataFrame,
    tasks_csv_path: str,
    risk_csv_path: str,
    report_path: str,
    mode: str = COMPLIANCE_MODE,
    *,
    output_dir: str = OUTPUT,
) -> None:
    history_path = os.path.join(output_dir, AUDIT_HISTORY_FILENAME)
    severity_counts = (
        risk_output_df["severity"].value_counts().to_dict()
        if "severity" in risk_output_df.columns
        else {}
    )
    risk_type_counts = (
        risk_output_df["risk_type"].value_counts().to_dict()
        if "risk_type" in risk_output_df.columns
        else {}
    )
    review_column = ""
    if "manual_review_required" in risk_output_df.columns:
        review_column = "manual_review_required"
    elif "needs_human_review" in risk_output_df.columns:
        review_column = "needs_human_review"
    manual_review_count = (
        int(risk_output_df[review_column].map(_truthy).sum())
        if review_column
        else 0
    )

    entry = {
        "audit_time": audit_time,
        "mode": mode,
        "source_file": mask_markdown_text(source_file),
        "task_count": int(len(task_output_df)),
        "risk_count": int(len(risk_output_df)),
        "high_risk_count": int(severity_counts.get("High", 0)),
        "manual_review_count": manual_review_count,
        "severity_counts": {
            str(key): int(value) for key, value in severity_counts.items()
        },
        "risk_type_counts": {
            str(key): int(value) for key, value in risk_type_counts.items()
        },
        "tasks_csv_path": os.path.basename(tasks_csv_path),
        "risk_csv_path": os.path.basename(risk_csv_path),
        "report_path": os.path.basename(report_path),
    }
    with open(history_path, "a", encoding="utf-8") as history_file:
        history_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
