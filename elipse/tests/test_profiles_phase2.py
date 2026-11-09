"""Fase 2: capacidades (spec ∩ caps), gestión de perfiles, API y compuerta de tools."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import auth, db, profiles


@pytest.fixture()
def conn_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


CAPS = {"tools": ["get_current_datetime", "calculate"], "host_tools": True, "max_host_tools": 2,
        "pipelines": [], "vision": False, "attachments": False, "continuation": False, "mcp": [],
        "skills": [], "voice": {"allowed": False, "cloud": False}, "data_policy": "local_only"}
HOST = {"name": "viajes_del_dia", "description": "Viajes", "risk": "read",
        "parameters": {"type": "object", "properties": {"fecha": {"type": "string"}}}}
SPEC = {"role": "Asistente de RBE", "brief": "Consultas", "tools": {"builtin": ["calculate", "remember"], "host": [HOST]},
        "vision": True, "callback_url": "http://evil", "inventado": 1}


def test_builtin_names_match_tools_module():
    from app.core import tools
    assert profiles.BUILTIN_TOOL_NAMES == frozenset(tools.TOOL_FUNCTIONS)


def test_effective_trims_and_reports():
    eff, rej = profiles.effective_spec(profiles.sanitize_spec(SPEC)[0], CAPS)
    assert eff["tools"]["builtin"] == ["calculate"]
    assert [h["name"] for h in eff["tools"]["host"]] == ["viajes_del_dia"]
    assert eff["vision"] is False
    assert any("remember" in r for r in rej) and any("vision" in r for r in rej)


def test_no_caps_means_everything_off():
    eff, _ = profiles.effective_spec(SPEC, None)
    assert eff["tools"] == {"builtin": [], "host": [], "mcp": []} and not eff["vision"]


def test_host_tool_cannot_shadow_builtin_and_respects_max():
    spec = {"tools": {"host": [{"name": "read_file"}, {"name": "a_uno"}, {"name": "a_dos"}, {"name": "a_tres"}]}}
    eff, rej = profiles.effective_spec(spec, CAPS)
    assert [h["name"] for h in eff["tools"]["host"]] == ["a_uno", "a_dos"]
    assert any("read_file" in r for r in rej) and any("a_tres" in r for r in rej)


def test_data_policy_local_only_wins_and_voice_cloud_off():
    spec = {"data_policy": "allow_cloud", "voice": {"enabled": True, "cloud_allowed": True}}
    caps = dict(CAPS, voice={"allowed": True, "cloud": True}, data_policy="allow_cloud")
    eff, _ = profiles.effective_spec(spec, caps)
    assert eff["data_policy"] == "allow_cloud" and eff["voice"]["cloud_allowed"] is True
    eff, _ = profiles.effective_spec(spec, dict(caps, data_policy="local_only"))
    assert eff["voice"]["cloud_allowed"] is False


def test_default_capabilities_allow_all_and_do_not_copy_schema():
    schema = [{"function": {"name": "x"}}]
    caps = profiles.Capabilities(profiles.DEFAULT)
    assert caps.tool_allowed("lo_que_sea") and caps.filter_schema(schema) is schema
    assert caps.vision_allowed() and caps.pipeline_allowed("code")


def test_filter_schema_is_idempotent_and_adds_host():
    p = profiles.Profile("rbe", False, SPEC, CAPS)
    caps = profiles.Capabilities(p)
    schema = [{"function": {"name": "calculate"}}, {"function": {"name": "remember"}}, {"function": {"name": "mcp_x"}}]
    once = caps.filter_schema(schema)
    assert [t["function"]["name"] for t in once] == ["calculate", "viajes_del_dia"]
    assert caps.filter_schema(once) == once


def test_sync_create_update_and_admin_vs_host(conn_db):
    res = profiles.sync_profile("rbe", SPEC, CAPS, create_only=True)
    assert any("callback_url" in r for r in res["rejected"]) and any("inventado" in r for r in res["rejected"])
    with pytest.raises(ValueError):
        profiles.sync_profile("rbe", SPEC, CAPS, create_only=True)
    with pytest.raises(LookupError):
        profiles.sync_profile("otro", SPEC, None, update_only=True)
    with pytest.raises(ValueError):
        profiles.sync_profile("elipse", SPEC, None)
    stored = json.loads(profiles.get_profile_info("rbe")["spec"] and json.dumps(profiles.get_profile_info("rbe")["spec"]))
    assert "callback_url" not in stored


def test_callback_rules(conn_db):
    profiles.sync_profile("rbe", SPEC, None)
    for bad in ("ftp://x", "http://u:p@host/x", "javascript:1", "http:///x"):
        with pytest.raises(ValueError):
            profiles.set_callback("rbe", bad, generate=True)
    out = profiles.set_callback("rbe", "http://127.0.0.1:8001/elipse/tools/", generate=True)
    assert len(out["secret"]) == 64 and out["callback_url"].endswith("/tools")
    again = profiles.set_callback("rbe", "http://127.0.0.1:8001/x")        # conserva secreto
    assert "secret" not in again and again["has_secret"]
    assert profiles.get_callback("rbe")[0] == "http://127.0.0.1:8001/x"
    info = profiles.get_profile_info("rbe")
    assert "callback_secret" not in json.dumps(info)


def test_disable_delete_revokes_keys(conn_db):
    profiles.sync_profile("rbe", SPEC, None)
    auth.create_key("rbe-django", profile="rbe", caps=CAPS)
    assert profiles.get_profile("rbe") is not None
    profiles.set_enabled("rbe", False)
    assert profiles.get_profile("rbe") is None
    assert profiles.delete_profile("rbe") == {"deleted": True, "keys_revoked": 1}
    assert all(k["revoked"] for k in auth.list_keys() if k["profile"] == "rbe")


def test_orphan_profile_shows_up_unsynced(conn_db):
    auth.create_key("k", profile="rbe", caps=CAPS)
    p = profiles.get_profile_info("rbe")
    assert p and p["synced"] is False and p["keys"] == 1


@pytest.fixture()
def client(conn_db):
    from app.core.profiles_api import router
    app = FastAPI()
    app.include_router(router)
    admin = auth.create_key("admin")
    host = auth.create_key("rbe-django", profile="rbe", caps=CAPS)
    other = auth.create_key("otro", profile="otro", caps=CAPS)
    h = lambda k: {"Authorization": f"Bearer {k}"}
    return TestClient(app), h(admin), h(host), h(other)


def test_api_access_matrix(client):
    c, admin, host, other = client
    assert c.get("/v1/profiles", headers=host).status_code == 403          # lista: solo admin
    assert c.get("/v1/profiles").status_code == 401
    assert c.put("/v1/profiles/rbe/callback", headers=host, json={"url": "http://x/y"}).status_code == 403
    assert c.post("/v1/profiles", headers=host, json={"name": "zz", "spec": {}}).status_code == 403
    assert c.delete("/v1/profiles/rbe", headers=host).status_code == 403
    assert c.put("/v1/profiles/rbe", headers=other, json={"spec": SPEC}).status_code == 404   # ajeno
    r = c.put("/v1/profiles/rbe", headers=host, json={"spec": SPEC})
    assert r.status_code == 200 and any("remember" in x for x in r.json()["rejected"])
    g = c.get("/v1/profiles/rbe", headers=host).json()
    assert g["spec"]["tools"]["builtin"] == ["calculate"] and "callback_url" not in g and g["has_callback"] is False


def test_api_admin_flow_never_leaks_secret(client):
    c, admin, host, _ = client
    assert c.post("/v1/profiles", headers=admin, json={"name": "rbe", "spec": SPEC}).status_code == 200
    assert c.post("/v1/profiles", headers=admin, json={"name": "rbe", "spec": SPEC}).status_code == 400
    r = c.put("/v1/profiles/rbe/callback", headers=admin, json={"url": "http://127.0.0.1:9/x", "generate_secret": True})
    assert len(r.json()["secret"]) == 64
    assert "secret" not in json.dumps(c.get("/v1/profiles", headers=admin).json()).replace("has_secret", "")
    assert "127.0.0.1" not in json.dumps(c.get("/v1/profiles/rbe", headers=host).json())
    assert c.post("/v1/profiles/rbe/enabled", headers=admin, json={"enabled": False}).status_code == 200
    assert c.get("/v1/profiles/rbe", headers=host).status_code == 403      # perfil desactivado
    assert c.delete("/v1/profiles/rbe", headers=admin).json()["keys_revoked"] == 1
    assert c.delete("/v1/profiles/rbe", headers=admin).status_code == 404


def test_execute_tool_gate_and_r13():
    from app.core import tools
    p = profiles.Profile("rbe", False, {"tools": {"builtin": ["calculate"], "host": [HOST]}}, CAPS)
    assert tools.execute_tool("remember", {"content": "x"}, p) == "Error: herramienta 'remember' no habilitada en este perfil."
    assert tools.execute_tool("calculate", {"expression": "2+2", "profile": "elipse"}, p) == tools.execute_tool("calculate", {"expression": "2+2"})
    from unittest import mock
    with mock.patch.object(profiles, "get_callback", return_value=None):      # sin callback: no depende de la base real
        from unittest import mock
    with mock.patch.object(profiles, "get_callback", return_value=None):      # sin callback: no depende de la base real
        from unittest import mock
    with mock.patch.object(profiles, "get_callback", return_value=None):      # sin callback: no depende de la base real
        assert tools.execute_tool("viajes_del_dia", {}, p).startswith("Error: las herramientas del anfitrión")
    names = [t["function"]["name"] for t in tools.get_tools_schema_compact("hola", profile=p)]
    assert names == ["calculate", "viajes_del_dia"]
    # R1: el perfil por defecto no cambia
    assert tools.get_tools_schema() == tools.get_tools_schema(profiles.DEFAULT)