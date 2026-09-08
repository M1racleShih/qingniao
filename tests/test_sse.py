from __future__ import annotations

import pytest

from qingniao.sse import SSEBufferOverflowError, SSEParser, UsageObserver


def collect(parser: SSEParser, chunks: list[bytes]) -> list[tuple[str | None, str]]:
    events: list[tuple[str | None, str]] = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.close())
    return events


def test_parses_events_split_across_chunks():
    parser = SSEParser()
    events = collect(parser, [b"event: message_st", b"art\ndata: {\"type\":", b"\"message_start\"}\n\n"])
    assert events == [("message_start", '{"type":"message_start"}')]


def test_multiple_events_in_one_chunk_and_comments():
    parser = SSEParser()
    blob = b": ping comment\nevent: a\ndata: 1\n\nevent: b\ndata: 2\n\n"
    events = collect(parser, [blob])
    assert events == [("a", "1"), ("b", "2")]


def test_lf_crlf_and_cr_line_endings():
    parser = SSEParser()
    events = collect(
        parser,
        [
            b"event: a\ndata: 1\n\nevent: b\r\ndata: 2\r\n\n",
            # bare-CR event: the blank CR line resolves because more bytes follow
            b"event: c\rdata: 3\r\revent: d\ndata: 4\n\n",
        ],
    )
    assert events == [("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")]


def test_bare_cr_event_at_stream_end_is_discarded():
    # Ambiguous at EOF (the blank CR line may be the start of a split CRLF):
    # the conservative ruling discards the pending event, never promoting it.
    parser = SSEParser()
    events = collect(parser, [b"event: a\ndata: 1\n\nevent: c\rdata: 3\r"])
    assert events == [("a", "1")]


def test_split_crlf_across_chunks():
    parser = SSEParser()
    events = collect(parser, [b"event: a\ndata: 1\r", b"\n\r", b"\nevent: b\ndata: 2\n\n"])
    assert events == [("a", "1"), ("b", "2")]


def test_utf8_split_across_chunks():
    parser = SSEParser()
    text = "event: delta\ndata: {\"text\":\"你好世界\"}\n\n"
    raw = text.encode("utf-8")
    split = raw.index(b"\xe4\xbd") + 1  # split inside a multibyte character
    events = collect(parser, [raw[:split], raw[split:]])
    assert events == [("delta", '{"text":"你好世界"}')]


def test_multi_data_lines_joined_with_newline():
    parser = SSEParser()
    events = collect(parser, [b"event: x\ndata: line1\ndata: line2\n\n"])
    assert events == [("x", "line1\nline2")]


def test_pending_event_state_is_bounded_s1():
    """Many complete small events drain; unterminated data must not pile up."""
    parser = SSEParser(max_pending_chars=64)
    # complete events well over the cap in total -> fine
    events = collect(parser, [b"event: a\ndata: " + b"x" * 40 + b"\n\n"] * 20)
    assert len(events) == 20
    # unterminated data lines accumulate in the pending event -> bounded
    small = SSEParser(max_pending_chars=64)
    with pytest.raises(SSEBufferOverflowError):
        for _ in range(1000):
            small.feed(b"data: " + b"x" * 40 + b"\n")
    # a single unterminated line larger than the cap is bounded too
    big_line = SSEParser(max_pending_chars=64)
    with pytest.raises(SSEBufferOverflowError):
        big_line.feed(b"data: " + b"x" * 200)


def test_empty_data_lines_are_bounded_s1():
    # "data:\n" carries zero characters but the pending line itself must
    # still count towards the bound.
    parser = SSEParser(max_pending_chars=64)
    with pytest.raises(SSEBufferOverflowError):
        for _ in range(1000):
            parser.feed(b"data:\n")


def test_large_chunk_of_complete_small_events_drains_s1():
    parser = SSEParser(max_pending_chars=64)
    blob = b"data: x\n\n" * 100
    events = collect(parser, [blob])
    assert len(events) == 100
    assert all(event == (None, "x") for event in events)


def test_close_discards_unfinished_event_s2():
    parser = SSEParser()
    parser.feed(b'event: message_stop\ndata: {"type":"message_stop"}')
    assert parser.close() == []
    observer = UsageObserver()
    parser2 = SSEParser()
    observer.observe_all(
        parser2.feed(b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":7}}\n\n')
    )
    parser2.feed(b"event: message_stop\ndata: {\"type\":\"mess")
    observer.observe_all(parser2.close())
    assert observer.saw_message_stop is False
    assert observer.usage == {"output_tokens": 7}


def test_trailing_cr_without_lf_discarded_at_eof():
    parser = SSEParser()
    events = collect(parser, [b"event: a\ndata: 1\n\nevent: b\ndata: 2\r"])
    assert events == [("a", "1")]


def test_usage_observer_replaces_cumulative_values():
    observer = UsageObserver()
    script = [
        ("message_start", '{"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1}}}'),
        ("message_delta", '{"type":"message_delta","usage":{"output_tokens":5}}'),
        ("message_delta", '{"type":"message_delta","usage":{"output_tokens":8}}'),
        ("message_stop", '{"type":"message_stop"}'),
    ]
    observer.observe_all(script)
    assert observer.usage == {"input_tokens": 10, "output_tokens": 8}
    assert observer.saw_message_stop is True


def test_usage_observer_missing_zero_and_invalid():
    observer = UsageObserver()
    observer.observe_all(
        [
            ("message_start", '{"type":"message_start","message":{"usage":{}}}'),
            ("message_delta", '{"type":"message_delta","usage":{"output_tokens":0}}'),
            ("message_stop", '{"type":"message_stop"}'),
        ]
    )
    assert observer.usage == {"output_tokens": 0}

    observer2 = UsageObserver()
    observer2.observe_all(
        [
            ("message_delta", '{"type":"message_delta","usage":{"output_tokens":"eight"}}'),
            ("message_stop", '{"type":"message_stop"}'),
        ]
    )
    assert observer2.usage == {"output_tokens": None}


def test_usage_observer_error_event_and_garbage_data():
    observer = UsageObserver()
    observer.observe_all(
        [
            ("error", '{"type":"error","error":{"type":"overloaded_error"}}'),
            ("message_delta", "not json at all"),
            ("message_delta", "also not json"),
        ]
    )
    assert observer.saw_error is True
    assert observer.usage == {}
    assert observer.saw_message_stop is False
