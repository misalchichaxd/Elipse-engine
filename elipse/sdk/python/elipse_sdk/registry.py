"""
Registro de tools y despacho del callback (lado ANFITRIÓN). Sin frameworks web.

    reg = ToolRegistry()

    @reg.tool(risk="read")
    def viajes_del_dia(ctx: Context, fecha: str) -> list:
        '''Lista los viajes de una fecha (AAAA-MM-DD).'''
        ...

    status, payload = reg.handle(secret, "rbe", tool, body_bytes, ts_header, sig_header)

`handle` hace TODO lo que pide PROTOCOL.md §5–§6: verifica la firma ANTES de mirar nada más, comprueba
que la tool del cuerpo coincida con la de la URL, rechaza call_id repetidos en tools write/danger,
valida los argumentos que eligió el modelo y convierte el resultado a JSON acotado.
Los adaptadores (Django, FastAPI…) solo traducen request/response.
"""

import asyncio
import dataclasses
import datetime
import decimal
import enum
import inspect
import json
import logging
import re
import threading
import time
import typing
from dataclasses import dataclass
from typing import Callable

from .schema import ArgError, SchemaError, build_schema, coerce, first_line, MAX_DESCRIPTION
from .signing import verify_request

log = logging.getLogger("elipse_sdk")

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
RISKS = ("read", "write", "danger")
MAX_BODY_BYTES = 32 * 1024
MAX_RESULT_BYTES = 40 * 1024        # el motor acepta ~44 KB de respuesta; más es error
REPLAY_TTL_S = 120

_IGNORED_ARGS = ("profile", "actor", "session")   # el motor ya las quita; por si acaso


class ToolError(Exception):
    """Lánzala desde una tool para devolver al modelo un fallo esperado (mensaje corto y seguro)."""


@dataclass(frozen=True)
class Context:
    """Identidad FIRMADA de la llamada: sale de la petición autenticada, nunca del modelo."""
    actor: str
    session: str
    call_id: str
    tool: str
    profile: str


@dataclass
class _Tool:
    name: str
    fn: Callable
    description: str
    risk: str
    parameters: dict
    info: dict
    ctx_param: str | None
    is_async: bool

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters, "risk": self.risk}


class _ReplayGuard:
    def __init__(self, ttl: int = REPLAY_TTL_S):
        self.ttl = ttl
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_time(self, call_id: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        with self._lock:
            for k in [k for k, t in self._seen.items() if now - t > self.ttl]:
                del self._seen[k]
            if call_id in self._seen:
                return False
            self._seen[call_id] = now
            return True


def _json_default(o):
    if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
        return o.isoformat()
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, enum.Enum):
        return o.value
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    raise TypeError(f"no serializable: {type(o).__name__}")


