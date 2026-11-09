"""
Tests de elipse_sdk (núcleo). Se corre desde la raíz del proyecto:  pytest tests -v

- Firma/verificación contra TODOS los vectores de docs/protocol/vectors.json.
- Esquema desde type hints, validación de argumentos y despacho del callback.
- Ida y vuelta REAL: el motor (app.core.callback.call_host_tool) llama a un servidor HTTP local que usa el SDK.
- Cliente (sync, chat, confirmaciones) con un transporte httpx simulado: no necesita motor levantado.
No usan elipse.db.
"""

import enum
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal
from unittest import mock

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from elipse_sdk import (Context, Elipse, ElipseError, ElipseTimeout, SchemaError,  # noqa: E402
                        ToolError, ToolRegistry, sign, verify_request)
from elipse_sdk.schema import build_schema  # noqa: E402

VECTORS = json.loads((ROOT / "docs" / "protocol" / "vectors.json").read_text(encoding="utf-8"))
SECRET = "elipse-sdk-test-secret-0123456789"


# ------------------------------------------------------------------------------ firma / vectores

def _cases():
    for p in VECTORS["positive"]:
        yield p["name"], VECTORS["secret"], p, p["ts"], True
    for n in VECTORS["negative"]:
        yield "neg_" + n["name"], n["secret"], n, n["now"], False
    for e in VECTORS["edge_valid"]:
        yield "edge_" + e["name"], e["secret"], e, e["now"], True


@pytest.mark.parametrize("name,secret,c,now,expected", list(_cases()), ids=[c[0] for c in _cases()])
def test_vectores_del_protocolo(name, secret, c, now, expected):
    got = verify_request(secret, c["profile"], c["tool"], c["body"].encode("utf-8"), str(c["ts"]),
                         c["signature"], now=now)
    assert got is expected


def test_sign_produce_la_firma_de_los_vectores_positivos():
    for p in VECTORS["positive"]:
        assert sign(VECTORS["secret"], p["ts"], p["profile"], p["tool"], p["body"].encode("utf-8")) == p["signature"]


def test_firma_no_ascii_no_lanza():
    assert verify_request(SECRET, "rbe", "t", b"{}", str(int(time.time())), "ñandú") is False


def test_ts_estricto():
    now = 1793400000
    sig = sign(SECRET, now, "rbe", "t", b"{}")
    assert verify_request(SECRET, "rbe", "t", b"{}", str(now), sig, now=now)
    for bad in ("+1793400000", " 1793400000", "01793400000", "1793400000.0", None, 1793400000):
        assert verify_request(SECRET, "rbe", "t", b"{}", bad, sig, now=now) is False


# ------------------------------------------------------------------------------ esquema

class Color(enum.Enum):
    rojo = "rojo"
    azul = "azul"


def test_esquema_desde_type_hints_y_docstring():
    def f(ctx: Context, fecha: str, n: int = 5, activo: bool = True, tags: list[str] | None = None,
          modo: Literal["a", "b"] = "a", color: Color = Color.rojo, extra: dict | None = None):
        """Hace algo útil.

        Args:
            fecha: día en AAAA-MM-DD
            n (int): cuántos
        """

    params, info = build_schema(f, Context)
    assert "ctx" not in params["properties"]
    assert params["required"] == ["fecha"]
    p = params["properties"]
    assert p["fecha"] == {"type": "string", "description": "día en AAAA-MM-DD"}
    assert p["n"]["type"] == "integer" and p["n"]["default"] == 5 and p["n"]["description"] == "cuántos"
    assert p["activo"]["type"] == "boolean"
    assert p["tags"] == {"type": "array", "items": {"type": "string"}}
    assert p["modo"]["enum"] == ["a", "b"]
    assert p["color"]["enum"] == ["rojo", "azul"]
    assert info["tags"]["nullable"] is True and info["fecha"]["nullable"] is False


