"""Fase 3 — aislamiento por perfil: memoria, historial, prompt, ejecución de tools y acciones pendientes.
Corre con una base temporal y un Chroma en memoria (no carga el modelo de embeddings)."""
import json

import pytest

from app.core import db, memory, profiles
from app.core.profiles import DEFAULT, Capabilities, Profile


# ----------------------------------------------------------------------------- Chroma falso (en memoria)
class _Col:
    def __init__(self):
        self.d = {}

    def add(self, ids, documents, metadatas):
        for i, doc, m in zip(ids, documents, metadatas):
            self.d[i] = (doc, m)

    def count(self):
        return len(self.d)

    def query(self, query_texts, n_results, where=None):
        q = set(query_texts[0].lower().split())
        items = []
        for i, (doc, m) in self.d.items():
            w = set(doc.lower().split())
            items.append((1 - len(q & w) / max(len(q | w), 1), i, doc, m))
        items.sort(key=lambda x: x[0])
        items = items[:n_results]
        return {"ids": [[x[1] for x in items]], "documents": [[x[2] for x in items]],
                "metadatas": [[x[3] for x in items]], "distances": [[x[0] for x in items]]}

    def get(self, include=None):
        return {"ids": list(self.d), "metadatas": [v[1] for v in self.d.values()]}

    def delete(self, ids):
        for i in ids:
            self.d.pop(i, None)


class _Client:
    def __init__(self):
        self.c = {}

    def get_or_create_collection(self, name, embedding_function=None, metadata=None):
        return self.c.setdefault(name, _Col())

    def delete_collection(self, name):
        if name not in self.c:
            raise ValueError("no existe")
        del self.c[name]


@pytest.fixture(autouse=True)
def entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    from app.core.seed_personality import seed
    seed()          # init_db + identidad/rasgos/reglas base
    monkeypatch.setattr(memory, "_client", _Client())
    monkeypatch.setattr(memory, "_collections", {})
    yield


def _rbe(tools=("remember", "recall"), memory_spec=None, host=False):
    spec = {"role": "Asistente de RBE.", "tools": {"builtin": list(tools)}}
    if memory_spec is not None:
        spec["memory"] = memory_spec
    caps = {"tools": list(tools), "host_tools": host}
    # remember/recall leen la memoria del perfil de la base (como en producción: el perfil está sincronizado)
    profiles.sync_profile("rbe", spec, None)
    return Profile("rbe", False, spec, caps, ())


# ----------------------------------------------------------------------------- memoria
def test_nombre_de_coleccion_y_elipse_intacta():
    assert memory._collection_name("elipse") == memory.settings.chroma_collection
    assert memory._collection_name("rbe") == memory.settings.chroma_collection + "__rbe"
    # Chroma exige terminar en alfanumérico; un perfil puede terminar en _ o -
    n = memory._collection_name("rbe_")
    assert n[-1].isalnum() and n != memory._collection_name("rbe")
    assert memory._collection_name("rbe-") != memory._collection_name("rbe_")


def test_memoria_aislada_por_perfil():
    memory.add_memory("la contraseña del wifi de elipse", {"type": "manual"})
    memory.add_memory("el cliente de rbe paga los viernes", {"type": "manual"}, profile="rbe")
    assert memory.count_memories() == 1 and memory.count_memories("rbe") == 1
    assert [r["content"] for r in memory.search_memory("cliente rbe", profile="rbe")] == ["el cliente de rbe paga los viernes"]
    assert all("rbe" not in r["content"] for r in memory.search_memory("cliente rbe paga viernes"))
    assert all("wifi" not in r["content"] for r in memory.search_memory("wifi elipse", profile="rbe"))


def test_poda_de_un_perfil_no_toca_a_elipse():
    for i in range(5):
        memory.add_memory(f"elipse {i}", {"type": "research"})
        memory.add_memory(f"rbe {i}", {"type": "manual"}, profile="rbe")
    out = memory.enforce_retention_policy(max_items=2, prunable_types=["manual"], profile="rbe")
    assert out["total_after"] == 2
    assert memory.count_memories() == 5          # elipse intacta


def test_reset_de_un_perfil_no_borra_elipse():
    memory.add_memory("de elipse")
    memory.add_memory("de rbe", profile="rbe")
    memory.reset_memory("rbe")
    assert memory.count_memories("rbe") == 0 and memory.count_memories() == 1
    memory.reset_memory("rbe")                   # idempotente (la colección ya no existe)


