"""Fase 4 — callback HMAC al anfitrión. Usa un servidor HTTP local real (127.0.0.1, puerto libre)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core import callback, db, profiles
from app.core.profiles import Capabilities, Profile

SECRET = "s" * 40


class _Host:
    """Anfitrión de mentira: verifica la firma como lo haría Django y responde según `mode`."""

    def __init__(self):
        self.calls, self.mode, self.sleep = [], "ok", 0.0
        host = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                tool = self.path.rsplit("/", 1)[-1]
                valid = callback.verify_request(SECRET, "rbe", tool, body, self.headers.get("X-Elipse-Ts"),
                                                self.headers.get("X-Elipse-Sig"))
                host.calls.append({"path": self.path, "valid": valid, "body": json.loads(body or b"{}")})
                if host.sleep:
                    time.sleep(host.sleep)
                if not valid:
                    return self._send(401, {"ok": False, "error": "firma"})
                m = host.mode
                if m == "redirect":
                    self.send_response(302); self.send_header("Location", "/otro"); self.end_headers(); return
                if m == "huge":
                    return self._send(200, {"ok": True, "result": "x" * 200_000})
                if m == "raw":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"<html>no json</html>"); return
                if m == "fail":
                    return self._send(200, {"ok": False, "error": "no hay viajes\nese día"})
                self._send(200, {"ok": True, "result": [{"viaje": 1}, {"viaje": 2}]})

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/elipse/tools"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


@pytest.fixture(autouse=True)
def entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    from app.core.seed_personality import seed
    seed()


@pytest.fixture
def host():
    h = _Host()
    yield h
    h.stop()


def _rbe(risk="read", confirm=None, timeout=3, with_cb=True, url=None):
    spec = {"role": "Asistente de RBE.", "limits": {"tool_timeout_s": timeout},
            "tools": {"host": [{"name": "viajes_del_dia", "description": "Viajes de una fecha.", "risk": risk,
                                "parameters": {"type": "object", "properties": {"fecha": {"type": "string"}}}}]}}
    if confirm:
        spec["confirm"] = confirm
    caps = {"tools": [], "host_tools": True}
    profiles.sync_profile("rbe", spec, None)
    if with_cb:
        profiles.set_callback("rbe", url, SECRET)
    return profiles.profile_from_key("rbe", caps)


def test_firma_y_verificacion():
    body = b'{"a":1}'
    sig = callback.sign(SECRET, 1000, "rbe", "t", body)
    assert callback.verify_request(SECRET, "rbe", "t", body, 1000, sig, now=1030)
    assert not callback.verify_request(SECRET, "rbe", "t", body, 1000, sig, now=1061)       # fuera de ventana
    assert not callback.verify_request(SECRET, "rbe", "t", body + b" ", 1000, sig, now=1000)  # cuerpo alterado
    assert not callback.verify_request(SECRET, "rbe", "otra", body, 1000, sig, now=1000)      # otra tool
    assert not callback.verify_request(SECRET, "otro", "t", body, 1000, sig, now=1000)        # otro perfil
    assert not callback.verify_request("x" * 40, "rbe", "t", body, 1000, sig, now=1000)       # otro secreto
    assert not callback.verify_request(SECRET, "rbe", "t", body, "abc", sig)
    assert not callback.verify_request(SECRET, "rbe", "t", body, 1000, None, now=1000)


def test_llamada_ok_firmada_y_actor_session_no_vienen_de_los_argumentos(host):
    from app.core.tools import execute_tool
    rbe = _rbe(url=host.url)
    # el modelo intenta suplantar actor/session/profile en los argumentos
    out = execute_tool("viajes_del_dia", {"fecha": "2026-10-28", "actor": "root", "session": "x", "profile": "elipse"},
                       rbe, actor="admin:42", session="s1")
    assert json.loads(out) == [{"viaje": 1}, {"viaje": 2}]
    c = host.calls[0]
    assert c["valid"] and c["path"].endswith("/elipse/tools/viajes_del_dia")
    assert c["body"]["actor"] == "admin:42" and c["body"]["session"] == "s1"
    assert c["body"]["args"] == {"fecha": "2026-10-28"} and c["body"]["tool"] == "viajes_del_dia" and c["body"]["call_id"]


def test_sin_callback_configurado(host):
    from app.core.tools import execute_tool
    rbe = _rbe(with_cb=False)
    out = execute_tool("viajes_del_dia", {}, rbe)
    assert out.startswith("Error:") and "callback" in out and not host.calls


def test_tool_que_el_perfil_no_tiene_no_llega_al_anfitrion(host):
    from app.core.tools import execute_tool
    rbe = _rbe(url=host.url)
    assert execute_tool("borrar_todo", {}, rbe).startswith("Error")
    assert not host.calls


def test_error_del_anfitrion_es_texto(host):
    from app.core.tools import execute_tool
    rbe = _rbe(url=host.url)
    host.mode = "fail"
    out = execute_tool("viajes_del_dia", {}, rbe)
    assert out == "Error: no hay viajes ese día."


def test_no_sigue_redirects(host):
    from app.core.tools import execute_tool
    rbe = _rbe(url=host.url)
    host.mode = "redirect"
    out = execute_tool("viajes_del_dia", {}, rbe)
    assert out.startswith("Error:") and "redirección" in out
    assert len(host.calls) == 1                                  # nunca fue a /otro


def test_timeout(host):
    from app.core.tools import execute_tool
    rbe = _rbe(url=host.url, timeout=1)
    host.sleep = 2.5
    t0 = time.monotonic()
    out = execute_tool("viajes_del_dia", {}, rbe)
    assert out.startswith("Error:") and "a tiempo" in out and time.monotonic() - t0 < 2.4


def test_respuesta_enorme_se_corta_y_basura_es_error(host, monkeypatch):
    from app.core.tools import execute_tool
    rbe = _rbe(url=host.url)
    monkeypatch.setattr(callback.settings, "mcp_max_output_chars", 1000)
    host.mode = "huge"
    assert "demasiado grande" in execute_tool("viajes_del_dia", {}, rbe)      # pasa el tope de bytes: error
    monkeypatch.setattr(callback.settings, "mcp_max_output_chars", 100_000)
    monkeypatch.setattr(callback.settings, "mcp_max_output_chars", 50_000)
    out = execute_tool("viajes_del_dia", {}, rbe)                              # cabe en bytes, pasa el de caracteres
    assert out.endswith("\n\n[...salida truncada...]") and len(out) == 50_000 + len("\n\n[...salida truncada...]")
    host.mode = "raw"
    assert execute_tool("viajes_del_dia", {}, rbe).startswith("Error: respuesta inválida")


def test_firma_rechazada_por_el_anfitrion_se_explica_sin_filtrar_secreto(host):
    from app.core.tools import execute_tool
    rbe = _rbe(url=host.url)
    profiles.set_callback("rbe", host.url, "otro-secreto-distinto-largo-1234")
    out = execute_tool("viajes_del_dia", {}, rbe)
    assert out.startswith("Error:") and "firma" in out
    for secreto in (SECRET, "otro-secreto-distinto-largo-1234", host.url):
        assert secreto not in out


def test_host_caido_no_filtra_url():
    from app.core.tools import execute_tool
    rbe = _rbe(url="http://127.0.0.1:9/elipse/tools")                 # puerto sin servicio
    out = execute_tool("viajes_del_dia", {}, rbe)
    assert out.startswith("Error:") and "127.0.0.1" not in out


def test_confirm_for():
    assert Capabilities(_rbe(risk="read", url="http://x.test")).confirm_for("read") is False
    assert Capabilities(_rbe(url="http://x.test")).confirm_for("write") is True
    c = Capabilities(Profile("rbe", False, {"confirm": {"write": "never", "danger": "never"}}, {"host_tools": True}, ()))
    assert c.confirm_for("write") is False and c.confirm_for("danger") is True      # danger no se apaga
    assert Capabilities(Profile("rbe", False, {"confirm": {"write": "quiza"}}, {}, ())).confirm_for("write") is True


def test_tool_write_queda_pendiente_y_se_ejecuta_al_confirmar_con_actor_y_session(host):
    from app.core.safety import create_pending_action, get_pending_action, is_risky, resolve_pending_action
    rbe = _rbe(url=host.url, risk="write")
    risky, detail = is_risky("viajes_del_dia", {"fecha": "hoy"}, rbe)
    assert risky and "viajes_del_dia" in detail
    # el modelo intenta colar un falso contexto dentro de sus argumentos
    forged = {"v": 2, "args": {"fecha": "hoy"}, "ctx": {"actor": "root", "session": "x"}}
    aid = create_pending_action("viajes_del_dia", forged, detail, "rbe", ctx={"actor": "admin:42", "session": "s1"})
    assert not host.calls                                              # nada se ejecuta hasta confirmar
    res = resolve_pending_action(aid, True, rbe)
    assert res["status"] == "aprobada"
    c = host.calls[0]
    assert c["valid"] and c["body"]["actor"] == "admin:42" and c["body"]["session"] == "s1"
    assert c["body"]["args"] == forged                                 # lo forjado quedó como un ARGUMENTO más, no como contexto
    assert get_pending_action(aid)["status"] == "aprobada"


def test_rechazar_no_llama_al_anfitrion(host):
    from app.core.safety import create_pending_action, resolve_pending_action
    rbe = _rbe(url=host.url, risk="danger")
    aid = create_pending_action("viajes_del_dia", {}, "d", "rbe", ctx={"actor": "a", "session": "s"})
    assert resolve_pending_action(aid, False, rbe)["status"] == "rechazada" and not host.calls


def test_elipse_no_cambia_formato_de_acciones_pendientes():
    from app.core.safety import create_pending_action, get_pending_action
    aid = create_pending_action("write_file", {"path": "a.txt", "content": "x"}, "d")
    assert json.loads(get_pending_action(aid)["arguments"]) == {"path": "a.txt", "content": "x"}
