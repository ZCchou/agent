import json
import logging

from datetime import date

from agent.infrastructure.logging.formatter import (
    JsonLineFormatter,
)
from agent.infrastructure.logging.handlers import (
    DailySizeJsonlHandler,
)


def make_logger(
    handler: logging.Handler,
) -> logging.Logger:
    """
    创建一个完全独立的测试 Logger。
    """

    logger = logging.getLogger(
        f"agent_test_{id(handler)}"
    )

    logger.setLevel(
        logging.DEBUG
    )

    logger.propagate = False
    logger.handlers.clear()

    logger.addHandler(
        handler
    )

    return logger


def test_handler_writes_jsonl_file(
    tmp_path,
) -> None:
    handler = DailySizeJsonlHandler(
        log_dir=tmp_path,
        component="core",
    )

    handler.setFormatter(
        JsonLineFormatter(
            component="core"
        )
    )

    logger = make_logger(
        handler
    )

    logger.info(
        "Agent Core started",
        extra={
            "event": "core_started",
            "fields": {
                "pid": 12345,
            },
        },
    )

    handler.flush()
    handler.close()

    files = list(
        tmp_path.glob(
            "core-*.jsonl"
        )
    )

    assert len(files) == 1

    content = (
        files[0]
        .read_text(
            encoding="utf-8"
        )
        .strip()
    )

    data = json.loads(
        content
    )

    assert (
        data["event"]
        == "core_started"
    )

    assert data["pid"] == 12345


def test_handler_writes_one_json_object_per_line(
    tmp_path,
) -> None:
    handler = DailySizeJsonlHandler(
        log_dir=tmp_path,
        component="core",
    )

    handler.setFormatter(
        JsonLineFormatter(
            component="core"
        )
    )

    logger = make_logger(
        handler
    )

    logger.info(
        "first",
        extra={
            "event": "first_event",
        },
    )

    logger.info(
        "second",
        extra={
            "event": "second_event",
        },
    )

    handler.flush()
    handler.close()

    file = next(
        tmp_path.glob(
            "core-*.jsonl"
        )
    )

    lines = (
        file.read_text(
            encoding="utf-8"
        )
        .splitlines()
    )

    assert len(lines) == 2

    first = json.loads(
        lines[0]
    )

    second = json.loads(
        lines[1]
    )

    assert (
        first["event"]
        == "first_event"
    )

    assert (
        second["event"]
        == "second_event"
    )


def test_handler_rotates_when_size_limit_reached(
    tmp_path,
) -> None:
    handler = DailySizeJsonlHandler(
        log_dir=tmp_path,
        component="core",

        # 测试使用很小的大小限制，
        # 不需要真的写 20 MB。
        max_bytes=350,
    )

    handler.setFormatter(
        JsonLineFormatter(
            component="core"
        )
    )

    logger = make_logger(
        handler
    )

    for index in range(6):
        logger.info(
            "x" * 200,
            extra={
                "event":
                    "rotation_test",

                "fields": {
                    "index": index,
                },
            },
        )

    handler.flush()
    handler.close()

    files = sorted(
        tmp_path.glob(
            "core-*.jsonl"
        )
    )

    assert len(files) >= 2

    names = [
        file.name
        for file in files
    ]

    assert any(
        ".001.jsonl" in name
        for name in names
    )


def test_handler_rotates_when_date_changes(
    tmp_path,
) -> None:
    current_date = [
        date(
            2026,
            9,
            13,
        )
    ]

    def date_provider() -> date:
        return current_date[0]

    handler = DailySizeJsonlHandler(
        log_dir=tmp_path,
        component="core",
        date_provider=date_provider,
    )

    handler.setFormatter(
        JsonLineFormatter(
            component="core"
        )
    )

    logger = make_logger(
        handler
    )

    logger.info(
        "day one",
        extra={
            "event": "day_one",
        },
    )

    handler.flush()

    # 模拟第二天到来。
    current_date[0] = date(
        2026,
        9,
        14,
    )

    logger.info(
        "day two",
        extra={
            "event": "day_two",
        },
    )

    handler.flush()
    handler.close()

    first_file = (
        tmp_path
        / "core-2026-09-13.jsonl"
    )

    second_file = (
        tmp_path
        / "core-2026-09-14.jsonl"
    )

    assert first_file.exists()
    assert second_file.exists()

    first_data = json.loads(
        first_file
        .read_text(
            encoding="utf-8"
        )
        .strip()
    )

    second_data = json.loads(
        second_file
        .read_text(
            encoding="utf-8"
        )
        .strip()
    )

    assert (
        first_data["event"]
        == "day_one"
    )

    assert (
        second_data["event"]
        == "day_two"
    )


def test_handler_continues_existing_file_if_not_full(
    tmp_path,
) -> None:
    existing = (
        tmp_path
        / "core-2026-09-13.jsonl"
    )

    existing.write_text(
        '{"existing":true}\n',
        encoding="utf-8",
    )

    handler = DailySizeJsonlHandler(
        log_dir=tmp_path,
        component="core",
        max_bytes=10_000,
        date_provider=lambda: date(
            2026,
            9,
            13,
        ),
    )

    handler.setFormatter(
        JsonLineFormatter(
            component="core"
        )
    )

    logger = make_logger(
        handler
    )

    logger.info(
        "new entry",
        extra={
            "event": "new_event",
        },
    )

    handler.flush()
    handler.close()

    files = list(
        tmp_path.glob(
            "core-*.jsonl"
        )
    )

    # 不应该创建 .001 文件。
    assert len(files) == 1

    lines = (
        existing
        .read_text(
            encoding="utf-8"
        )
        .splitlines()
    )

    assert len(lines) == 2