def test_memory_settings_y_capabilities():
    assert profiles.memory_settings("noexiste") == {"max_items": 500, "max_distance": None}
    cfg = Capabilities(_rbe(memory_spec={"max_items": 99999, "max_distance": 7})).memory()
    assert cfg == {"max_items": 5000, "max_distance": 2.0}
    assert Capabilities(_rbe(memory_spec={"max_distance": True})).memory()["max_distance"] is None
    assert Capabilities(DEFAULT).memory() == {}


# ----------------------------------------------------------------------------- tools profile-aware
def test_remember_recall_usan_la_memoria_del_perfil_y_el_modelo_no_elige():
    from app.core.tools import execute_tool
    rbe = _rbe()
    # el modelo intenta colarse en la memoria de elipse con arguments.profile: se descarta (R13)
    execute_tool("remember", {"content": "dato secreto de rbe", "profile": "elipse"}, rbe)
    assert memory.count_memories() == 0 and memory.count_memories("rbe") == 1
    execute_tool("remember", {"content": "dato de elipse"})
    assert "dato secreto" not in execute_tool("recall", {"query": "dato de elipse"})
    assert "dato de elipse" not in execute_tool("recall", {"query": "dato de elipse", "profile": "elipse"}, rbe)
    assert "dato secreto de rbe" in execute_tool("recall", {"query": "dato secreto rbe"}, rbe)


def test_tope_de_memoria_del_perfil_se_respeta():
    from app.core.tools import execute_tool
    rbe = _rbe(memory_spec={"max_items": 3})
    for i in range(6):
        execute_tool("remember", {"content": f"recuerdo numero {i}"}, rbe)
    assert memory.count_memories("rbe") == 3


def test_umbral_de_distancia_en_recall():
    from app.core.tools import execute_tool
    rbe = _rbe(memory_spec={"max_distance": 0.2})
    execute_tool("remember", {"content": "gatos negros duermen"}, rbe)
    assert "No hay nada relevante" in execute_tool("recall", {"query": "motor diesel"}, rbe)


def test_tool_no_permitida_se_rechaza():
    from app.core.tools import execute_tool
    out = execute_tool("remember", {"content": "x"}, _rbe(tools=("calculate",)))
    assert memory.count_memories("rbe") == 0
    assert out.lower().startswith("error") or "no" in out.lower()


def test_remember_recall_ya_estan_abiertas_pero_el_resto_no():
    assert {"remember", "recall"} <= profiles.PROFILE_SAFE_BUILTINS
    assert profiles.PROFILE_AWARE_TOOLS == {"remember", "recall"}
    for cerrada in ("write_file", "run_python", "search_web", "research_topic", "write_code"):
        assert cerrada not in profiles.PROFILE_SAFE_BUILTINS
    eff, rej = profiles.effective_spec({"tools": {"builtin": ["remember", "run_python"]}}, {"tools": ["remember", "run_python"]})
    assert eff["tools"]["builtin"] == ["remember"] and any("run_python" in r for r in rej)
    assert profiles.default_caps()["tools"] == ["calculate", "get_current_datetime"]   # techo sugerido sin cambios


# ----------------------------------------------------------------------------- historial por perfil
def test_historial_aislado_por_perfil_y_sesion():
    from app.core.core import get_recent_messages, save_message
    save_message("user", "hola elipse")
    save_message("user", "hola rbe a", profile="rbe", session="s1")
    save_message("user", "hola rbe b", profile="rbe", session="s2")
    assert [m["content"] for m in get_recent_messages()] == ["hola elipse"]
    assert [m["content"] for m in get_recent_messages(10, "rbe", "s1")] == ["hola rbe a"]
    assert [m["content"] for m in get_recent_messages(10, "rbe", "s2")] == ["hola rbe b"]
    assert get_recent_messages(10, "rbe", "otra") == []


def test_poda_de_historial_por_perfil_no_desplaza_a_elipse():
    from app.core.core import get_recent_messages, save_message
    save_message("user", "mensaje de elipse")
    for i in range(30):
        save_message("user", f"rbe {i}", profile="rbe", session="s")
    db.prune_old_rows(keep_messages=5)
    assert [m["content"] for m in get_recent_messages()] == ["mensaje de elipse"]
    conn = db.get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE profile = 'rbe'").fetchone()[0]
    finally:
        conn.close()
    assert n == 5


