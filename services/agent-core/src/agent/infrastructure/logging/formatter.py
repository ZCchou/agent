from __future__ import annotations

import json
import logging

from datetime import datetime, timezone
from typing import Any

from agent.infrastructure.logging.redaction import (
    redact_value,
)


SCHEMA_VERSION = 1

DEFAULT_EVENT = "log_message"


def utc_timestamp() -> str:
    """
    生成统一 UTC ISO 8601 时间。

    示例：
        2026-09-13T07:42:31.127Z

    日志文件统一保存 UTC，
    避免用户跨时区后日志时间产生歧义。
    """

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class JsonLineFormatter(logging.Formatter):
    """
    agent JSONL 日志格式化器。

    每一个 LogRecord 最终都会变成一行独立 JSON。

    基础字段固定为：

        schema_version
        timestamp
        level
        component
        event
        message

    业务模块额外提供的结构化字段放在：

        record.fields

    中，并展开到 JSON 顶层。
    """

    RESERVED_FIELDS = {
        "schema_version",
        "timestamp",
        "level",
        "component",
        "event",
        "message",
        "exception",
    }

    def __init__(
        self,
        component: str,
    ) -> None:
        super().__init__()

        self.component = component

    def format(
        self,
        record: logging.LogRecord,
    ) -> str:
        """
        把 Python LogRecord 转成单行 JSON 字符串。
        """

        component = getattr(
            record,
            "component",
            self.component,
        )

        event = getattr(
            record,
            "event",
            DEFAULT_EVENT,
        )

        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_timestamp(),
            "level": record.levelname,
            "component": component,
            "event": event,

            # 日志正文同样必须经过脱敏。
            "message": redact_value(
                record.getMessage()
            ),
        }

        # 业务层可以通过：
        #
        # extra={
        #     "fields": {
        #         "request_id": "...",
        #         "duration_ms": 12,
        #     }
        # }
        #
        # 添加结构化字段。
        fields = getattr(
            record,
            "fields",
            None,
        )

        if isinstance(fields, dict):
            sanitized_fields = redact_value(
                fields
            )

            for key, value in sanitized_fields.items():
                # 防止业务字段覆盖日志系统自己的核心字段。
                if key in self.RESERVED_FIELDS:
                    continue

                data[key] = value

        # 如果 logger.exception(...) 携带异常，
        # traceback 也会写进 JSONL。
        #
        # traceback 仍然必须经过脱敏。
        if record.exc_info:
            exception_text = self.formatException(
                record.exc_info
            )

            data["exception"] = redact_value(
                exception_text
            )

        return json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        )


class ConsoleFormatter(logging.Formatter):
    """
    开发终端使用的人类可读格式。

    文件日志使用 JSONL，
    控制台则保持简单易读。

    注意：
    Python Core 的控制台日志以后必须写 stderr，
    不能污染 stdout IPC 通道。
    """

    def __init__(
        self,
        component: str,
    ) -> None:
        super().__init__()

        self.component = component

    def format(
        self,
        record: logging.LogRecord,
    ) -> str:
        timestamp = (
            datetime.now()
            .astimezone()
            .strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )

        component = getattr(
            record,
            "component",
            self.component,
        )

        event = getattr(
            record,
            "event",
            DEFAULT_EVENT,
        )

        message = redact_value(
            record.getMessage()
        )

        return (
            f"{timestamp} | "
            f"{record.levelname:<5} | "
            f"{component} | "
            f"{event} | "
            f"{message}"
        )