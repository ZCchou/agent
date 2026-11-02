from __future__ import annotations

import logging
import os

from datetime import date
from logging.handlers import QueueHandler
from pathlib import Path
from typing import Callable


DEFAULT_MAX_BYTES = 20 * 1024 * 1024


class AgentQueueHandler(QueueHandler):
    """
    agent 使用的日志队列 Handler。

    Python 标准 QueueHandler 默认会提前格式化 LogRecord，
    并可能清除异常信息。

    agent 希望真正的 Formatter 在后台写线程中执行，
    因此这里保留原始 LogRecord。
    """

    def prepare(
        self,
        record: logging.LogRecord,
    ) -> logging.LogRecord:
        return record


class DailySizeJsonlHandler(
    logging.FileHandler
):
    """
    agent JSONL 文件 Handler。

    同时支持：

    1. 按日期自动切换日志文件；
    2. 单个日志文件达到大小上限后自动轮转；
    3. 应用重启后继续使用当天尚未满的日志文件。

    文件格式：

        core-2026-09-13.jsonl

    当文件超过大小限制：

        core-2026-09-13.001.jsonl
        core-2026-09-13.002.jsonl
        ...
    """

    def __init__(
        self,
        log_dir: Path,
        component: str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.component = component
        self.max_bytes = max_bytes

        # 通过 date_provider 注入日期来源，
        # 主要是为了让日期轮转能够独立测试。
        self._date_provider = (
            date_provider
            or date.today
        )

        self.log_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.current_date = (
            self._today_string()
        )

        self.file_index = (
            self._find_available_index(
                self.current_date
            )
        )

        initial_path = self._build_path(
            self.current_date,
            self.file_index,
        )

        super().__init__(
            filename=initial_path,
            mode="a",
            encoding="utf-8",
            delay=True,
        )

    def _today_string(self) -> str:
        """
        返回当前本地日期：

            YYYY-MM-DD

        文件名使用本地日期，
        JSONL 内部 timestamp 仍由 Formatter 使用 UTC。
        """

        return (
            self._date_provider()
            .isoformat()
        )

    def _build_path(
        self,
        date_string: str,
        index: int,
    ) -> Path:
        """
        根据日期和轮转序号生成日志路径。
        """

        if index == 0:
            filename = (
                f"{self.component}-"
                f"{date_string}.jsonl"
            )
        else:
            filename = (
                f"{self.component}-"
                f"{date_string}."
                f"{index:03d}.jsonl"
            )

        return (
            self.log_dir
            / filename
        )

    def _find_available_index(
        self,
        date_string: str,
    ) -> int:
        """
        找到当天第一个尚未达到大小上限的日志文件。

        应用重启时，如果：

            core-2026-09-13.jsonl

        还没达到 20 MB，就继续追加。

        如果已经满了，则寻找：

            .001
            .002
            ...
        """

        index = 0

        while True:
            path = self._build_path(
                date_string,
                index,
            )

            if not path.exists():
                return index

            try:
                size = path.stat().st_size
            except OSError:
                # 无法读取文件大小时，
                # 不继续使用这个文件。
                index += 1
                continue

            if size < self.max_bytes:
                return index

            index += 1

    def _current_file_size(self) -> int:
        """
        返回当前日志文件大小。

        文件尚未创建时返回 0。
        """

        path = Path(
            self.baseFilename
        )

        if not path.exists():
            return 0

        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _switch_file(
        self,
        date_string: str,
        index: int,
    ) -> None:
        """
        切换到新的日志文件。

        切换前必须 flush 并关闭旧文件，
        防止文件句柄泄漏。
        """

        if self.stream is not None:
            self.flush()
            self.stream.close()
            self.stream = None

        self.current_date = date_string
        self.file_index = index

        new_path = self._build_path(
            date_string,
            index,
        )

        self.baseFilename = os.path.abspath(
            new_path
        )

    def _rotate_for_new_day(
        self,
        today: str,
    ) -> None:
        """
        日期发生变化时切换到新一天的日志。
        """

        index = (
            self._find_available_index(
                today
            )
        )

        self._switch_file(
            today,
            index,
        )

    def _rotate_for_size(self) -> None:
        """
        当前文件超过大小限制时，
        切换到下一个序号文件。
        """

        next_index = (
            self.file_index + 1
        )

        self._switch_file(
            self.current_date,
            next_index,
        )

    def emit(
        self,
        record: logging.LogRecord,
    ) -> None:
        """
        写入日志前检查：

        1. 是否跨天；
        2. 当前文件写入后是否会超过大小限制。

        所有异常由 logging Handler 自身处理，
        日志失败不能影响 Agent 主业务。
        """

        try:
            today = self._today_string()

            if today != self.current_date:
                self._rotate_for_new_day(
                    today
                )

            # 必须先格式化，才能知道这条日志写入磁盘后
            # 实际会占多少 UTF-8 字节。
            formatted = self.format(
                record
            )

            encoded = (
                formatted
                + self.terminator
            ).encode(
                "utf-8"
            )

            current_size = (
                self._current_file_size()
            )

            # 当前文件已经有内容，并且加入新日志后
            # 将超过限制，则先切文件再写。
            #
            # 如果单条日志自己就超过 max_bytes，
            # 仍允许写入一个新文件，避免无限轮转。
            if (
                current_size > 0
                and current_size
                + len(encoded)
                > self.max_bytes
            ):
                self._rotate_for_size()

            super().emit(record)

        except Exception:
            # Python logging 的标准错误处理路径。
            #
            # 即使磁盘满、目录被删除或权限异常，
            # 也不能让 agent 主业务崩溃。
            self.handleError(record)