def test_esquema_rechaza_lo_no_soportado_al_decorar():
    reg = ToolRegistry()
    with pytest.raises(SchemaError):
        @reg.tool(risk="read")
        def sin_hint(x):  # sin type hint
            """d"""
    with pytest.raises(SchemaError):
        @reg.tool(risk="read")
        def tipo_raro(x: set):
            """d"""
    with pytest.raises(SchemaError):
        @reg.tool(risk="read")
        def con_varargs(*args: int):
            """d"""
    with pytest.raises(ValueError):
        @reg.tool(risk="read")
        def Mala(x: int):  # nombre inválido
            """d"""
    with pytest.raises(ValueError):
        @reg.tool(risk="read")
        def sin_doc(x: int):
            pass
    with pytest.raises(ValueError):
        @reg.tool(risk="raro")
        def riesgo_raro(x: int):
            """d"""


def test_registro_duplicado_y_riesgo_por_defecto():
    reg = ToolRegistry()

    @reg.tool
    def hola(x: int):
        """Dice hola."""

    assert reg.specs()[0]["risk"] == "write"          # falla cerrado: pide confirmación
    with pytest.raises(ValueError):
        reg.register(hola)


# ------------------------------------------------------------------------------ despacho

def _call(reg, tool, args=None, *, actor="admin:42", session="s1", call_id=None, secret=SECRET,
          profile="rbe", body_tool=None, ts=None, tamper=None):
    body = json.dumps({"call_id": call_id or f"c{time.monotonic_ns()}", "tool": body_tool or tool,
                       "args": args if args is not None else {}, "actor": actor, "session": session},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts = str(ts if ts is not None else int(time.time()))
    sig = sign(secret, ts, profile, tool, body)
    if tamper:
        body = tamper(body)
    return reg.handle(SECRET, "rbe", tool, body, ts, sig)


@pytest.fixture()
def reg():
    r = ToolRegistry()
    r.calls = []

    @r.tool(risk="read")
    def suma(ctx: Context, a: int, b: int = 0) -> dict:
        """Suma dos enteros."""
        r.calls.append(ctx)
        return {"total": a + b, "actor": ctx.actor, "session": ctx.session}

    @r.tool(risk="write")
    def crear(nombre: str) -> str:
        """Crea algo."""
        return f"creado {nombre}"

    @r.tool(risk="read")
    def falla() -> str:
        """Siempre falla (esperado)."""
        raise ToolError("no hay datos")

    @r.tool(risk="read")
    def explota() -> str:
        """Siempre explota (bug)."""
        raise RuntimeError("password=hunter2 en la conexión")

    @r.tool(risk="read")
    def enorme() -> str:
        """Devuelve demasiado."""
        return "x" * 100_000

    @r.tool(risk="read")
    def color(c: Color) -> str:
        """Usa un enum."""
        assert isinstance(c, Color)
        return c.value

    return r


def test_ok_y_ctx_firmado(reg):
    st, out = _call(reg, "suma", {"a": 2, "b": 3})
    assert st == 200 and out == {"ok": True, "result": {"total": 5, "actor": "admin:42", "session": "s1"}}
    assert reg.calls[0].tool == "suma" and reg.calls[0].profile == "rbe"


def test_ctx_no_se_puede_forjar_desde_args(reg):
    st, out = _call(reg, "suma", {"a": 1, "actor": "root", "session": "x", "profile": "elipse"})
    assert out["ok"] is True and out["result"]["actor"] == "admin:42" and out["result"]["session"] == "s1"


def test_firma_invalida_no_ejecuta(reg):
    st, out = _call(reg, "suma", {"a": 1}, tamper=lambda b: b.replace(b'"a":1', b'"a":9'))
    assert (st, out) == (401, {"ok": False, "error": "firma inválida"}) and reg.calls == []
    st, _ = _call(reg, "suma", {"a": 1}, ts=int(time.time()) - 61)
    assert st == 401 and reg.calls == []


def test_firma_se_verifica_antes_que_la_existencia_de_la_tool(reg):
    st, out = reg.handle(SECRET, "rbe", "no_existe", b"{}", str(int(time.time())), "0" * 64)
    assert st == 401                                       # no filtra qué tools existen


def test_tool_desconocida_404(reg):
    st, out = _call(reg, "no_existe")
    assert (st, out["error"]) == (404, "tool desconocida")


def test_tool_del_cuerpo_distinta_de_la_url(reg):
    st, out = _call(reg, "suma", {"a": 1}, body_tool="crear")
    assert st == 400 and reg.calls == []


def test_argumentos_del_modelo_se_validan_y_coercionan(reg):
    assert _call(reg, "suma", {"a": "7", "b": 3.0})[1]["result"]["total"] == 10     # "7" → 7, 3.0 → 3
    for bad in ({}, {"a": "x"}, {"a": True}, {"a": 1, "zzz": 2}, {"a": [1]}, {"a": 1.5}):
        st, out = _call(reg, "suma", bad)
        assert out["ok"] is False and out["error"].startswith("argumentos inválidos"), bad
    assert _call(reg, "color", {"c": "azul"})[1]["result"] == "azul"
    assert _call(reg, "color", {"c": "verde"})[1]["ok"] is False


def test_toolerror_y_bug_no_filtran_detalles(reg):
    assert _call(reg, "falla")[1] == {"ok": False, "error": "no hay datos"}
    out = _call(reg, "explota")[1]
    assert out == {"ok": False, "error": "error interno de la herramienta"} and "hunter2" not in json.dumps(out)


def test_resultado_enorme_se_rechaza_con_mensaje_util(reg):
    out = _call(reg, "enorme")[1]
    assert out["ok"] is False and "demasiado grande" in out["error"]


def test_replay_de_call_id_solo_en_write_y_danger(reg):
    assert _call(reg, "crear", {"nombre": "a"}, call_id="id1")[1]["ok"] is True
    st, out = _call(reg, "crear", {"nombre": "a"}, call_id="id1")
    assert (st, out["error"]) == (409, "llamada repetida")
    assert _call(reg, "suma", {"a": 1}, call_id="id2")[1]["ok"] is True
    assert _call(reg, "suma", {"a": 1}, call_id="id2")[1]["ok"] is True              # read: se permite


def test_cuerpo_invalido_y_args_no_objeto(reg):
    ts = str(int(time.time()))
    for body in (b"no es json", b"[]", b'{"tool":"suma","args":[1]}'):
        st, out = reg.handle(SECRET, "rbe", "suma", body, ts, sign(SECRET, ts, "rbe", "suma", body))
        assert out["ok"] is False and st in (200, 400)


def test_json_extendido_en_resultados():
    import datetime, decimal
    r = ToolRegistry()

    @r.tool(risk="read")
    def raro() -> dict:
        """Tipos comunes de un ORM."""
        return {"f": datetime.date(2026, 10, 30), "d": decimal.Decimal("12.50"), "t": (1, 2)}

    assert _call(r, "raro")[1]["result"] == {"f": "2026-10-30", "d": 12.5, "t": [1, 2]}


def test_ahandle_corre_sync_y_async():
    import asyncio
    r = ToolRegistry()

    @r.tool(risk="read")
    async def suma_async(x: int) -> int:
        """Async."""
        return x + 1

    @r.tool(risk="read")
    def suma_sync(x: int) -> int:
        """Sync."""
        return x + 2

    async def go():
        out = []
        for tool, x in (("suma_async", 1), ("suma_sync", 1)):
            body = json.dumps({"call_id": "c", "tool": tool, "args": {"x": x}, "actor": "", "session": ""},
                              separators=(",", ":")).encode()
            ts = str(int(time.time()))
            out.append(await r.ahandle(SECRET, "rbe", tool, body, ts, sign(SECRET, ts, "rbe", tool, body)))
        return out

    assert asyncio.run(go()) == [(200, {"ok": True, "result": 2}), (200, {"ok": True, "result": 3})]
    # handle() síncrono no puede esperar una corrutina: lo dice claro
    body = b'{"call_id":"c","tool":"suma_async","args":{"x":1},"actor":"","session":""}'
    ts = str(int(time.time()))
    assert "async" in r.handle(SECRET, "rbe", "suma_async", body, ts, sign(SECRET, ts, "rbe", "suma_async", body))[1]["error"]


# ------------------------------------------------------------------------------ ida y vuelta con el MOTOR real

def _serve(reg):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            tool = self.path.rsplit("/", 1)[-1]
            st, out = reg.handle(SECRET, "rbe", tool, body, self.headers.get("X-Elipse-Ts"),
                                 self.headers.get("X-Elipse-Sig"))
            data = json.dumps(out, ensure_ascii=False).encode("utf-8")
            self.send_response(st)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def engine_side(reg):
    from app.core import callback, profiles
    srv = _serve(reg)
    prof = profiles.Profile("rbe", False, {}, {}, ())
    url = f"http://127.0.0.1:{srv.server_address[1]}/elipse/tools"
    with mock.patch.object(profiles, "get_callback", lambda name: (url, SECRET)):
        yield callback, prof
    srv.shutdown()


def test_motor_real_llama_al_sdk(engine_side):
    callback, prof = engine_side
    out = callback.call_host_tool(prof, "suma", {"a": 4, "b": 1}, actor="admin:42", session="s9")
    assert json.loads(out) == {"total": 5, "actor": "admin:42", "session": "s9"}


def test_motor_real_y_unicode(engine_side):
    callback, prof = engine_side
    # unicode_y_escapes: acentos, ñ, emoji, saltos de línea y comillas deben sobrevivir al viaje completo
    out = callback.call_host_tool(prof, "crear", {"nombre": 'José Ñandú 🚌\n"x"'}, actor="usuario:ñ7", session="sesión-é")
    assert out == 'creado José Ñandú 🚌\n"x"'


def test_motor_real_errores_llegan_como_texto(engine_side):
    callback, prof = engine_side
    assert callback.call_host_tool(prof, "falla", {}) == "Error: no hay datos."
    assert callback.call_host_tool(prof, "no_existe", {}) == "Error: tool desconocida."
    assert callback.call_host_tool(prof, "suma", {"a": "x"}).startswith("Error: argumentos inválidos")
    assert "demasiado grande" in callback.call_host_tool(prof, "enorme", {})


def test_motor_real_con_secreto_distinto_es_rechazado(engine_side):
    callback, prof = engine_side
    from app.core import profiles
    url = profiles.get_callback("rbe")[0]
    with mock.patch.object(profiles, "get_callback", lambda name: (url, "otro-secreto-de-16-chars")):
        assert callback.call_host_tool(prof, "suma", {"a": 1}) == "Error: firma inválida."


# ------------------------------------------------------------------------------ cliente

def _client(handler, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return Elipse("http://motor:8000/", "elp_secreta", "rbe", SECRET, http=http, poll_interval=0.01, **kw)


def test_sync_publica_spec_con_tools_del_registro_y_avisa_lo_recortado():
    seen = {}

    def h(req):
        seen["req"] = req
        return httpx.Response(200, json={"profile": "rbe", "rejected": ["tools.host:x (no permitido)"]})

    e = _client(h)
    e.configure(role="Asistente de RBE", rules=["Responde corto"], tools={"builtin": ["calculate"]})

    @e.tool(risk="read")
    def viajes_del_dia(fecha: str) -> list:
        """Lista los viajes de una fecha."""

    with pytest.warns(UserWarning, match="recortó"):
        res = e.sync()
    r = seen["req"]
    assert r.method == "PUT" and r.url.path == "/v1/profiles/rbe"
    assert r.headers["authorization"] == "Bearer elp_secreta"
    spec = json.loads(r.content)["spec"]
    assert spec["role"] == "Asistente de RBE" and spec["tools"]["builtin"] == ["calculate"]
    assert spec["tools"]["host"] == [{"name": "viajes_del_dia", "description": "Lista los viajes de una fecha.",
                                      "parameters": {"type": "object", "properties": {"fecha": {"type": "string"}},
                                                     "required": ["fecha"]}, "risk": "read"}]
    assert res["profile"] == "rbe"
    with pytest.raises(ElipseError):
        e.sync(strict=True)


def test_configure_rechaza_tools_host_manual():
    e = _client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        e.configure(tools={"host": []})


def test_chat_hace_polling_hasta_el_final():
    estados = iter([{"status": "en_progreso", "result": None}, {"status": "en_progreso", "result": None},
                    {"status": "completado", "result": {"reply": "Hay 3 viajes."}}])

    def h(req):
        if req.method == "POST":
            body = json.loads(req.content)
            assert body == {"message": "viajes de hoy", "actor": "admin:42", "session": "s1"}
            return httpx.Response(200, json={"task_id": "t1", "status": "en_progreso"})
        return httpx.Response(200, json=next(estados))

    r = _client(h).chat("viajes de hoy", actor="admin:42", session="s1")
    assert r.ok and r.reply == "Hay 3 viajes." and r.task_id == "t1"


def test_chat_esperando_confirmacion_expone_las_pendientes():
    def h(req):
        if req.method == "POST":
            return httpx.Response(200, json={"task_id": "t1"})
        return httpx.Response(200, json={"status": "esperando_confirmacion", "result": {
            "reply": "necesita confirmación", "pending_confirmations": [{"action_id": "a1"}]}})

    r = _client(h).chat("borra todo")
    assert r.needs_confirmation and not r.ok and r.pending == [{"action_id": "a1"}]


def test_chat_timeout_y_errores_http():
    def lento(req):
        return httpx.Response(200, json={"task_id": "t"} if req.method == "POST" else {"status": "en_progreso"})

    with pytest.raises(ElipseTimeout):
        _client(lento).chat("hola", timeout=0.05)

    for code, detail in ((409, "El perfil 'rbe' aún no está listo"), (401, "API key inválida"), (422, "'actor' inválido")):
        with pytest.raises(ElipseError) as ei:
            _client(lambda r, c=code, d=detail: httpx.Response(c, json={"detail": d})).chat("hola")
        assert ei.value.status == code and detail in ei.value.detail


def test_error_de_red_no_filtra_la_llave():
    def caido(req):
        raise httpx.ConnectError("boom elp_secreta")

    with pytest.raises(ElipseError) as ei:
        _client(caido).chat("hola")
    assert "elp_secreta" not in str(ei.value) and "elp_secreta" not in repr(_client(caido))


def test_confirm_y_pending():
    def h(req):
        if req.url.path == "/v1/pending-actions":
            return httpx.Response(200, json={"actions": [{"id": "a1"}]})
        assert req.url.path == "/v1/confirm-action/a1" and json.loads(req.content) == {"approved": True}
        return httpx.Response(200, json={"status": "ok"})

    e = _client(h)
    assert e.pending_actions() == [{"id": "a1"}] and e.confirm("a1", True) == {"status": "ok"}


def test_handle_callback_atajo():
    e = _client(lambda r: httpx.Response(200, json={}))

    @e.tool(risk="read")
    def eco(texto: str) -> str:
        """Repite."""
        return texto

    body = json.dumps({"call_id": "c", "tool": "eco", "args": {"texto": "hola"}, "actor": "", "session": ""},
                      separators=(",", ":")).encode()
    ts = str(int(time.time()))
    assert e.handle_callback("eco", body, ts, sign(SECRET, ts, "rbe", "eco", body)) == (200, {"ok": True, "result": "hola"})
    assert e.verify_request("eco", body, ts, "0" * 64) is False
