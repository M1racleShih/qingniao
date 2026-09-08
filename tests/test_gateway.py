from __future__ import annotations

import pytest

from qingniao import errors
from qingniao.config import ConfigStore, validate_config
from qingniao.gateway import Gateway


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config(main_dest="model-p", aux_dest="model-p"):
    return {
        "providers": {
            "prov-p": {
                "base_url": "http://127.0.0.1:9101",
                "credential_env": "QING_TEST_P",
                "auth": "bearer",
            },
            "prov-q": {
                "base_url": "http://127.0.0.1:9102",
                "credential_env": "QING_TEST_Q",
                "auth": "bearer",
            },
        },
        "models": {
            "model-p": {"provider": "prov-p", "upstream_model": "vendor/p"},
            "model-q": {"provider": "prov-q", "upstream_model": "vendor/q"},
        },
        "defaults": {
            "model": "req-main",
            "aux_model": "req-aux",
            "routes": {"req-main": main_dest, "req-aux": aux_dest},
        },
    }


def make_gateway(tmp_path=None, config=None, clock=None) -> Gateway:
    store = ConfigStore(tmp_path / "config.json") if tmp_path else None
    cfg = validate_config(config if config is not None else make_config())
    return Gateway(cfg, store, clock=clock or FakeClock())


def api_code(excinfo) -> str:
    return excinfo.value.code


# ------------------------------------------------------------- snapshots


def test_same_catalog_two_instances_independent_selection(tmp_path):
    gw = make_gateway(tmp_path)
    a, token_a = gw.create_instance()
    b, token_b = gw.create_instance()
    assert a.id != b.id and token_a != token_b
    for inst in (a, b):
        snap = inst.routes["req-main"]
        assert (snap.provider_id, snap.upstream_model, snap.base_url) == ("prov-p", "vendor/p", "http://127.0.0.1:9101")
    # explicit per-instance override only affects that instance
    c, _ = gw.create_instance(route_overrides={"req-main": "model-q"})
    assert c.routes["req-main"].provider_id == "prov-q"
    assert a.routes["req-main"].provider_id == "prov-p"


def test_defaults_change_leaves_old_instance_new_instance_gets_new(tmp_path):
    gw = make_gateway(tmp_path)
    a, _ = gw.create_instance()
    old = a.routes["req-main"]
    data = make_config(main_dest="model-q")
    gw.apply_config(data)
    # old instance keeps its resolved snapshot
    assert a.routes["req-main"] is old
    assert old.provider_id == "prov-p"
    # new default instance resolves to Q
    b, _ = gw.create_instance()
    assert b.routes["req-main"].provider_id == "prov-q"
    # explicit selection of P still wins for a new instance
    c, _ = gw.create_instance(route_overrides={"req-main": "model-p"})
    assert c.routes["req-main"].provider_id == "prov-p"


def test_shared_catalog_edit_and_delete_keep_old_snapshots(tmp_path):
    gw = make_gateway(tmp_path)
    a, _ = gw.create_instance()

    edited = make_config()
    edited["providers"]["prov-p"]["base_url"] = "http://127.0.0.1:9999"
    edited["providers"]["prov-p"]["credential_env"] = "QING_TEST_P2"
    edited["models"]["model-p"]["upstream_model"] = "vendor/p2"
    gw.apply_config(edited)
    status = gw.instance_status(a)
    assert status["routes"]["req-main"]["base_url"] == "http://127.0.0.1:9101"
    assert status["routes"]["req-main"]["upstream_model"] == "vendor/p"
    assert status["routes"]["req-main"]["catalog_present"] is False

    # deleting the catalog entry still keeps the snapshot usable
    deleted = make_config()
    deleted["models"].pop("model-p")
    deleted["defaults"]["routes"] = {"req-main": "model-q", "req-aux": "model-q"}
    gw.apply_config(deleted)
    status = gw.instance_status(a)
    assert status["routes"]["req-main"]["upstream_model"] == "vendor/p"
    assert status["routes"]["req-main"]["catalog_present"] is False


def test_create_instance_validates_wholly_before_registration(tmp_path):
    gw = make_gateway(tmp_path)
    with pytest.raises(errors.ApiError) as e1:
        gw.create_instance(route_overrides={"req-main": "model-missing"})
    assert api_code(e1) == errors.INVALID_SELECTION
    with pytest.raises(errors.ApiError) as e2:
        gw.create_instance(model="req-other")
    assert api_code(e2) == errors.INVALID_SELECTION
    with pytest.raises(errors.ApiError) as e3:
        gw.create_instance(label="  ")
    assert api_code(e3) == errors.INVALID_SELECTION
    assert gw.instance_count == 0


