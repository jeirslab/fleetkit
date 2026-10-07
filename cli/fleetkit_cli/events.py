"""The event stream every stage writes to: the CLI prints it, the API stores and
serves it. An event is a flat JSON-able dict with at least `stage` and `kind`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

Sink = Callable[[dict[str, Any]], None]


class Cancelled(Exception):
    pass


class Emitter:
    """Stamps events with time and stage, and carries the cancel flag."""

    def __init__(self, sink: Sink):
        self._sink = sink
        self._cancel = threading.Event()
        self._on_cancel: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def emit(self, stage: str, kind: str, **data: Any) -> None:
        self._sink({"ts": time.time(), "stage": stage, "kind": kind, **data})

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            hooks = list(self._on_cancel)
        for h in hooks:
            try:
                h()
            except Exception:  # noqa: BLE001 - a cancel hook must not stop the others
                pass

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def on_cancel(self, hook: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._on_cancel.append(hook)

        def remove() -> None:
            with self._lock:
                if hook in self._on_cancel:
                    self._on_cancel.remove(hook)
        return remove
