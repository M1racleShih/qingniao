"""/control/v1 API: authenticated gateway administration.

Every endpoint requires the gateway control token (a bearer distinct from
any instance token). Instance tokens are rejected explicitly.
"""

from __future__ import annotations

import hmac

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import errors
from .gateway import Gateway
from .tokens import token_hash

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def error_response(exc: errors.ApiError) -> JSONResponse:
    return JSONResponse(exc.to_json(), status_code=exc.status_code)


def require_admin(request: Request, admin_token_hash: str) -> None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise errors.ApiError(401, errors.INVALID_ADMIN_TOKEN, "control API requires a bearer token")
    token = header[7:].strip()
    if not hmac.compare_digest(token_hash(token), admin_token_hash):
        gateway: Gateway = request.app.state.gateway
        if gateway.is_instance_token(token):
            raise errors.ApiError(403, errors.INSTANCE_TOKEN_NOT_ADMIN, "instance tokens cannot call the control API")
        raise errors.ApiError(401, errors.INVALID_ADMIN_TOKEN, "invalid control token")


async def read_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception as exc:
        raise errors.ApiError(400, errors.INVALID_JSON, "request body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise errors.ApiError(400, errors.INVALID_REQUEST, "request body must be a JSON object")
    return data


def gateway_of(request: Request) -> Gateway:
    return request.app.state.gateway


async def get_config(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        gateway = gateway_of(request)
        return JSONResponse(
            {"revision": gateway.config_revision, "config": gateway.config.to_json()}
        )
    except errors.ApiError as exc:
        return error_response(exc)


async def put_config(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        data = await read_json(request)
        gateway = gateway_of(request)
        gateway.apply_config(data)
        return JSONResponse(
            {
                "applied": True,
                "revision": gateway.config_revision,
                "config": gateway.config.to_json(),
            }
        )
    except errors.ApiError as exc:
        return error_response(exc)


async def create_instance(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        data = await read_json(request)
        gateway = gateway_of(request)
        routes = data.get("routes")
        if routes is not None and not isinstance(routes, dict):
            raise errors.ApiError(400, errors.INVALID_REQUEST, "routes must be an object")
        instance, token = gateway.create_instance(
            label=data.get("label"),
            model=data.get("model"),
            aux_model=data.get("aux_model"),
            route_overrides=routes,
        )
        return JSONResponse(
            {"instance": gateway.instance_status(instance), "token": token},
            status_code=201,
        )
    except errors.ApiError as exc:
        return error_response(exc)


def _pagination(request: Request) -> tuple[int, int]:
    raw_offset = request.query_params.get("offset", "0")
    raw_limit = request.query_params.get("limit", str(DEFAULT_LIMIT))
    try:
        offset = int(raw_offset)
        limit = int(raw_limit)
    except ValueError as exc:
        raise errors.ApiError(400, errors.INVALID_PAGINATION, "offset and limit must be integers") from exc
    if offset < 0:
        raise errors.ApiError(400, errors.INVALID_PAGINATION, "offset must be >= 0")
    if limit < 1:
        raise errors.ApiError(400, errors.INVALID_PAGINATION, "limit must be >= 1")
    return offset, min(limit, MAX_LIMIT)


async def list_instances(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        offset, limit = _pagination(request)
        gateway = gateway_of(request)
        instances, total, next_offset = gateway.list_instances(offset=offset, limit=limit)
        return JSONResponse(
            {
                "instances": instances,
                "offset": offset,
                "limit": limit,
                "total": total,
                "next_offset": next_offset,
            }
        )
    except errors.ApiError as exc:
        return error_response(exc)


async def instance_detail(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        gateway = gateway_of(request)
        instance = gateway.get_instance(request.path_params["instance_id"])
        if instance is None:
            raise errors.ApiError(404, errors.INSTANCE_NOT_FOUND, "no such instance")
        return JSONResponse({"instance": gateway.instance_status(instance)})
    except errors.ApiError as exc:
        return error_response(exc)


async def renew_instance(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        gateway = gateway_of(request)
        instance = gateway.renew_instance(request.path_params["instance_id"])
        return JSONResponse({"instance": gateway.instance_status(instance)})
    except errors.ApiError as exc:
        return error_response(exc)


async def end_instance(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        gateway = gateway_of(request)
        instance = gateway.end_instance(request.path_params["instance_id"])
        return JSONResponse({"ended": True, "instance": gateway.instance_status(instance)})
    except errors.ApiError as exc:
        return error_response(exc)


async def put_route(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        data = await read_json(request)
        request_model = data.get("request_model")
        catalog_model = data.get("model")
        expected_revision = data.get("expected_revision")
        if not isinstance(request_model, str) or not request_model:
            raise errors.ApiError(400, errors.INVALID_REQUEST, "request_model must be a non-empty string")
        if not isinstance(catalog_model, str) or not catalog_model:
            raise errors.ApiError(400, errors.INVALID_REQUEST, "model must be a non-empty string")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise errors.ApiError(400, errors.INVALID_REQUEST, "expected_revision must be an integer")
        gateway = gateway_of(request)
        instance, snapshot = gateway.set_route(
            request.path_params["instance_id"], request_model, catalog_model, expected_revision
        )
        return JSONResponse(
            {
                "applied": True,
                "revision": instance.revision,
                "route": {
                    "request_model": snapshot.request_model,
                    "catalog_model": snapshot.catalog_model,
                    "provider": snapshot.provider_id,
                    "upstream_model": snapshot.upstream_model,
                },
                "instance": gateway.instance_status(instance),
            }
        )
    except errors.ApiError as exc:
        return error_response(exc)


async def list_requests(request: Request):
    try:
        require_admin(request, request.app.state.admin_token_hash)
        offset, limit = _pagination(request)
        instance_id = request.query_params.get("instance_id") or None
        gateway = gateway_of(request)
        records, total, next_offset = gateway.list_requests(
            offset=offset, limit=limit, instance_id=instance_id
        )
        return JSONResponse(
            {
                "requests": records,
                "offset": offset,
                "limit": limit,
                "total": total,
                "next_offset": next_offset,
            }
        )
    except errors.ApiError as exc:
        return error_response(exc)


def build_control_routes() -> list[Route]:
    return [
        Route("/config", get_config, methods=["GET"]),
        Route("/config", put_config, methods=["PUT"]),
        Route("/instances", create_instance, methods=["POST"]),
        Route("/instances", list_instances, methods=["GET"]),
        Route("/instances/{instance_id}", instance_detail, methods=["GET"]),
        Route("/instances/{instance_id}", end_instance, methods=["DELETE"]),
        Route("/instances/{instance_id}/renew", renew_instance, methods=["POST"]),
        Route("/instances/{instance_id}/routes", put_route, methods=["PUT"]),
        Route("/requests", list_requests, methods=["GET"]),
    ]
