import json
import logging

from agent.infrastructure.logging.formatter import (
    ConsoleFormatter,
    JsonLineFormatter,
)


def make_record(
    message: str = "Agent Core started",
    *,
    level: int = logging.INFO,
    event: str = "core_started",
    fields: dict | None = None,
) -> logging.LogRecord:
    """
    创建测试用 LogRecord。

    不依赖真正的 Logger，
    让 Formatter 可以独立测试。
    """

    record = logging.LogRecord(
        name="agent",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )

    record.event = event

    if fields is not None:
        record.fields = fields

    return record


def test_json_formatter_contains_required_fields() -> None:
    formatter = JsonLineFormatter(
        component="core"
    )

    record = make_record(
        fields={
            "pid": 12345,
        }
    )

    result = formatter.format(
        record
    )

    data = json.loads(result)

    assert data["schema_version"] == 1
    assert data["level"] == "INFO"
    assert data["component"] == "core"
    assert data["event"] == "core_started"
    assert data["message"] == "Agent Core started"
    assert data["pid"] == 12345

    assert data["timestamp"].endswith(
        "Z"
    )


def test_json_formatter_preserves_request_metadata() -> None:
    formatter = JsonLineFormatter(
        component="ipc"
    )

    record = make_record(
        event="ipc_request_completed",
        fields={
            "request_id": "req-001",
            "message_type": "ping",
            "duration_ms": 8,
            "success": True,
        },
    )

    data = json.loads(
        formatter.format(record)
    )

    assert data["component"] == "ipc"
    assert (
        data["request_id"]
        == "req-001"
    )
    assert (
        data["message_type"]
        == "ping"
    )
    assert data["duration_ms"] == 8
    assert data["success"] is True


def test_json_formatter_redacts_sensitive_fields() -> None:
    formatter = JsonLineFormatter(
        component="core"
    )

    record = make_record(
        fields={
            "api_key":
                "sk-super-secret123456",
            "provider":
                "openai",
        }
    )

    result = formatter.format(
        record
    )

    data = json.loads(result)

    assert (
        data["api_key"]
        == "[REDACTED]"
    )

    assert data["provider"] == "openai"

    assert (
        "sk-super-secret123456"
        not in result
    )


def test_json_formatter_redacts_secret_inside_message() -> None:
    formatter = JsonLineFormatter(
        component="core"
    )

    record = make_record(
        message=(
            "Authorization: "
            "Bearer abc.def.ghi"
        )
    )

    result = formatter.format(
        record
    )

    assert "abc.def.ghi" not in result
    assert "[REDACTED]" in result


def test_reserved_fields_cannot_be_overwritten() -> None:
    formatter = JsonLineFormatter(
        component="core"
    )

    record = make_record(
        fields={
            "schema_version": 999,
            "component": "evil",
            "level": "FAKE",
            "request_id": "req-001",
        }
    )

    data = json.loads(
        formatter.format(record)
    )

    assert data["schema_version"] == 1
    assert data["component"] == "core"
    assert data["level"] == "INFO"

    assert (
        data["request_id"]
        == "req-001"
    )


def test_console_formatter_is_human_readable() -> None:
    formatter = ConsoleFormatter(
        component="core"
    )

    record = make_record(
        event="core_started"
    )

    result = formatter.format(
        record
    )

    assert "INFO" in result
    assert "core" in result
    assert "core_started" in result
    assert "Agent Core started" in result


def test_console_formatter_redacts_secret() -> None:
    formatter = ConsoleFormatter(
        component="core"
    )

    record = make_record(
        message=(
            "token=my-secret-token"
        )
    )

    result = formatter.format(
        record
    )

    assert (
        "my-secret-token"
        not in result
    )

    assert "[REDACTED]" in result