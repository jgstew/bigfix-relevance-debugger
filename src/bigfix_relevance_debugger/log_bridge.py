"""Forward ``bigfix_remote_client_relevance`` log records to a UI callback.

The library logs and never prints; its INFO records are the progress of slow
first-time work (qna downloads, image pulls, image prep), which is exactly
what the activity log panel should show. No NiceGUI imports here: the
callback is whatever the UI passes (e.g. a ``ui.log``'s ``push``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

LIBRARY_LOGGER = "bigfix_remote_client_relevance"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class CallbackHandler(logging.Handler):
    """Hands each formatted record to ``callback``.

    With a ``loop``, a record emitted on any other thread is scheduled onto
    that loop rather than delivered in place: UI elements belong to the loop,
    and the library can log from worker threads (e.g. docker SDK calls run via
    ``asyncio.to_thread``).
    """

    def __init__(
        self,
        callback: Callable[[str], object],
        level: int = logging.NOTSET,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(level)
        self._callback = callback
        self._loop = loop
        self.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))

    def _on_loop_thread(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            if self._loop is None or self._on_loop_thread():
                self._callback(line)
            else:
                self._loop.call_soon_threadsafe(self._callback, line)
        except RuntimeError:
            # call_soon_threadsafe on a closed loop: the page is gone, and a
            # log line nobody can see is not worth a traceback.
            if self._loop is not None and self._loop.is_closed():
                return
            self.handleError(record)
        except Exception:  # noqa: BLE001 - logging must never break the caller
            self.handleError(record)


def attach(
    callback: Callable[[str], object],
    level: int = logging.INFO,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> CallbackHandler:
    """Attach a handler to the library logger and make sure ``level`` gets through.

    The library attaches only a ``NullHandler`` and leaves its logger level
    unset, which inherits root's WARNING -- INFO records would never be
    created. Lower the logger to ``level`` if needed, but never raise it: a
    second client asking for INFO must not silence the first one's DEBUG.
    """
    logger = logging.getLogger(LIBRARY_LOGGER)
    handler = CallbackHandler(callback, level, loop=loop)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    return handler


def detach(handler: logging.Handler) -> None:
    """Remove a handler added by :func:`attach`. Safe to call twice."""
    logging.getLogger(LIBRARY_LOGGER).removeHandler(handler)
    handler.close()
