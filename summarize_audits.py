#!/usr/bin/env python3
import argparse
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是"}


def _read_csv_files(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_csv(path) for path in paths]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def summarize_output_dir(output_dir: str | Path) -> dict[str, Any]:
    output_path = Path(output_dir)
    task_files = sorted(output_path.glob("*_tasks.csv"))
    risk_files = sorted(output_path.glob("*_risk_items.csv"))

    tasks_df = _read_csv_files(task_files)
    risks_df = _read_csv_files(risk_files)

    if "source_file" in risks_df.columns and not risks_df.empty:
        audited_file_count = int(risks_df["source_file"].dropna().nunique())
    else:
        audited_file_count = max(len(task_files), len(risk_files))

    severity_counts: Counter[str] = Counter()
    if "severity" in risks_df.columns:
        severity_counts.update(str(value) for value in risks_df["severity"].dropna())

    risk_type_counts: Counter[str] = Counter()
    if "risk_type" in risks_df.columns:
        risk_type_counts.update(str(value) for value in risks_df["risk_type"].dropna())

    manual_review_count = 0
    if "manual_review_required" in risks_df.columns:
        manual_review_count = int(sum(_truthy(value) for value in risks_df["manual_review_required"]))

    return {
        "audited_file_count": audited_file_count,
        "task_count": int(len(tasks_df)),
        "risk_count": int(len(risks_df)),
        "manual_review_count": manual_review_count,
        "severity_counts": dict(severity_counts),
        "risk_type_counts": dict(risk_type_counts),
    }


def _markdown_count_table(title: str, values: dict[str, int]) -> str:
    lines = [f"## {title}", "", "| Item | Count |", "| :--- | ---: |"]
    if not values:
        lines.append("| None | 0 |")
    else:
        for item, count in sorted(values.items(), key=lambda pair: (-pair[1], pair[0])):
            lines.append(f"| {item} | {count} |")
    return "\n".join(lines)


def render_summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Audit Portfolio Summary",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "| :--- | ---: |",
        f"| Audited files | {summary['audited_file_count']} |",
        f"| Tasks extracted | {summary['task_count']} |",
        f"| Risks detected | {summary['risk_count']} |",
        f"| Manual review items | {summary['manual_review_count']} |",
        "",
        _markdown_count_table("Severity Breakdown", summary["severity_counts"]),
        "",
        _markdown_count_table("Risk Type Breakdown", summary["risk_type_counts"]),
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Offline Auto Audit CSV outputs.")
    parser.add_argument("--output-dir", default="output", help="Directory containing audit CSV outputs.")
    parser.add_argument("--write", help="Optional markdown path to write the summary report.")
    args = parser.parse_args()

    summary = summarize_output_dir(args.output_dir)
    markdown = render_summary_markdown(summary)

    if args.write:
        write_path = Path(args.write)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(markdown, encoding="utf-8")
    else:
        print(markdown)


if __name__ == "__main__":
    main()
