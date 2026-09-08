"""OpenAPI contract tests: structural validity and agreement with the runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate as validate_spec

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def schema_for(spec: dict, component: str) -> dict:
    return spec["components"]["schemas"][component]


def validator_for(spec: dict, component: str):
    """Validate payloads against a component of the real document root.

    The full OpenAPI document is registered as the base resource, so
    $ref chains resolve through the original components root instead of a
    detached copy.
    """
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    base = "urn:qingniao:openapi"
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry = Registry().with_resource(base, resource)
    return Draft202012Validator({"$ref": f"{base}#/components/schemas/{component}"}, registry=registry)


def validator_at(spec: dict, pointer: str):
    """Validate payloads against any schema node of the real document root
    (e.g. a response media-type schema), resolving $ref through the
    registered components instead of a detached copy."""
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    base = "urn:qingniao:openapi"
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry = Registry().with_resource(base, resource)
    return Draft202012Validator({"$ref": f"{base}{pointer}"}, registry=registry)


def test_openapi_document_structurally_valid(spec):
    validate_spec(spec)


def test_every_path_template_variable_is_declared(spec):
    import re

    problems = []
    for path, item in spec["paths"].items():
        variables = set(re.findall(r"\{([^}]+)\}", path))
        for method, operation in item.items():
            if method not in ("get", "put", "post", "delete"):
                continue
            declared = set()
            for param in operation.get("parameters", []) + item.get("parameters", []):
                if param.get("in") == "path":
                    declared.add(param["name"])
            missing = variables - declared
            if missing:
                problems.append(f"{method.upper()} {path}: undeclared {sorted(missing)}")
    assert not problems


def test_proxy_endpoints_declare_required_request_body(spec):
    for path in ("/v1/messages", "/v1/messages/count_tokens"):
        operation = spec["paths"][path]["post"]
        body = operation.get("requestBody")
        assert body is not None and body.get("required") is True, path
        schema = body["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("ProxyMessageRequest")
        message_schema = schema_for(spec, "ProxyMessageRequest")
        assert message_schema["required"] == ["model"]
        assert message_schema.get("additionalProperties") is True


def test_pagination_limit_matches_clamping_runtime(spec):
    param = spec["paths"]["/control/v1/instances"]["get"]["parameters"][1]
    assert param["name"] == "limit"
    assert "maximum" not in param["schema"]
    assert param["schema"]["minimum"] == 1
    assert "clamp" in param["description"].lower()


def test_instance_create_schema_accepts_null_and_ignores_extras(spec):
    schema = schema_for(spec, "InstanceCreateRequest")
    assert schema.get("additionalProperties", True) is not False
    for prop in ("label", "model", "aux_model"):
        assert "null" in schema["properties"][prop]["type"]
    assert "null" in schema["properties"]["routes"]["type"]

    validator = validator_for(spec, "InstanceCreateRequest")
    assert validator.is_valid({"model": None, "aux_model": None, "routes": None, "label": None})
    assert validator.is_valid({"label": "a", "model": "req-main", "unknown_extra": 1})
    assert not validator.is_valid({"model": []})
    assert not validator.is_valid({"routes": {"k": 3}})


def test_shared_config_schema_matches_runtime_strictness(spec):
    schema = schema_for(spec, "SharedConfig")
    assert schema.get("additionalProperties") is False
    assert schema["required"] == ["defaults"]
    assert schema_for(spec, "Provider").get("additionalProperties") is False
    assert schema_for(spec, "ModelEntry").get("additionalProperties") is False
    defaults = schema["properties"]["defaults"]
    assert defaults.get("additionalProperties") is False
    assert set(defaults["required"]) == {"model", "aux_model", "routes"}

    validator = validator_for(spec, "SharedConfig")
    valid = {
        "providers": {"p": {"base_url": "http://127.0.0.1:1", "credential_env": "X", "auth": "bearer"}},
        "models": {"m": {"provider": "p", "upstream_model": "u"}},
        "defaults": {"model": "r", "aux_model": "r", "routes": {"r": "m"}},
    }
    assert validator.is_valid(valid)
    assert validator.is_valid({"defaults": {"model": None, "aux_model": None, "routes": {}}})
    # runtime rejects unknown top-level fields and a missing defaults section
    assert not validator.is_valid({**valid, "extra": 1})
    assert not validator.is_valid({"providers": {}, "models": {}})


async def test_runtime_rejects_config_without_defaults(make_gateway_app):
    from tests.conftest import make_config_dict

    async with make_gateway_app() as ctx:
        payload = make_config_dict()
        payload.pop("defaults")
        r = await ctx.client.put("/control/v1/config", headers=ctx.admin_headers, json=payload)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_config"


async def test_runtime_ignores_unknown_instance_create_fields(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.post(
            "/control/v1/instances",
            headers=ctx.admin_headers,
            json={"label": "a", "ignored_extra": {"x": 1}, "model": None, "aux_model": None},
        )
        assert r.status_code == 201
        assert r.json()["instance"]["label"] == "a"


PROXY_RELAY_STATUSES = {
    "/v1/messages": ["400", "401", "403", "500", "502"],
    "/v1/messages/count_tokens": ["400", "401", "403", "500", "502"],
}


def test_proxy_error_statuses_accept_relayed_and_gateway_bodies(spec):
    for path, statuses in PROXY_RELAY_STATUSES.items():
        responses = spec["paths"][path]["post"]["responses"]
        for status in statuses:
            response = responses[status]
            assert "relay" in response["description"].lower(), f"{path} {status}"
            content = response["content"]
            for media in ("application/json", "text/plain", "*/*"):
                assert media in content, f"{path} {status} missing {media}"
            validator = validator_at(
                spec,
                f"#/paths/~1v1~1messages/post/responses/{status}/content/application~1json/schema"
                if path == "/v1/messages"
                else f"#/paths/~1v1~1messages~1count_tokens/post/responses/{status}/content/application~1json/schema",
            )
            # gateway Error shape is valid at this status ...
            assert validator.is_valid({"error": {"code": "unknown_model", "message": "m"}})
            # ... and so is an arbitrary upstream body with no stable code,
            # including one that coincidentally nests an error-shaped object
            assert validator.is_valid({"detail": "invalid api key supplied"})
            assert validator.is_valid(["plain", "array"])
            assert validator.is_valid({"error": {"code": "anything", "from": "upstream"}})
            # the wildcard media type accepts bodies of any shape
            wildcard_pointer = (
                f"#/paths/~1v1~1messages/post/responses/{status}/content/*~1*/schema"
                if path == "/v1/messages"
                else f"#/paths/~1v1~1messages~1count_tokens/post/responses/{status}/content/*~1*/schema"
            )
            wildcard = validator_at(spec, wildcard_pointer)
            assert wildcard.is_valid({"type": "about:blank", "title": "Bad Request"})
            assert wildcard.is_valid("<html>bad request</html>")


def test_proxy_default_response_has_wildcard(spec):
    for path in ("/v1/messages", "/v1/messages/count_tokens"):
        default = spec["paths"][path]["post"]["responses"]["default"]
        assert "*/*" in default.get("content", {}), path
