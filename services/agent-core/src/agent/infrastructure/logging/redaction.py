from __future__ import annotations

import re
from typing import Any


REDACTED = "[REDACTED]"

# 结构化日志中常见的敏感字段。
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "client_secret",
    "password",
    "authorization",
    "cookie",
    "set_cookie",
}


# 文本中常见的敏感信息模式。
_TEXT_PATTERNS: tuple[
    tuple[re.Pattern[str], str],
    ...
] = (
    # Authorization: Bearer xxxxx
    (
        re.compile(
            r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"
        ),
        f"Bearer {REDACTED}",
    ),

    # api_key=xxx / token: xxx / password=xxx
    (
        re.compile(
            r"(?i)\b("
            r"api[_-]?key|"
            r"token|"
            r"access[_-]?token|"
            r"refresh[_-]?token|"
            r"password|"
            r"secret|"
            r"client[_-]?secret"
            r")"
            r"\s*[:=]\s*"
            r"[^\s,;]+"
        ),
        rf"\1={REDACTED}",
    ),

    # 常见 sk-... 类型 API Key。
    (
        re.compile(
            r"\bsk-[A-Za-z0-9_-]{8,}\b"
        ),
        REDACTED,
    ),
)


def _normalize_key(key: str) -> str:
    """
    把字段名转换成统一形式，便于识别敏感字段。

    例如：
        API-Key      -> api_key
        access-token -> access_token
        Client Secret -> client_secret
    """

    return (
        key.strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _is_sensitive_key(key: str) -> bool:
    """
    判断结构化字段是否属于敏感字段。

    除了完全匹配，还允许：
        provider_api_key
        github_access_token
        smtp_password
    """

    normalized = _normalize_key(key)

    if normalized in _SENSITIVE_KEYS:
        return True

    return any(
        normalized.endswith(f"_{sensitive}")
        for sensitive in _SENSITIVE_KEYS
    )


def redact_text(text: str) -> str:
    """
    清除普通字符串中的常见秘密。

    注意：
    这是日志层的最后一道保护，不应该替代业务代码本身的
    Secret Store / Stronghold 等安全机制。
    """

    result = text

    for pattern, replacement in _TEXT_PATTERNS:
        result = pattern.sub(
            replacement,
            result,
        )

    return result


def redact_value(
    value: Any,
    *,
    key: str | None = None,
) -> Any:
    """
    递归清理准备进入日志的数据。

    支持：
    - dict
    - list
    - tuple
    - string
    - int / float / bool / None

    如果当前字段本身是敏感字段，
    无论里面是什么内容，都直接替换为 [REDACTED]。
    """

    if (
        key is not None
        and _is_sensitive_key(key)
    ):
        return REDACTED

    if isinstance(value, dict):
        return {
            item_key: redact_value(
                item_value,
                key=str(item_key),
            )
            for item_key, item_value
            in value.items()
        }

    if isinstance(value, list):
        return [
            redact_value(item)
            for item in value
        ]

    if isinstance(value, tuple):
        return tuple(
            redact_value(item)
            for item in value
        )

    if isinstance(value, str):
        return redact_text(value)

    if value is None or isinstance(
        value,
        (bool, int, float),
    ):
        return value

    # 未知对象不直接把原对象传进日志系统。
    # 转成字符串以后仍然经过文本脱敏。
    return redact_text(str(value))