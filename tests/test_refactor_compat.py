import app
from audit_core import config, file_ops, formatting, models, text_processing


def test_app_reexports_core_types_and_helpers():
    assert app.ProcessResult is models.ProcessResult
    assert app.COMPLIANCE_MODE == config.COMPLIANCE_MODE
    assert app.SEMICONDUCTOR_IP_MODE == config.SEMICONDUCTOR_IP_MODE
    assert app.normalize_audit_mode(" semiconductor_ip ") == "semiconductor_ip"
    assert app.count_tokens("中文 alpha beta") == text_processing.count_tokens(
        "中文 alpha beta"
    )
    assert app.extract_json_object('{"ok": true}') == formatting.extract_json_object(
        '{"ok": true}'
    )
    assert app.unique_file_path is file_ops.unique_file_path


def test_core_split_preserves_process_result_defaults():
    result = models.ProcessResult(success=True)
    assert result.success is True
    assert result.mode == "compliance"
    assert result.cancelled is False
