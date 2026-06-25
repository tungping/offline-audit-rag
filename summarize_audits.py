#!/usr/bin/env python3
import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


HISTORY_FILENAME = "audit_history.jsonl"


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


def _read_history_entries(output_dir: str | Path) -> list[dict[str, Any]]:
    history_path = Path(output_dir) / HISTORY_FILENAME
    if not history_path.exists():
        return []

    entries = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def summarize_history(output_dir: str | Path) -> dict[str, Any]:
    entries = _read_history_entries(output_dir)
    risk_type_counts: Counter[str] = Counter()
    severity_counts: Counter[str] = Counter()

    for entry in entries:
        risk_type_counts.update(entry.get("risk_type_counts", {}))
        severity_counts.update(entry.get("severity_counts", {}))

    return {
        "audit_count": len(entries),
        "task_count": int(sum(entry.get("task_count", 0) for entry in entries)),
        "risk_count": int(sum(entry.get("risk_count", 0) for entry in entries)),
        "high_risk_count": int(sum(entry.get("high_risk_count", 0) for entry in entries)),
        "manual_review_count": int(sum(entry.get("manual_review_count", 0) for entry in entries)),
        "risk_type_counts": dict(risk_type_counts),
        "severity_counts": dict(severity_counts),
        "recent_entries": entries[-10:][::-1],
    }


def render_history_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Audit History Summary",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "| :--- | ---: |",
        f"| Audit runs | {summary['audit_count']} |",
        f"| Tasks extracted | {summary['task_count']} |",
        f"| Risks detected | {summary['risk_count']} |",
        f"| High risks | {summary['high_risk_count']} |",
        f"| Manual review items | {summary['manual_review_count']} |",
        "",
        _markdown_count_table("Risk Type Breakdown", summary["risk_type_counts"]),
        "",
        _markdown_count_table("Severity Breakdown", summary["severity_counts"]),
        "",
        "## Recent Audits",
        "",
        "| Source | Audit time | Tasks | Risks | High risks | Manual review |",
        "| :--- | :--- | ---: | ---: | ---: | ---: |",
    ]
    if not summary["recent_entries"]:
        lines.append("| None | - | 0 | 0 | 0 | 0 |")
    else:
        for entry in summary["recent_entries"]:
            lines.append(
                f"| {entry.get('source_file', '')} | {entry.get('audit_time', '')} | "
                f"{int(entry.get('task_count', 0))} | {int(entry.get('risk_count', 0))} | "
                f"{int(entry.get('high_risk_count', 0))} | {int(entry.get('manual_review_count', 0))} |"
            )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Offline Auto Audit CSV outputs.")
    parser.add_argument("--output-dir", default="output", help="Directory containing audit CSV outputs.")
    parser.add_argument("--write", help="Optional markdown path to write the summary report.")
    parser.add_argument("--history", action="store_true", help="Summarize audit_history.jsonl instead of scanning CSV files.")
    args = parser.parse_args()

    if args.history:
        summary = summarize_history(args.output_dir)
        markdown = render_history_markdown(summary)
    else:
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
