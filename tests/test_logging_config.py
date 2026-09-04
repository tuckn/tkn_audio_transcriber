import io
import logging

import pytest

from audio_transcriber.logging_config import (
    RESET,
    SUCCESS,
    configure_logging,
    log_success,
)


class TerminalBuffer(io.StringIO):
    def __init__(self, is_tty: bool) -> None:
        super().__init__()
        self.is_tty = is_tty

    def isatty(self) -> bool:
        return self.is_tty


def test_success_level_order_and_prefix() -> None:
    assert logging.INFO < SUCCESS < logging.WARNING
    assert logging.getLevelName(SUCCESS) == "SUCCESS"
    stream = TerminalBuffer(False)
    logger = configure_logging(quiet=False, verbose=False, stream=stream)
    log_success(logger, "done")
    assert stream.getvalue() == "[SUCCESS] done\n"


@pytest.mark.parametrize(
    ("quiet", "verbose", "expected"),
    [
        (False, False, "[INFO] info\n[ERROR] error\n"),
        (True, False, "[ERROR] error\n"),
        (False, True, "[DEBUG] debug\n[INFO] info\n[ERROR] error\n"),
    ],
)
def test_level_filtering(quiet: bool, verbose: bool, expected: str) -> None:
    stream = TerminalBuffer(False)
    logger = configure_logging(quiet=quiet, verbose=verbose, stream=stream)
    logger.debug("debug")
    logger.info("info")
    logger.error("error")
    assert stream.getvalue() == expected


def test_color_and_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(
        "audio_transcriber.logging_config._enable_windows_virtual_terminal",
        lambda stream: True,
    )
    stream = TerminalBuffer(True)
    logger = configure_logging(quiet=False, verbose=False, stream=stream)
    log_success(logger, "ok")
    logger.error("bad")
    rendered = stream.getvalue()
    assert "\x1b[32m[SUCCESS] ok" in rendered
    assert "\x1b[31m[ERROR] bad" in rendered
    assert rendered.count(RESET) == 2

    monkeypatch.setenv("NO_COLOR", "1")
    no_color = TerminalBuffer(True)
    logger = configure_logging(quiet=False, verbose=False, stream=no_color)
    log_success(logger, "ok")
    assert "\x1b[" not in no_color.getvalue()
