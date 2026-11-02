from __future__ import annotations

import re

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_TOTAL_BYTES = 200 * 1024 * 1024


# 只管理 agent 自己生成的日志。
#
# 支持：
#   core-2026-09-13.jsonl
#   core-2026-09-13.001.jsonl
#   host-2026-09-13.jsonl
#   ipc-2026-09-13.002.jsonl
_LOG_FILE_PATTERN = re.compile(
    r"^(core|host|ipc)-"
    r"(\d{4}-\d{2}-\d{2})"
    r"(?:\.(\d{3}))?"
    r"\.jsonl$"
)


@dataclass(frozen=True)
class LogFileInfo:
    """一个 agent 日志文件的元信息。"""

    path: Path
    log_date: date
    index: int
    size: int


@dataclass
class RetentionResult:
    """一次日志清理的结果。"""

    deleted_files: list[Path]
    failed_files: list[Path]
    freed_bytes: int
    remaining_bytes: int


def _parse_log_file(
    path: Path,
) -> LogFileInfo | None:
    """
    识别 agent 日志文件。

    非 agent 文件直接返回 None，
    retention 不会碰它。
    """

    match = _LOG_FILE_PATTERN.match(
        path.name
    )

    if match is None:
        return None

    try:
        log_date = date.fromisoformat(
            match.group(2)
        )

        index_text = match.group(3)

        index = (
            int(index_text)
            if index_text is not None
            else 0
        )

        size = path.stat().st_size

    except (
        ValueError,
        OSError,
    ):
        return None

    return LogFileInfo(
        path=path,
        log_date=log_date,
        index=index,
        size=size,
    )


def _collect_log_files(
    log_dir: Path,
) -> list[LogFileInfo]:
    """
    收集日志目录中由 agent 管理的 JSONL 文件。
    """

    if not log_dir.exists():
        return []

    files: list[LogFileInfo] = []

    try:
        paths = list(
            log_dir.glob("*.jsonl")
        )
    except OSError:
        return []

    for path in paths:
        info = _parse_log_file(path)

        if info is not None:
            files.append(info)

    return files


def _delete_file(
    info: LogFileInfo,
    *,
    deleted_files: list[Path],
    failed_files: list[Path],
) -> int:
    """
    尝试删除一个日志文件。

    成功：
        返回释放的字节数。

    失败：
        返回 0，并记录失败文件。

    日志清理失败不能影响 Agent 主业务。
    """

    try:
        info.path.unlink()

    except OSError:
        failed_files.append(
            info.path
        )

        return 0

    deleted_files.append(
        info.path
    )

    return info.size


def cleanup_logs(
    log_dir: Path,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    today: date | None = None,
) -> RetentionResult:
    """
    执行 agent 日志清理策略。

    第一层保护：
        只保留最近 retention_days 个自然日。

    第二层保护：
        清理后如果日志总大小仍超过 max_total_bytes，
        从最旧日志开始继续删除。

    例如：

        today = 2026-09-13
        retention_days = 14

    则保留：

        2026-08-31 ~ 2026-09-13

    更早的日志会被删除。
    """

    if retention_days <= 0:
        raise ValueError(
            "retention_days must be greater than 0"
        )

    if max_total_bytes < 0:
        raise ValueError(
            "max_total_bytes cannot be negative"
        )

    log_dir = Path(log_dir)

    current_day = (
        today
        if today is not None
        else date.today()
    )

    # “最近 14 个自然日”包含今天。
    oldest_allowed_date = (
        current_day
        - timedelta(
            days=retention_days - 1
        )
    )

    deleted_files: list[Path] = []
    failed_files: list[Path] = []

    freed_bytes = 0

    files = _collect_log_files(
        log_dir
    )

    # =====================================================
    # 第一阶段：按日期删除过期日志
    # =====================================================

    for info in files:
        if (
            info.log_date
            < oldest_allowed_date
        ):
            freed_bytes += _delete_file(
                info,
                deleted_files=deleted_files,
                failed_files=failed_files,
            )

    # =====================================================
    # 第二阶段：限制日志目录总大小
    # =====================================================

    remaining_files = (
        _collect_log_files(
            log_dir
        )
    )

    total_size = sum(
        info.size
        for info in remaining_files
    )

    if total_size > max_total_bytes:
        # 最旧日期优先。
        #
        # 同一天：
        #   .000 / 主文件
        #   .001
        #   .002
        #
        # 按 index 从小到大删除。
        remaining_files.sort(
            key=lambda info: (
                info.log_date,
                info.index,
                info.path.name,
            )
        )

        for info in remaining_files:
            if (
                total_size
                <= max_total_bytes
            ):
                break

            released = _delete_file(
                info,
                deleted_files=deleted_files,
                failed_files=failed_files,
            )

            freed_bytes += released
            total_size -= released

    # 最后重新计算一次，
    # 避免删除失败导致统计值失真。
    final_files = _collect_log_files(
        log_dir
    )

    remaining_bytes = sum(
        info.size
        for info in final_files
    )

    return RetentionResult(
        deleted_files=deleted_files,
        failed_files=failed_files,
        freed_bytes=freed_bytes,
        remaining_bytes=remaining_bytes,
    )