def test_missing_defaults_require_explicit_selection(tmp_path):
    empty = {"providers": {}, "models": {}, "defaults": {"model": None, "aux_model": None, "routes": {}}}
    gw = make_gateway(tmp_path, config=empty)
    with pytest.raises(errors.ApiError) as excinfo:
        gw.create_instance()
    assert api_code(excinfo) == errors.NO_DEFAULT_MODEL
    assert gw.instance_count == 0


def test_non_string_selection_rejected_wholesale(tmp_path):
    gw = make_gateway(tmp_path)
    for bad_model in ([], {}, 7, ""):
        with pytest.raises(errors.ApiError) as e1:
            gw.create_instance(model=bad_model)
        assert api_code(e1) == errors.INVALID_SELECTION
        with pytest.raises(errors.ApiError) as e2:
            gw.create_instance(aux_model=bad_model)
        assert api_code(e2) == errors.INVALID_SELECTION
    assert gw.instance_count == 0


def test_lease_expires_at_exact_deadline(tmp_path):
    clock = FakeClock()
    gw = make_gateway(tmp_path, clock=clock)
    a, token = gw.create_instance()
    clock.advance(29.999)
    assert gw.authenticate(token).id == a.id
    clock.advance(0.001)  # exactly at the deadline
    with pytest.raises(errors.ApiError) as e1:
        gw.authenticate(token)
    assert api_code(e1) == errors.INSTANCE_EXPIRED
    with pytest.raises(errors.ApiError) as e2:
        gw.renew_instance(a.id)
    assert api_code(e2) == errors.INSTANCE_EXPIRED
    with pytest.raises(errors.ApiError) as e3:
        gw.set_route(a.id, "req-main", "model-q", expected_revision=0)
    assert api_code(e3) == errors.INSTANCE_EXPIRED


# ------------------------------------------------------------- targeting


def test_label_targeting_semantics(tmp_path):
    gw = make_gateway(tmp_path)
    a, _ = gw.create_instance(label="worker")
    b, _ = gw.create_instance(label="worker")
    assert gw.resolve_target(a.id).id == a.id
    with pytest.raises(errors.ApiError) as e_amb:
        gw.resolve_target("worker")
    assert api_code(e_amb) == errors.AMBIGUOUS_LABEL
    assert set(e_amb.value.details["candidates"]) == {a.id, b.id}
    c, _ = gw.create_instance(label="solo")
    assert gw.resolve_target("solo").id == c.id
    with pytest.raises(errors.ApiError) as e1:
        gw.resolve_target("nope")
    assert api_code(e1) == errors.INSTANCE_NOT_FOUND
    # ended target fails closed without mutation
    gw.end_instance(a.id)
    inst = gw.resolve_target(a.id)
    assert inst.ended


def test_lease_expiry_and_renewal_with_controlled_clock(tmp_path):
    clock = FakeClock()
    gw = make_gateway(tmp_path, clock=clock)
    a, token = gw.create_instance()
    assert gw.authenticate(token).id == a.id

    clock.advance(31)
    with pytest.raises(errors.ApiError) as e1:
        gw.authenticate(token)
    assert api_code(e1) == errors.INSTANCE_EXPIRED
    assert gw.instance_status(a)["state"] == "expired"
    with pytest.raises(errors.ApiError) as e2:
        gw.renew_instance(a.id)
    assert api_code(e2) == errors.INSTANCE_EXPIRED

    b, token_b = gw.create_instance()
    clock.advance(20)
    gw.renew_instance(b.id)
    clock.advance(20)
    assert gw.authenticate(token_b).id == b.id
    clock.advance(11)
    with pytest.raises(errors.ApiError):
        gw.authenticate(token_b)


def test_end_instance_then_authenticate_fails_closed(tmp_path):
    gw = make_gateway(tmp_path)
    a, token = gw.create_instance()
    gw.end_instance(a.id)
    with pytest.raises(errors.ApiError) as excinfo:
        gw.authenticate(token)
    assert api_code(excinfo) == errors.INSTANCE_ENDED
    with pytest.raises(errors.ApiError):
        gw.renew_instance(a.id)
    assert gw.instance_status(a)["state"] == "ended"
    gw.end_instance(a.id)  # idempotent


# --------------------------------------------------------------- routing


