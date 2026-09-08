"""Incremental SSE parsing with bounded pending state.

The parser is fed raw upstream chunks and yields parsed events while
relaying bytes verbatim. The pending, not-yet-dispatched state (partial
line, accumulated data lines and event name) is capped so a hostile or
broken upstream cannot make the gateway hold unbounded output in memory,
while a large chunk containing many already-complete small events drains
normally. The cap counts characters of pending data.

The usage observer follows the Anthropic streaming contract: usage fields
arrive as cumulative values in message_start / message_delta and are
replaced, never summed.
"""

from __future__ import annotations

import codecs
import json
from typing import Iterable

DEFAULT_MAX_PENDING = 1 << 20
USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class SSEBufferOverflowError(Exception):
    """The pending parse state exceeded its bound."""


class SSEParser:
    """Incremental server-sent-events parser (LF, CRLF and CR line ends)."""

    def __init__(self, max_pending_chars: int = DEFAULT_MAX_PENDING):
        self.max_pending_chars = max_pending_chars
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buf = ""
        self._data_lines: list[str] = []
        self._event_name: str | None = None

    @property
    def pending_chars(self) -> int:
        # +1 per pending data line accounts for the line break itself so
        # that empty data lines cannot accumulate without bound either.
        return (
            len(self._buf)
            + sum(len(line) + 1 for line in self._data_lines)
            + len(self._event_name or "")
        )

    def _check_pending(self) -> None:
        if self.pending_chars > self.max_pending_chars:
            raise SSEBufferOverflowError(
                f"SSE pending parse state exceeded {self.max_pending_chars} characters without a complete event"
            )

    def feed(self, chunk: bytes) -> list[tuple[str | None, str]]:
        text = self._decoder.decode(chunk)
        self._buf += text
        events: list[tuple[str | None, str]] = []
        while True:
            idx, sep_len = self._find_line_break()
            if idx is None:
                break
            line = self._buf[:idx]
            self._buf = self._buf[idx + sep_len :]
            self._handle_line(line, events)
        # Bound only retained state: a chunk full of complete small events
        # drained above; what remains is the unterminated tail plus any
        # accumulated data lines of an unfinished event.
        self._check_pending()
        return events

    def close(self) -> list[tuple[str | None, str]]:
        """End of stream: discard any unfinished event.

        Per SSE semantics an event that never saw its terminating blank line
        is abandoned, so a truncated final event cannot promote an
        incomplete stream to success; usage from earlier complete events is
        preserved by the observer.
        """
        self._decoder.decode(b"", final=True)
        self._buf = ""
        self._data_lines = []
        self._event_name = None
        return []

    def _find_line_break(self) -> tuple[int | None, int]:
        lf = self._buf.find("\n")
        cr = self._buf.find("\r")
        if cr != -1 and (lf == -1 or cr < lf):
            if cr == len(self._buf) - 1:
                # A trailing CR may be the first half of a split CRLF: wait.
                return None, 0
            if self._buf[cr + 1] == "\n":
                return cr, 2
            return cr, 1
        if lf != -1:
            return lf, 1
        return None, 0

    def _handle_line(self, line: str, events: list[tuple[str | None, str]]) -> None:
        if line == "":
            self._dispatch(events)
            return
        if line.startswith(":"):
            return
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value)
        elif field == "event":
            self._event_name = value

    def _dispatch(self, events: list[tuple[str | None, str]]) -> None:
        if self._data_lines or self._event_name is not None:
            events.append((self._event_name, "\n".join(self._data_lines)))
        self._data_lines = []
        self._event_name = None


class UsageObserver:
    """Observes Anthropic SSE events; usage values are cumulative updates."""

    def __init__(self):
        self.usage: dict[str, int | None] = {}
        self.saw_message_stop = False
        self.saw_error = False

    def observe_all(self, events: Iterable[tuple[str | None, str]]) -> None:
        for name, data_text in events:
            self.observe(name, data_text)

    def observe(self, event_name: str | None, data_text: str) -> None:
        if not data_text.strip():
            return
        try:
            data = json.loads(data_text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type") if isinstance(data.get("type"), str) else event_name
        if kind == "message_start":
            message = data.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self._merge(usage)
        elif kind == "message_delta":
            usage = data.get("usage")
            if isinstance(usage, dict):
                self._merge(usage)
        elif kind == "message_stop":
            self.saw_message_stop = True
        elif kind == "error":
            self.saw_error = True

    def _merge(self, usage: dict) -> None:
        for key in USAGE_KEYS:
            if key not in usage:
                continue
            value = usage[key]
            if isinstance(value, bool) or not isinstance(value, int):
                self.usage[key] = None
            else:
                self.usage[key] = value
