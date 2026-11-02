from agent.infrastructure.logging.redaction import (
    REDACTED,
    redact_text,
    redact_value,
)


def test_redacts_sensitive_dict_fields() -> None:
    data = {
        "api_key": "sk-secret-12345678",
        "password": "123456",
        "user": "alice",
    }

    result = redact_value(data)

    assert result["api_key"] == REDACTED
    assert result["password"] == REDACTED
    assert result["user"] == "alice"


def test_redacts_nested_sensitive_fields() -> None:
    data = {
        "provider": {
            "access_token": "abc123",
            "config": {
                "client_secret": "very-secret",
            },
        }
    }

    result = redact_value(data)

    assert (
        result["provider"]["access_token"]
        == REDACTED
    )

    assert (
        result["provider"]["config"]["client_secret"]
        == REDACTED
    )


def test_redacts_sensitive_fields_with_prefix() -> None:
    data = {
        "openai_api_key": "sk-abcdefgh123456",
        "github_access_token": "token-value",
    }

    result = redact_value(data)

    assert result["openai_api_key"] == REDACTED
    assert (
        result["github_access_token"]
        == REDACTED
    )


def test_redacts_bearer_token_inside_text() -> None:
    text = (
        "Authorization: "
        "Bearer abc.def.ghi"
    )

    result = redact_text(text)

    assert "abc.def.ghi" not in result
    assert REDACTED in result


def test_redacts_api_key_inside_text() -> None:
    text = (
        "provider api_key="
        "sk-test-secret123456"
    )

    result = redact_text(text)

    assert (
        "sk-test-secret123456"
        not in result
    )

    assert REDACTED in result


def test_keeps_normal_values_unchanged() -> None:
    data = {
        "provider": "openai",
        "model": "gpt-example",
        "request_id": "req-001",
        "duration_ms": 125,
        "success": True,
    }

    result = redact_value(data)

    assert result == data


def test_redacts_values_inside_lists() -> None:
    data = [
        {
            "token": "secret-token",
        },
        {
            "name": "normal",
        },
    ]

    result = redact_value(data)

    assert result[0]["token"] == REDACTED
    assert result[1]["name"] == "normal"