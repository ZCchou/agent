from datetime import date

import pytest

from agent.infrastructure.logging.retention import (
    cleanup_logs,
)


def create_log(
    tmp_path,
    name: str,
    size: int = 100,
):
    path = tmp_path / name

    path.write_bytes(
        b"x" * size
    )

    return path


def test_removes_logs_older_than_retention_period(
    tmp_path,
) -> None:
    old_log = create_log(
        tmp_path,
        "core-2026-08-30.jsonl",
    )

    boundary_log = create_log(
        tmp_path,
        "core-2026-08-31.jsonl",
    )

    recent_log = create_log(
        tmp_path,
        "core-2026-09-13.jsonl",
    )

    result = cleanup_logs(
        tmp_path,
        retention_days=14,
        max_total_bytes=10_000,
        today=date(
            2026,
            9,
            13,
        ),
    )

    assert not old_log.exists()

    assert boundary_log.exists()
    assert recent_log.exists()

    assert old_log in result.deleted_files


def test_total_size_limit_deletes_oldest_first(
    tmp_path,
) -> None:
    oldest = create_log(
        tmp_path,
        "core-2026-09-10.jsonl",
        size=100,
    )

    middle = create_log(
        tmp_path,
        "core-2026-09-11.jsonl",
        size=100,
    )

    newest = create_log(
        tmp_path,
        "core-2026-09-12.jsonl",
        size=100,
    )

    result = cleanup_logs(
        tmp_path,
        retention_days=14,

        # 三个文件总共 300，
        # 限制到 200。
        max_total_bytes=200,

        today=date(
            2026,
            9,
            13,
        ),
    )

    assert not oldest.exists()

    assert middle.exists()
    assert newest.exists()

    assert (
        result.remaining_bytes
        <= 200
    )


def test_rotation_files_are_sorted_by_index(
    tmp_path,
) -> None:
    base = create_log(
        tmp_path,
        "core-2026-09-12.jsonl",
        size=100,
    )

    first_rotation = create_log(
        tmp_path,
        "core-2026-09-12.001.jsonl",
        size=100,
    )

    second_rotation = create_log(
        tmp_path,
        "core-2026-09-12.002.jsonl",
        size=100,
    )

    cleanup_logs(
        tmp_path,
        retention_days=14,
        max_total_bytes=200,
        today=date(
            2026,
            9,
            13,
        ),
    )

    # 同一天达到总容量上限时，
    # 最早的主文件应该先删除。
    assert not base.exists()

    assert first_rotation.exists()
    assert second_rotation.exists()


def test_does_not_delete_unrelated_files(
    tmp_path,
) -> None:
    unrelated = (
        tmp_path
        / "user-data.jsonl"
    )

    unrelated.write_text(
        "important",
        encoding="utf-8",
    )

    create_log(
        tmp_path,
        "core-2026-01-01.jsonl",
    )

    cleanup_logs(
        tmp_path,
        today=date(
            2026,
            9,
            13,
        ),
    )

    # Retention 只能管理 agent 自己的日志。
    assert unrelated.exists()

    assert (
        unrelated.read_text(
            encoding="utf-8"
        )
        == "important"
    )


def test_missing_directory_is_safe(
    tmp_path,
) -> None:
    missing = (
        tmp_path
        / "does-not-exist"
    )

    result = cleanup_logs(
        missing
    )

    assert result.deleted_files == []
    assert result.failed_files == []
    assert result.freed_bytes == 0
    assert result.remaining_bytes == 0


def test_invalid_retention_days_are_rejected(
    tmp_path,
) -> None:
    with pytest.raises(
        ValueError
    ):
        cleanup_logs(
            tmp_path,
            retention_days=0,
        )


def test_negative_size_limit_is_rejected(
    tmp_path,
) -> None:
    with pytest.raises(
        ValueError
    ):
        cleanup_logs(
            tmp_path,
            max_total_bytes=-1,
        )