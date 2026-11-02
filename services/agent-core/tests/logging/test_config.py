import json
import logging

from agent.infrastructure.logging import (
    events,
    log_event,
    setup_logging,
)


def test_setup_logging_writes_jsonl(
    tmp_path,
) -> None:
    runtime = setup_logging(
        log_dir=tmp_path,
        level="INFO",
    )

    log_event(
        runtime.logger,
        logging.INFO,
        events.CORE_STARTED,
        "Agent Core started",
        pid=12345,
    )

    runtime.shutdown()

    files = list(
        tmp_path.glob(
            "core-*.jsonl"
        )
    )

    assert len(files) == 1

    lines = (
        files[0]
        .read_text(
            encoding="utf-8"
        )
        .splitlines()
    )

    # 至少应该存在：
    #
    # logging_started
    # core_started
    assert len(lines) >= 2

    records = [
        json.loads(line)
        for line in lines
    ]

    core_started = next(
        record
        for record in records
        if (
            record["event"]
            == events.CORE_STARTED
        )
    )

    assert (
        core_started["pid"]
        == 12345
    )


def test_setup_logging_without_directory_is_console_only(
) -> None:
    runtime = setup_logging(
        log_dir=None,
        level="INFO",
    )

    try:
        assert (
            runtime.file_enabled
            is False
        )

        assert (
            runtime.log_dir
            is None
        )

    finally:
        runtime.shutdown()


def test_log_event_redacts_secrets(
    tmp_path,
) -> None:
    runtime = setup_logging(
        log_dir=tmp_path,
    )

    log_event(
        runtime.logger,
        logging.INFO,
        "provider_test",
        "Provider configured",
        api_key="sk-super-secret123456",
    )

    runtime.shutdown()

    text = next(
        tmp_path.glob(
            "core-*.jsonl"
        )
    ).read_text(
        encoding="utf-8"
    )

    assert (
        "sk-super-secret123456"
        not in text
    )

    assert (
        "[REDACTED]"
        in text
    )


def test_environment_variable_can_supply_log_directory(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AGENT_LOG_DIR",
        str(tmp_path),
    )

    runtime = setup_logging()

    try:
        assert (
            runtime.log_dir
            == tmp_path
        )

        assert (
            runtime.file_enabled
            is True
        )

    finally:
        runtime.shutdown()


def test_explicit_directory_overrides_environment(
    tmp_path,
    monkeypatch,
) -> None:
    environment_dir = (
        tmp_path
        / "environment"
    )

    explicit_dir = (
        tmp_path
        / "explicit"
    )

    monkeypatch.setenv(
        "AGENT_LOG_DIR",
        str(environment_dir),
    )

    runtime = setup_logging(
        log_dir=explicit_dir
    )

    try:
        assert (
            runtime.log_dir
            == explicit_dir
        )

    finally:
        runtime.shutdown()