def test_log_de_turno_guarda_perfil_actor_y_tools():
    from app.core.core import log_turn_metadata
    log_turn_metadata("hola", "ollama", "x", 0.5, profile="rbe", actor="u7", tools=["recall"], status="ok")
    conn = db.get_connection()
    try:
        r = conn.execute("SELECT profile, actor, tools, status FROM router_log ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert (r["profile"], r["actor"], r["status"]) == ("rbe", "u7", "ok") and json.loads(r["tools"]) == ["recall"]


# ----------------------------------------------------------------------------- prompt y enforcement
def test_prompt_de_elipse_no_cambia_y_el_de_rbe_es_mas_corto():
    from app.core.personality import build_system_prompt
    assert build_system_prompt() == build_system_prompt(profile=DEFAULT)
    assert build_system_prompt(compact=True) == build_system_prompt(compact=True, profile=DEFAULT)
    rbe = _rbe()
    full = build_system_prompt(profile=rbe)
    assert len(full) < len(build_system_prompt())
    assert "Asistente de RBE." in full and "recall" in full
    assert "recall" not in build_system_prompt(profile=_rbe(tools=("calculate",)))


def test_required_tools_no_empuja_tools_que_el_perfil_no_tiene():
    from app.core.core import _missing_required_tools
    caps = Capabilities(_rbe(tools=("calculate",)))
    for msg in ("busca en internet las noticias de hoy", "investiga a fondo el tema de la fusión nuclear"):
        assert all(caps.tool_allowed(r["name"]) for r in _missing_required_tools(msg, [], False, caps))


# ----------------------------------------------------------------------------- acciones pendientes
def test_exec_profile():
    rbe = _rbe()
    assert profiles.exec_profile("elipse", None) is DEFAULT
    assert profiles.exec_profile("elipse", rbe) is DEFAULT           # (resolve_pending_action ya filtró a las ajenas)
    assert profiles.exec_profile("rbe", rbe) is rbe
    assert profiles.exec_profile("otro", rbe) is None
    admin = profiles.exec_profile("rbe", DEFAULT)                      # admin confirma: techo restrictivo por defecto
    assert admin.name == "rbe" and admin.caps == profiles.default_caps()
    assert profiles.exec_profile("fantasma", DEFAULT) is None          # sin fila → no se ejecuta nada


def test_llave_de_anfitrion_no_resuelve_acciones_ajenas():
    from app.core.safety import create_pending_action, list_pending_actions, resolve_pending_action
    a_elipse = create_pending_action("write_file", {"path": "x"}, "d")
    a_rbe = create_pending_action("write_file", {"path": "y"}, "d", profile="rbe")
    rbe = _rbe()
    assert [a["id"] for a in list_pending_actions("rbe")] == [a_rbe]
    res = resolve_pending_action(a_elipse, False, rbe)
    assert res["status"] == "error" and "No existe" in res["detail"]
    assert resolve_pending_action(a_rbe, False, rbe)["status"] == "rechazada"


# ----------------------------------------------------------------------------- HTTP: dueño de tarea / adjuntos / listo
@pytest.fixture
def api():
    from fastapi.testclient import TestClient
    from app import main
    from app.core import auth
    admin = auth.create_key("admin-test")
    host = auth.create_key("rbe-test", profile="rbe", caps={"tools": ["remember"], "host_tools": False})
    return TestClient(main.app), {"Authorization": f"Bearer {admin}"}, {"Authorization": f"Bearer {host}"}


def test_tarea_ajena_es_404_para_la_llave_de_anfitrion(api):
    from app.core.tasks import create_task
    client, admin, host = api
    tid = create_task()                                     # tarea de 'elipse'
    assert client.get(f"/v1/task/{tid}", headers=admin).status_code == 200
    assert client.get(f"/v1/task/{tid}", headers=host).status_code == 404
    assert client.post(f"/v1/task/{tid}/cancel", headers=host).status_code == 404
    mine = create_task(_rbe(), "s1", "u1")
    assert client.get(f"/v1/task/{mine}", headers=host).status_code == 200


def test_chat_de_anfitrion_exige_perfil_listo_y_rechaza_adjuntos(api):
    client, admin, host = api
    r = client.post("/v1/chat", json={"message": "hola"}, headers=host)          # perfil aún sin sincronizar
    assert r.status_code == 409
    _rbe()                                                                        # sincroniza el perfil con rol
    r = client.post("/v1/chat", json={"message": "hola", "attachments": [{"filename": "a.txt", "content": "hola"}]}, headers=host)
    assert r.status_code == 403
    r = client.post("/v1/chat", json={"message": "hola", "session": "a\nb"}, headers=host)
    assert r.status_code == 422
