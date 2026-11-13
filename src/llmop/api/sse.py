"""Incremental parser for ``text/event-stream`` bodies from LLM servers."""

from __future__ import annotations


class SSEParser:
    """Feed raw bytes, get the ``data`` payload of each complete event.

    Lines end with ``\\n`` (an optional ``\\r`` before it is dropped). Comments
    (``:...``), ``event:``/``id:``/``retry:`` fields are ignored. Lines that are
    not SSE at all (e.g. a plain JSON error body) are kept in :attr:`stray` so the
    caller can report them.
    """

    STRAY_LIMIT = 4096

    def __init__(self) -> None:
        self._buf = b""
        self._data: list[str] = []
        self.stray = ""

    def feed(self, chunk: bytes) -> list[str]:
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        events: list[str] = []
        for raw in lines:
            self._line(raw.rstrip(b"\r").decode("utf-8", "replace"), events)
        return events

    def flush(self) -> list[str]:
        """Call at end of stream: returns a last event that had no blank line after it."""
        events: list[str] = []
        if self._buf:
            self._line(self._buf.rstrip(b"\r").decode("utf-8", "replace"), events)
            self._buf = b""
        self._line("", events)
        return events

    def _line(self, line: str, events: list[str]) -> None:
        if not line:
            if self._data:
                events.append("\n".join(self._data))
                self._data = []
            return
        if line.startswith(":"):
            return
        field, sep, value = line.partition(":")
        if not sep or field not in ("data", "event", "id", "retry"):
            if len(self.stray) < self.STRAY_LIMIT:
                self.stray += line + "\n"
            return
        if field == "data":
            self._data.append(value[1:] if value.startswith(" ") else value)