def test_route_set_cas_conflict_and_atomic_replace(tmp_path):
    gw = make_gateway(tmp_path)
    a, _ = gw.create_instance()
    b, _ = gw.create_instance()
    assert a.revision == 0

    with pytest.raises(errors.ApiError) as excinfo:
        gw.set_route(a.id, "req-main", "model-q", expected_revision=7)
    assert api_code(excinfo) == errors.REVISION_CONFLICT
    assert excinfo.value.details["current_revision"] == 0
    assert a.routes["req-main"].provider_id == "prov-p"

    # catalog change between creation and route set is picked up by route set
    edited = make_config()
    edited["providers"]["prov-q"]["base_url"] = "http://127.0.0.1:9103"
    gw.apply_config(edited)

    inst, snap = gw.set_route(a.id, "req-main", "model-q", expected_revision=0)
    assert inst.revision == 1
    assert snap.provider_id == "prov-q"
    assert snap.base_url == "http://127.0.0.1:9103"
    assert snap.route_revision == 1
    assert a.routes["req-main"] is snap
    # other instance untouched
    assert b.routes["req-main"].provider_id == "prov-p"
    assert b.revision == 0

    with pytest.raises(errors.ApiError) as e2:
        gw.set_route(a.id, "req-main", "model-q", expected_revision=0)
    assert e2.value.code == errors.REVISION_CONFLICT
    assert e2.value.details["current_revision"] == 1


def test_route_set_unknown_destination_rejected(tmp_path):
    gw = make_gateway(tmp_path)
    a, _ = gw.create_instance()
    with pytest.raises(errors.ApiError) as excinfo:
        gw.set_route(a.id, "req-main", "model-missing", expected_revision=0)
    assert api_code(excinfo) == errors.INVALID_SELECTION
    assert a.revision == 0


def test_route_set_on_ended_or_expired_instance(tmp_path):
    clock = FakeClock()
    gw = make_gateway(tmp_path, clock=clock)
    a, _ = gw.create_instance()
    gw.end_instance(a.id)
    with pytest.raises(errors.ApiError) as e1:
        gw.set_route(a.id, "req-main", "model-q", expected_revision=0)
    assert api_code(e1) == errors.INSTANCE_ENDED
    b, _ = gw.create_instance()
    clock.advance(31)
    with pytest.raises(errors.ApiError) as e2:
        gw.set_route(b.id, "req-main", "model-q", expected_revision=0)
    assert api_code(e2) == errors.INSTANCE_EXPIRED


def test_invalid_apply_leaves_config_and_instances_unchanged(tmp_path):
    gw = make_gateway(tmp_path)
    gw.apply_config(make_config())
    a, _ = gw.create_instance()
    snapshot = a.routes["req-main"]
    revision = gw.config_revision
    persisted = (tmp_path / "config.json").read_text()

    bad = make_config()
    bad["providers"]["prov-p"]["base_url"] = "http://localhost:70000"
    with pytest.raises(errors.ApiError) as excinfo:
        gw.apply_config(bad)
    assert api_code(excinfo) == errors.INVALID_CONFIG

    assert gw.config_revision == revision
    assert gw.instance_status(a)["routes"]["req-main"]["base_url"] == "http://127.0.0.1:9101"
    assert a.routes["req-main"] is snapshot
    assert (tmp_path / "config.json").read_text() == persisted


def test_unknown_request_model_no_fallback(tmp_path):
    gw = make_gateway(tmp_path)
    a, _ = gw.create_instance()
    with pytest.raises(errors.ApiError) as excinfo:
        gw.route_for(a, "claude-unknown-model")
    assert api_code(excinfo) == errors.UNKNOWN_MODEL
    assert excinfo.value.details["request_model"] == "claude-unknown-model"


def test_instance_token_entropy_and_id_separation(tmp_path):
    gw = make_gateway(tmp_path)
    _, token = gw.create_instance()
    # token_urlsafe(32) -> 32 bytes >= 256 bits, 43 chars
    assert len(token) >= 43
    import re as _re

    assert _re.match(r"^[A-Za-z0-9_-]+$", token)
    inst2, token2 = gw.create_instance()
    assert token2 != token and inst2.id.startswith("i-") and len(inst2.id) == 14


def test_pagination_listing(tmp_path):
    gw = make_gateway(tmp_path)
    for _ in range(7):
        gw.create_instance()
    page, total, next_offset = gw.list_instances(offset=0, limit=4)
    assert total == 7 and len(page) == 4 and next_offset == 4
    page2, total2, next_offset2 = gw.list_instances(offset=4, limit=4)
    assert total2 == 7 and len(page2) == 3 and next_offset2 is None
