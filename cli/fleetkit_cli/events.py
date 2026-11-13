"""The event stream every stage writes to: the CLI prints it, the API stores and
serves it. An event is a flat JSON-able dict with at least `stage` and `kind`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

Sink = Callable[[dict[str, Any]], None]


class Cancelled(Exception):
    """The run was asked to stop. `result` is what is known of it by then (the
    plan, and `stopped`: which steps finished and which were in flight)."""

    def __init__(self, message: str = "cancelled", result: dict[str, Any] | None = None):
        super().__init__(message)
        self.result = result


def _redact(v: Any, secrets: list[str]) -> Any:
    if isinstance(v, str):
        for x in secrets:
            v = v.replace(x, "[secret]")
        return v
    if isinstance(v, dict):
        return {k: _redact(x, secrets) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact(x, secrets) for x in v]
    return v


class Emitter:
    """Stamps events with time and stage, and carries the cancel flag."""

    def __init__(self, sink: Sink):
        self._sink = sink
        self._cancel = threading.Event()
        self._on_cancel: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._secrets: set[str] = set()

    def emit(self, stage: str, kind: str, **data: Any) -> None:
        e = {"ts": time.time(), "stage": stage, "kind": kind, **data}
        with self._lock:
            secrets = list(self._secrets)
        if secrets:
            e = _redact(e, secrets)
        self._sink(e)

    def redact(self, text: Any) -> Any:
        """`text` (a string, or any JSON-able value) without the registered secrets."""
        with self._lock:
            secrets = list(self._secrets)
        return _redact(text, secrets)

    def secret(self, value: str) -> str:
        """Never let `value` reach an event (a decrypted backend URL or key:
        a failing tool may print it)."""
        if value and len(value) >= 4:
            with self._lock:
                self._secrets.add(value)
        return value

    def cancel(self) -> None:
        """Ask the run to stop. The hooks are called on every call: a second
        cancel is how a caller insists (infra.py then stops the engine at
        once instead of letting the step in flight finish)."""
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
