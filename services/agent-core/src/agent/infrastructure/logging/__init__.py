"""
agent Python 日志基础设施。

业务代码原则上只从这个包导入公开 API，
而不是直接依赖内部 Formatter / Handler。
"""

from agent.infrastructure.logging.config import (
    LoggingRuntime,
    log_event,
    setup_logging,
)

from agent.infrastructure.logging import events


__all__ = [
    "LoggingRuntime",
    "events",
    "log_event",
    "setup_logging",
]