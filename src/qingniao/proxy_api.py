"""/v1 proxy endpoints: instance authentication and upstream forwarding.

Each request is authenticated against an instance, matched against that
instance's exact request-model routes, and only then forwarded with the
immutable route snapshot captured before any outgoing I/O. The client's own
Authorization/x-api-key headers are never forwarded; the snapshot's provider
credential is injected instead. No retries, no redirects, no fallback.

Downstream disconnects are observed while awaiting upstream response
headers, and every upstream response is always released (also on downstream
cancellation, upstream errors and pre-header cancellations). Streaming
requests are relayed chunk by chunk; SSE traffic is observed in passing for
bounded usage metadata without altering the bytes on the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from . import errors
from .control_api import error_response
from .gateway import Gateway, RouteSnapshot, sanitize_usage
from .sse import SSEBufferOverflowError, SSEParser, UsageObserver

FORWARDED_HEADERS = ("anthropic-version", "anthropic-beta", "accept")


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


async def _read_body(request: Request) -> dict:
    try:
        data = json.loads(await request.body())
    except Exception as exc:
        raise errors.ApiError(400, errors.INVALID_JSON, "request body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise errors.ApiError(400, errors.INVALID_REQUEST, "request body must be a JSON object")
    return data


def _build_headers(request: Request, snapshot: RouteSnapshot, credential: str) -> dict:
    headers = {"content-type": "application/json"}
    for name in FORWARDED_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    if snapshot.auth == "bearer":
        headers["authorization"] = f"Bearer {credential}"
    else:
        headers["x-api-key"] = credential
    return headers


def _try_json(content: bytes) -> object:
    try:
        return json.loads(content)
    except Exception:
        return None


def _extract_usage(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    # count_tokens responses carry token counts at the top level
    if any(key in payload for key in ("input_tokens", "output_tokens")):
        return payload
    return None


async def _wait_disconnect(request: Request) -> None:
    """Return when the downstream client disconnects.

    The request body has already been consumed at admission, so the next
    ASGI receive message is the disconnect notification.
    """
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def _race_disconnect(awaitable, disconnect_task: asyncio.Future, closer):
    """Await an upstream operation, racing the downstream disconnect.

    ASGI does not cancel an endpoint whose client went away, so the
    disconnect is observed explicitly for every phase that waits on the
    upstream (opening the stream and reading buffered bodies alike). Every
    exit path is owned here: operation completion, downstream disconnect,
    operation exception and external cancellation of the caller all cancel
    and await the unfinished operation task, and cancellation-side exits
    release the upstream via `closer` before propagating. `closer` must be
    safe to call more than once.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        # External cancellation of the caller: the operation never
        # completed, so cancel it and release the upstream here.
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await closer()
        raise
    if disconnect_task in done and task not in done:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await closer()
        raise asyncio.CancelledError()
    # The operation completed (or raised); its result or exception
    # propagates. Cancelling the disconnect watcher stays with the owner.
    return task.result()


async def _close_quietly(upstream_cm) -> None:
    async def _close() -> None:
        try:
            await upstream_cm.__aexit__(None, None, None)
        except Exception:
            pass

    # Shielded so re-cancellation cannot skip releasing the upstream; safe
    # to call again after an earlier close.
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.shield(_close())


async def _forward(request: Request) -> Response:
    gateway: Gateway = request.app.state.gateway
    bearer = _bearer(request)
    instance = None
    request_model = "<unknown>"
    try:
        instance = gateway.authenticate(bearer)
        body = await _read_body(request)
        request_model = body.get("model")
        if not isinstance(request_model, str) or not request_model:
            raise errors.ApiError(400, errors.INVALID_REQUEST, "body must include a model string")
        # Admission boundary: the body has arrived; re-validate identity and
        # lifecycle now so an instance ended or expired during the upload is
        # rejected before the snapshot is captured. From here to the outgoing
        # request there is no await on shared gateway state.
        instance = gateway.authenticate(bearer)
        snapshot = gateway.route_for(instance, request_model)
        credential = gateway.credential(snapshot)
    except errors.ApiError as exc:
        rejected_id = gateway.begin_request(
            instance_id=instance.id if instance is not None else "<unauthenticated>",
            request_model=request_model if isinstance(request_model, str) else "<invalid>",
        )
        gateway.finish_request(rejected_id, outcome="failed", error_code=exc.code)
        return error_response(exc)

    record_id = gateway.begin_request(
        instance_id=instance.id,
        request_model=request_model,
        provider_id=snapshot.provider_id,
        upstream_model=snapshot.upstream_model,
        route_revision=snapshot.route_revision,
    )
    out_body = dict(body)
    out_body["model"] = snapshot.upstream_model
    query = request.url.query
    upstream_url = snapshot.base_url + request.url.path + (f"?{query}" if query else "")
    headers = _build_headers(request, snapshot, credential)
    client = request.app.state.http_client
    return await _relay(request, gateway, record_id, client, upstream_url, headers, out_body)


