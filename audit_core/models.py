from dataclasses import dataclass

from .config import COMPLIANCE_MODE


@dataclass(frozen=True)
class ProcessResult:
    success: bool
    tasks_csv_path: str = ""
    risk_csv_path: str = ""
    report_path: str = ""
    cancelled: bool = False
    mode: str = COMPLIANCE_MODE
