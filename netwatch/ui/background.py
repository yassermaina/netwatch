"""Run blocking work off the GTK main thread and deliver results on it."""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Callable, Optional

from gi.repository import GLib

log = logging.getLogger(__name__)

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="nw-ui-worker")


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> concurrent.futures.Future:
    """Run ``fn`` on a bounded worker pool; call ``on_done(result)`` on the main loop."""

    def deliver(future: concurrent.futures.Future) -> None:
        exc = future.exception()

        def main_thread() -> bool:
            if exc is not None:
                log.error("background task failed", exc_info=exc)
                if on_error:
                    on_error(exc)
            else:
                on_done(future.result())
            return GLib.SOURCE_REMOVE

        GLib.idle_add(main_thread)

    future = _pool.submit(fn)
    future.add_done_callback(deliver)
    return future


def shutdown() -> None:
    _pool.shutdown(wait=False, cancel_futures=True)


class Generation:
    """Drop results of superseded requests (e.g. fast period switching)."""

    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value

    def current(self, token: int) -> bool:
        return token == self.value
