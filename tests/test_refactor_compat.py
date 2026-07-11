import agent_cli
import app
from audit_core import artifacts, config, file_ops, formatting, history, models, pipeline, text_processing
from audit_core import knowledge_base, model_io
from capabilities.patent_research import legacy_analysis
from unittest import mock


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


def test_app_reexports_knowledge_and_model_adapters():
    assert app.initialize_knowledge_base is knowledge_base.initialize_knowledge_base
    assert app.retrieve_relevant_context is knowledge_base.retrieve_relevant_context
    assert app.check_ollama_status is model_io.check_ollama_status


def test_generate_json_stream_uses_injected_generator():
    generator = mock.Mock(return_value=[{"response": '{"ok": true}'}])
    result = model_io.generate_json_stream(
        model="demo",
        system="system",
        prompt="prompt",
        options={"temperature": 0.1},
        generate=generator,
    )
    assert result == {"ok": True}
    generator.assert_called_once()


def test_generate_json_stream_forwards_structured_generation_options():
    generator = mock.Mock(return_value=[{"response": '{"ok": true}'}])

    result = model_io.generate_json_stream(
        model="demo",
        system="system",
        prompt="prompt",
        options={"temperature": 0.1},
        think=False,
        response_format="json",
        generate=generator,
    )

    assert result == {"ok": True}
    generator.assert_called_once_with(
        model="demo",
        system="system",
        prompt="prompt",
        options={"temperature": 0.1},
        stream=True,
        think=False,
        format="json",
    )


def test_agent_json_generation_is_bounded_and_disables_thinking():
    with mock.patch.object(
        agent_cli, "generate_json_stream", return_value={"ok": True}
    ) as generate:
        assert agent_cli._ollama_json("system", "prompt") == {"ok": True}

    kwargs = generate.call_args.kwargs
    assert kwargs["think"] is False
    assert kwargs["response_format"] == "json"
    assert kwargs["options"]["num_predict"] == 512


def test_app_reexports_legacy_semiconductor_analysis():
    assert (
        app.validate_semiconductor_ip_result
        is legacy_analysis.validate_semiconductor_ip_result
    )
    assert (
        app.build_semiconductor_ip_system_prompt
        is legacy_analysis.build_semiconductor_ip_system_prompt
    )
    assert (
        app.write_semiconductor_ip_outputs
        is legacy_analysis.write_semiconductor_ip_outputs
    )


def test_app_reexports_classic_pipeline():
    assert app.process_file_with_result is pipeline.process_file_with_result
    assert app.process_file is pipeline.process_file
    assert app.record_audit_history is history.record_audit_history


def test_classic_cli_modes_remain_supported():
    assert app.parse_args([]).mode == "compliance"
    assert app.parse_args(["--mode", "semiconductor_ip"]).mode == "semiconductor_ip"
