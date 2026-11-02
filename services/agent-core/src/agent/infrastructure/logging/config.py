from __future__ import annotations

import logging
import os
import queue
import sys

from dataclasses import dataclass
from logging.handlers import QueueListener
from pathlib import Path
from typing import Any

from agent.infrastructure.logging.events import (
    LOGGING_DEGRADED,
    LOGGING_STARTED,
)
from agent.infrastructure.logging.formatter import (
    ConsoleFormatter,
    JsonLineFormatter,
)
from agent.infrastructure.logging.handlers import (
    DEFAULT_MAX_BYTES,
    DailySizeJsonlHandler,
    AgentQueueHandler,
)
from agent.infrastructure.logging.retention import (
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_RETENTION_DAYS,
    RetentionResult,
    cleanup_logs,
)


DEFAULT_LEVEL = "INFO"
LOGGER_NAME = "agent"


@dataclass
class LoggingRuntime:
    """
    agent Python 日志运行时。

    保存 QueueListener 和实际 Handler，
    以便 Agent Core 退出时正确 flush 和关闭日志。
    """

    logger: logging.Logger
    listener: QueueListener
    handlers: list[logging.Handler]

    file_enabled: bool
    log_dir: Path | None
    retention_result: RetentionResult | None

    def shutdown(self) -> None:
        """
        停止后台日志线程并关闭所有 Handler。

        QueueListener.stop() 会让已经进入队列的日志
        在程序退出前尽量完成处理。
        """

        self.listener.stop()

        for handler in self.handlers:
            try:
                handler.flush()
                handler.close()
            except Exception:
                # 日志系统关闭失败不能影响主程序退出。
                pass


def _resolve_level(
    level: str | None,
) -> int:
    """
    解析日志级别。

    优先顺序：

        显式参数
        ↓
        AGENT_LOG_LEVEL
        ↓
        INFO
    """

    level_name = (
        level
        or os.getenv(
            "AGENT_LOG_LEVEL",
            DEFAULT_LEVEL,
        )
    ).upper()

    resolved = getattr(
        logging,
        level_name,
        logging.INFO,
    )

    if not isinstance(
        resolved,
        int,
    ):
        return logging.INFO

    return resolved


def _resolve_log_dir(
    log_dir: Path | None,
) -> Path | None:
    """
    获取文件日志目录。

    Python Core 不自行推断 Windows/macOS/Linux 路径。

    正式运行时由 Rust Host 通过：

        AGENT_LOG_DIR

    注入系统日志目录。
    """

    if log_dir is not None:
        return Path(log_dir)

    value = os.getenv(
        "AGENT_LOG_DIR"
    )

    if not value:
        return None

    return Path(value)


def setup_logging(
    *,
    component: str = "core",
    log_dir: Path | None = None,
    level: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> LoggingRuntime:
    """
    初始化 agent Python Core 日志系统。

    默认行为：

    - stdout：
        完全保留给 NDJSON IPC。

    - stderr：
        人类可读控制台日志。

    - 文件：
        JSONL 结构化日志。

    - 文件初始化失败：
        自动降级为 stderr-only，
        不阻止 Agent Core 启动。
    """

    level_number = _resolve_level(
        level
    )

    resolved_log_dir = (
        _resolve_log_dir(
            log_dir
        )
    )

    # =====================================================
    # Logger
    # =====================================================

    logger = logging.getLogger(
        f"{LOGGER_NAME}.{component}"
    )

    # Logger 自身允许所有调试信息进入 Queue。
    # 最终是否输出由下游 Handler 的 level 决定。
    logger.setLevel(
        logging.DEBUG
    )

    logger.propagate = False

    # 防止重复 setup_logging() 时重复添加 Handler。
    logger.handlers.clear()

    # =====================================================
    # 异步日志队列
    # =====================================================

    log_queue: queue.Queue[
        logging.LogRecord
    ] = queue.Queue()

    queue_handler = (
        AgentQueueHandler(
            log_queue
        )
    )

    queue_handler.setLevel(
        logging.DEBUG
    )

    logger.addHandler(
        queue_handler
    )

    output_handlers: list[
        logging.Handler
    ] = []

    # =====================================================
    # stderr Console
    # =====================================================

    # 非常重要：
    #
    # Python stdout 已经被 agent NDJSON IPC 协议占用。
    # 普通日志绝对不能写 stdout。
    console_handler = (
        logging.StreamHandler(
            sys.stderr
        )
    )

    console_handler.setLevel(
        level_number
    )

    console_handler.setFormatter(
        ConsoleFormatter(
            component=component
        )
    )

    output_handlers.append(
        console_handler
    )

    # =====================================================
    # JSONL File
    # =====================================================

    file_enabled = False
    retention_result: (
        RetentionResult | None
    ) = None

    if resolved_log_dir is not None:
        try:
            resolved_log_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            # 每次初始化日志系统时先执行清理。
            #
            # 清理失败不会影响日志系统继续启动。
            retention_result = cleanup_logs(
                resolved_log_dir,
                retention_days=retention_days,
                max_total_bytes=(
                    max_total_bytes
                ),
            )

            file_handler = (
                DailySizeJsonlHandler(
                    log_dir=resolved_log_dir,
                    component=component,
                    max_bytes=max_bytes,
                )
            )

            file_handler.setLevel(
                level_number
            )

            file_handler.setFormatter(
                JsonLineFormatter(
                    component=component
                )
            )

            output_handlers.append(
                file_handler
            )

            file_enabled = True

        except Exception as error:
            # 此处不能使用 logger，
            # 因为 QueueListener 还没有启动。
            print(
                "[agent] "
                "File logging unavailable; "
                "falling back to stderr only: "
                f"{error}",
                file=sys.stderr,
            )

    # =====================================================
    # Queue Listener
    # =====================================================

    listener = QueueListener(
        log_queue,
        *output_handlers,

        # 下游 Handler 的 level 生效。
        respect_handler_level=True,
    )

    listener.start()

    runtime = LoggingRuntime(
        logger=logger,
        listener=listener,
        handlers=output_handlers,
        file_enabled=file_enabled,
        log_dir=resolved_log_dir,
        retention_result=retention_result,
    )

    # 日志系统自己启动之后，
    # 再通过正式日志链路记录初始化结果。
    log_event(
        logger,
        logging.INFO,
        LOGGING_STARTED,
        "Logging system started",
        file_enabled=file_enabled,
        component_name=component,
    )

    if (
        resolved_log_dir is not None
        and not file_enabled
    ):
        log_event(
            logger,
            logging.WARNING,
            LOGGING_DEGRADED,
            "File logging unavailable; using stderr only",
        )

    return runtime


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    """
    agent 业务代码统一使用的结构化日志入口。

    业务代码只需要说明：

        发生了什么
        event 是什么
        有哪些结构化字段

    不需要知道：

        文件在哪里
        JSON 怎么构造
        如何轮转
        如何脱敏
        如何异步写入
    """

    logger.log(
        level,
        message,
        extra={
            "event": event,
            "fields": fields,
        },
    )