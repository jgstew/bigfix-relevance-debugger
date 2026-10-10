"""Tests for log_bridge.py: library log records -> a UI callback."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from bigfix_relevance_debugger.log_bridge import (
    LIBRARY_LOGGER,
    CallbackHandler,
    attach,
    detach,
)


@pytest.fixture
def library_logger():
    logger = logging.getLogger(LIBRARY_LOGGER)
    saved = logger.level, list(logger.handlers)
    yield logger
    logger.setLevel(saved[0])
    logger.handlers[:] = saved[1]


def test_library_logger_name():
    assert LIBRARY_LOGGER == "bigfix_remote_client_relevance"


def test_handler_forwards_formatted_records():
    lines: list[str] = []
    handler = CallbackHandler(lines.append)
    logger = logging.getLogger("test.log_bridge.forward")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("pulling %s", "debian:11")
    finally:
        logger.removeHandler(handler)

    [line] = lines
    assert "pulling debian:11" in line
    assert "INFO" in line


def test_attach_receives_child_logger_info(library_logger):
    lines: list[str] = []
    handler = attach(lines.append, logging.INFO)
    try:
        logging.getLogger(f"{LIBRARY_LOGGER}.bootstrap.cache").info("downloading qna")
        logging.getLogger(f"{LIBRARY_LOGGER}.orchestrate").debug("noisy detail")
    finally:
        detach(handler)

    assert len(lines) == 1
    assert "downloading qna" in lines[0]


def test_attach_debug_level_includes_debug(library_logger):
    lines: list[str] = []
    handler = attach(lines.append, logging.DEBUG)
    try:
        logging.getLogger(LIBRARY_LOGGER).debug("noisy detail")
    finally:
        detach(handler)

    assert any("noisy detail" in line for line in lines)


def test_attach_lowers_library_logger_level_so_records_are_created(library_logger):
    library_logger.setLevel(logging.NOTSET)  # effective level: root's WARNING

    handler = attach(lambda _: None, logging.INFO)
    try:
        assert library_logger.isEnabledFor(logging.INFO)
    finally:
        detach(handler)


def test_attach_never_raises_logger_level(library_logger):
    library_logger.setLevel(logging.DEBUG)

    handler = attach(lambda _: None, logging.INFO)
    try:
        assert library_logger.level == logging.DEBUG
    finally:
        detach(handler)


def test_detach_stops_forwarding(library_logger):
    lines: list[str] = []
    handler = attach(lines.append, logging.INFO)
    detach(handler)

    logging.getLogger(LIBRARY_LOGGER).info("after detach")

    assert lines == []


def test_detach_is_idempotent(library_logger):
    handler = attach(lambda _: None, logging.INFO)
    detach(handler)
    detach(handler)


async def test_records_from_other_threads_are_handed_to_the_loop(library_logger):
    loop = asyncio.get_running_loop()
    seen: list[tuple[str, int]] = []
    done = asyncio.Event()

    def callback(line: str) -> None:
        seen.append((line, threading.get_ident()))
        done.set()

    handler = attach(callback, logging.INFO, loop=loop)
    try:
        thread = threading.Thread(
            target=lambda: logging.getLogger(LIBRARY_LOGGER).info("from a worker thread")
        )
        thread.start()
        thread.join()
        await asyncio.wait_for(done.wait(), 2)
    finally:
        detach(handler)

    [(line, thread_id)] = seen
    assert "from a worker thread" in line
    assert thread_id == threading.get_ident()


async def test_records_on_the_loop_thread_are_delivered_immediately(library_logger):
    seen: list[str] = []
    handler = attach(seen.append, logging.INFO, loop=asyncio.get_running_loop())
    try:
        logging.getLogger(LIBRARY_LOGGER).info("same thread")
    finally:
        detach(handler)

    assert len(seen) == 1


def test_callback_failure_does_not_propagate(library_logger):
    def broken(_: str) -> None:
        raise RuntimeError("ui gone")

    handler = attach(broken, logging.INFO)
    try:
        logging.raiseExceptions = False
        logging.getLogger(LIBRARY_LOGGER).info("still fine")
    finally:
        logging.raiseExceptions = True
        detach(handler)


def test_closed_loop_drops_records_quietly(library_logger):
    loop = asyncio.new_event_loop()
    loop.close()
    seen: list[str] = []
    handler = attach(seen.append, logging.INFO, loop=loop)
    try:
        thread = threading.Thread(target=lambda: logging.getLogger(LIBRARY_LOGGER).info("too late"))
        thread.start()
        thread.join()
    finally:
        detach(handler)

    assert seen == []
