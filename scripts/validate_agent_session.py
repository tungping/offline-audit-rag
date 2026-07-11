import argparse
import json
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.session_validator import validate_session_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a persisted agent session bundle")
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_session_bundle(args.session_dir)
    if args.as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"session={report.session_id or '-'} workspace={report.workspace or '-'} "
            f"status={report.status or '-'} valid={str(report.valid).lower()}"
        )
        for issue in (*report.errors, *report.warnings):
            print(f"{issue.code}: {issue.message}")
    return 0 if report.valid else 2


if __name__ == "__main__":
    sys.exit(main())