async def _relay(
    request: Request,
    gateway: Gateway,
    record_id: str,
    client,
    upstream_url: str,
    headers: dict,
    body: dict,
) -> Response:
    payload = json.dumps(body).encode("utf-8")
    upstream_cm = client.stream("POST", upstream_url, content=payload, headers=headers)
    disconnect = asyncio.ensure_future(_wait_disconnect(request))
    try:
        try:
            upstream = await _race_disconnect(upstream_cm.__aenter__(), disconnect, lambda: _close_quietly(upstream_cm))
        except asyncio.CancelledError:
            gateway.finish_request(record_id, outcome="cancelled")
            raise
        except Exception:
            gateway.finish_request(record_id, outcome="failed", error_code="upstream_error")
            return error_response(errors.ApiError(502, errors.UPSTREAM_ERROR, "upstream request failed"))

        status_code = upstream.status_code
        content_type = upstream.headers.get("content-type", "application/json")
        is_sse = "text/event-stream" in content_type and status_code < 400

        if not is_sse:
            try:
                content = await _race_disconnect(upstream.aread(), disconnect, lambda: _close_quietly(upstream_cm))
            except asyncio.CancelledError:
                gateway.finish_request(record_id, outcome="cancelled")
                raise
            except Exception:
                await _close_quietly(upstream_cm)
                gateway.finish_request(record_id, outcome="failed", status_code=status_code, error_code="upstream_error")
                return error_response(errors.ApiError(502, errors.UPSTREAM_ERROR, "upstream request failed"))
            await _close_quietly(upstream_cm)
            usage = sanitize_usage(_extract_usage(_try_json(content)))
            gateway.finish_request(
                record_id,
                outcome="success" if status_code < 400 else "failed",
                status_code=status_code,
                usage=usage,
            )
            return Response(content=content, status_code=status_code, media_type=content_type)

        # Streaming hand-off: the watcher must not consume the disconnect
        # message Starlette's response needs for its own listener.
        disconnect.cancel()
        try:
            await disconnect
        except (asyncio.CancelledError, Exception):
            pass
    finally:
        disconnect.cancel()

    parser = SSEParser()
    observer = UsageObserver()
    finished = False

    def finish(outcome: str, error_code: str | None = None) -> None:
        nonlocal finished
        if not finished:
            finished = True
            gateway.finish_request(
                record_id,
                outcome=outcome,
                status_code=status_code,
                usage=observer.usage or None,
                error_code=error_code,
            )

    async def relay():
        try:
            # Decoded iteration: content encodings (e.g. gzip) are decoded by
            # HTTPX and never forwarded, so downstream always receives
            # identity-encoded SSE with intact event and usage semantics.
            async for chunk in upstream.aiter_bytes():
                observer.observe_all(parser.feed(chunk))
                gateway.note_request_usage(record_id, observer.usage or None)
                yield chunk
            observer.observe_all(parser.close())
            if observer.saw_error:
                finish("failed", "upstream_sse_error")
            elif observer.saw_message_stop:
                finish("success")
            else:
                finish("incomplete")
        except (asyncio.CancelledError, GeneratorExit):
            finish("cancelled")
            raise
        except SSEBufferOverflowError:
            finish("incomplete", "sse_buffer_overflow")
            raise
        except Exception:
            # The upstream stream died before message_stop: the request is
            # incomplete (never success) and the known usage is retained.
            finish("incomplete", "upstream_error")
            raise
        finally:
            await _close_quietly(upstream_cm)

    return StreamingResponse(relay(), status_code=status_code, media_type=content_type)


async def messages(request: Request):
    return await _forward(request)


async def count_tokens(request: Request):
    return await _forward(request)


def build_proxy_routes() -> list[Route]:
    return [
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/messages/count_tokens", count_tokens, methods=["POST"]),
    ]
