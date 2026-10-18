"""Forward the evaluation libraries' log records to a UI callback.

The libraries log and never print. bigfix-remote-client-relevance's INFO
records are the progress of slow first-time work (qna downloads, image pulls,
image prep); bigfix-remote-session-relevance's are certificate pins (a
WARNING with the fingerprint), failover and recorded deployment ids. That's
exactly what the activity log panel should show. No NiceGUI imports here: the
callback is whatever the UI passes (e.g. a ``ui.log``'s ``push``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

LIBRARY_LOGGERS = ("bigfix_remote_client_relevance", "bigfix_remote_session_relevance")

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
    """Attach one handler to each library logger and make sure ``level`` gets through.

    The libraries attach at most a ``NullHandler`` and leave their logger
    level unset, which inherits root's WARNING -- INFO records would never be
    created. Lower each logger to ``level`` if needed, but never raise it: a
    second client asking for INFO must not silence the first one's DEBUG.
    """
    handler = CallbackHandler(callback, level, loop=loop)
    for name in LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > level:
            logger.setLevel(level)
    return handler


def detach(handler: logging.Handler) -> None:
    """Remove a handler added by :func:`attach`. Safe to call twice."""
    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).removeHandler(handler)
    handler.close()