def _fail(status: int, msg: str) -> tuple[int, dict]:
    return status, {"ok": False, "error": msg}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, _Tool] = {}
        self._replay = _ReplayGuard()

    # ------------------------------------------------------------------ registro
    def tool(self, fn: Callable | None = None, *, risk: str = "write", name: str | None = None,
             description: str | None = None):
        """
        @reg.tool(risk="read")  — risk: "read" | "write" | "danger" (por defecto "write": pide confirmación).
        El nombre es el de la función (a-z, 0-9, _; 2-48). La descripción, la 1ª línea del docstring.
        """
        def deco(f: Callable):
            self.register(f, risk=risk, name=name, description=description)
            return f
        return deco(fn) if fn is not None else deco

    def register(self, fn: Callable, *, risk: str = "write", name: str | None = None,
                 description: str | None = None) -> None:
        if risk not in RISKS:
            raise ValueError(f"risk inválido: {risk!r} (usa {', '.join(RISKS)})")
        tname = name or fn.__name__
        if not TOOL_NAME_RE.match(tname):
            raise ValueError(f"nombre de tool inválido: {tname!r} (a-z, 0-9 y _; 2-48 caracteres; empieza con letra)")
        if tname in self._tools:
            raise ValueError(f"la tool '{tname}' ya está registrada")
        desc = (description or first_line(fn.__doc__)).strip()
        if not desc:
            raise ValueError(f"{tname}: falta la descripción (primera línea del docstring)")
        params, info = build_schema(fn, Context)
        hints = typing.get_type_hints(fn)
        ctx_param = next((n for n in inspect.signature(fn).parameters if n == "ctx" or hints.get(n) is Context), None)
        self._tools[tname] = _Tool(tname, fn, desc[:MAX_DESCRIPTION], risk, params, info, ctx_param,
                                   inspect.iscoroutinefunction(fn))

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict]:
        """Lista para spec.tools.host (esto es lo que publica sync())."""
        return [t.spec() for t in self._tools.values()]

    # ------------------------------------------------------------------ despacho
    def _prepare(self, secret: str, profile: str, tool: str, body: bytes, ts, sig):
        """→ (early, prepared). early = (status, payload) si hay que responder ya."""
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("body debe ser bytes (los bytes crudos de la petición)")
        body = bytes(body)
        if not verify_request(secret, profile, tool, body, ts, sig):
            return _fail(401, "firma inválida"), None
        if len(body) > MAX_BODY_BYTES:
            return _fail(413, "cuerpo demasiado grande"), None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _fail(400, "cuerpo inválido"), None
        if not isinstance(data, dict) or data.get("tool") != tool:
            return _fail(400, "el cuerpo no coincide con la tool de la URL"), None
        t = self._tools.get(tool)
        if t is None:
            return _fail(404, "tool desconocida"), None
        call_id = str(data.get("call_id") or "")
        if t.risk != "read":
            if not call_id or not self._replay.first_time(call_id):
                return _fail(409, "llamada repetida"), None
        raw = data.get("args")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            return _fail(200, "args debe ser un objeto"), None
        try:
            kwargs = self._validate(t, raw)
        except ArgError as e:
            return _fail(200, f"argumentos inválidos: {e}"), None
        ctx = Context(actor=str(data.get("actor") or ""), session=str(data.get("session") or ""),
                      call_id=call_id, tool=tool, profile=profile)
        if t.ctx_param:
            kwargs[t.ctx_param] = ctx
        return None, (t, kwargs)

    @staticmethod
    def _validate(t: _Tool, raw: dict) -> dict:
        raw = {k: v for k, v in raw.items() if k not in _IGNORED_ARGS}
        unknown = [k for k in raw if k not in t.info]
        if unknown:
            raise ArgError(f"argumento desconocido: {', '.join(sorted(map(str, unknown)))}")
        out = {}
        for name, meta in t.info.items():
            if name not in raw:
                if meta["required"]:
                    raise ArgError(f"falta '{name}'")
                continue
            val = coerce(raw[name], meta["schema"], meta["nullable"], name)
            if val is not None and meta["enum_cls"] is not None:
                val = meta["enum_cls"](val)
            out[name] = val
        return out

    @staticmethod
    def _finish(result) -> tuple[int, dict]:
        try:
            text = json.dumps({"ok": True, "result": result}, ensure_ascii=False,
                              separators=(",", ":"), default=_json_default)
        except (TypeError, ValueError):
            return _fail(200, "la tool devolvió un valor que no es JSON")
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            return _fail(200, "resultado demasiado grande: devuelve menos filas o un resumen")
        return 200, json.loads(text)

    @staticmethod
    def _tool_exception(t: _Tool, e: Exception) -> tuple[int, dict]:
        if isinstance(e, ToolError):
            return _fail(200, " ".join(str(e).split())[:300] or "la herramienta falló")
        log.exception("tool %s falló", t.name)          # el detalle queda en TU log, no va al modelo
        return _fail(200, "error interno de la herramienta")

    def handle(self, secret: str, profile: str, tool: str, body: bytes, ts, sig) -> tuple[int, dict]:
        """→ (status_http, json). Pensado para vistas síncronas (Django, Flask…)."""
        early, prep = self._prepare(secret, profile, tool, body, ts, sig)
        if early:
            return early
        t, kwargs = prep
        try:
            if t.is_async:
                return _fail(200, "tool async: usa ahandle() en un adaptador async")
            return self._finish(t.fn(**kwargs))
        except Exception as e:
            return self._tool_exception(t, e)

    async def ahandle(self, secret: str, profile: str, tool: str, body: bytes, ts, sig) -> tuple[int, dict]:
        """Igual que handle() para FastAPI/ASGI: las tools síncronas corren en un hilo."""
        early, prep = self._prepare(secret, profile, tool, body, ts, sig)
        if early:
            return early
        t, kwargs = prep
        try:
            if t.is_async:
                return self._finish(await t.fn(**kwargs))
            return self._finish(await asyncio.to_thread(t.fn, **kwargs))
        except Exception as e:
            return self._tool_exception(t, e)
