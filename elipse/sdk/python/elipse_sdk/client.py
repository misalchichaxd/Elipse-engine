"""
Cliente del motor ELIPSE (lado anfitrión): declarar perfil, publicarlo y conversar.

    elipse = Elipse(url="http://127.0.0.1:8000", key="elp_...", profile="rbe", secret="...")
    elipse.configure(role="Asistente de RBE...", rules=["Responde corto"])

    @elipse.tool(risk="read")
    def viajes_del_dia(ctx: Context, fecha: str) -> list: ...

    elipse.sync()
    r = elipse.chat("viajes de hoy", actor="admin:42", session="s1")
    print(r.reply)

Requiere `httpx` (pip install elipse-sdk[client]) y, solo para stream(), `websockets`.
"""

import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlencode, urlparse, urlunparse

from .registry import Context, ToolRegistry
from .signing import verify_request


class ElipseError(Exception):
    """El motor respondió con un error HTTP (o no respondió)."""
    def __init__(self, status: int, detail: str):
        super().__init__(f"[{status}] {detail}")
        self.status = status
        self.detail = detail


class ElipseTimeout(ElipseError):
    def __init__(self, detail: str = "el motor no terminó a tiempo"):
        super().__init__(0, detail)


@dataclass
class ChatResult:
    task_id: str
    status: str                     # completado | esperando_confirmacion | error | cancelado
    reply: str
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completado"

    @property
    def needs_confirmation(self) -> bool:
        return self.status == "esperando_confirmacion"

    @property
    def pending(self) -> list:
        return list(self.raw.get("pending_confirmations") or [])


def _httpx():
    try:
        import httpx
        return httpx
    except ImportError as e:  # pragma: no cover
        raise ImportError("Falta httpx: pip install elipse-sdk[client]") from e


class Elipse:
    def __init__(self, url: str, key: str, profile: str, secret: str | None = None, *,
                 timeout: float = 30.0, poll_interval: float = 0.4, chat_timeout: float = 300.0,
                 http=None):
        self.url = url.rstrip("/")
        self.profile = profile
        self.secret = secret
        self.poll_interval = poll_interval
        self.chat_timeout = chat_timeout
        self._key = key
        self._timeout = timeout
        self._http = http
        self._spec: dict = {}
        self.tools = ToolRegistry()
        self.tool = self.tools.tool                 # @elipse.tool(risk="read")

    def __repr__(self):                              # nunca imprime la llave ni el secreto
        return f"Elipse(url={self.url!r}, profile={self.profile!r})"

    # ------------------------------------------------------------------ perfil
    def configure(self, **spec: Any) -> "Elipse":
        """
        Declara el perfil: role, brief, traits, rules, voice, memory, limits, confirm, data_policy,
        retention, vision, attachments, skills, continuation, model, pipelines, tools={"builtin":[...]}.
        `tools.host` NO se pasa aquí: sale de los @tool registrados.
        """
        t = spec.get("tools")
        if isinstance(t, dict) and "host" in t:
            raise ValueError("no pases tools.host: se genera de las funciones con @elipse.tool")
        self._spec.update(spec)
        return self

    def build_spec(self) -> dict:
        spec = dict(self._spec)
        tools = dict(spec.get("tools") or {})
        tools["host"] = self.tools.specs()
        spec["tools"] = tools
        return spec

    def sync(self, *, strict: bool = False) -> dict:
        """
        Publica el perfil (PUT idempotente: repetirlo deja el mismo resultado). Devuelve la respuesta del
        motor: {"profile": ..., "rejected": [...]}. Lo que el techo de la llave recorta va en `rejected`:
        se avisa con un warning, o lanza ElipseError si strict=True.
        """
        res = self._request("PUT", f"/v1/profiles/{self.profile}", json={"spec": self.build_spec()})
        rejected = res.get("rejected") or []
        if rejected:
            msg = "el motor recortó parte del perfil: " + "; ".join(map(str, rejected))
            if strict:
                raise ElipseError(422, msg)
            warnings.warn(msg, stacklevel=2)
        return res

    # ------------------------------------------------------------------ callback (atajo)
    def handle_callback(self, tool: str, body: bytes, ts, sig):
        """→ (status, json). Los adaptadores llaman a esto con los bytes crudos y los headers."""
        if not self.secret:
            raise ValueError("falta el secreto del callback (Elipse(..., secret=...))")
        return self.tools.handle(self.secret, self.profile, tool, body, ts, sig)

    async def ahandle_callback(self, tool: str, body: bytes, ts, sig):
        if not self.secret:
            raise ValueError("falta el secreto del callback (Elipse(..., secret=...))")
        return await self.tools.ahandle(self.secret, self.profile, tool, body, ts, sig)

    def verify_request(self, tool: str, body: bytes, ts, sig) -> bool:
        return bool(self.secret) and verify_request(self.secret, self.profile, tool, body, ts, sig)

    # ------------------------------------------------------------------ chat
    def start_chat(self, message: str, *, actor: str = "", session: str = "", speak: bool | None = None,
                   attachments: list | None = None) -> dict:
        """Lanza el turno y devuelve {"task_id", "ws_url", "poll_url", ...} sin esperar."""
        body: dict = {"message": message, "actor": actor or "", "session": session or ""}
        if speak is not None:
            body["speak"] = speak
        if attachments:
            body["attachments"] = attachments
        return self._request("POST", "/v1/chat", json=body)

    def chat(self, message: str, *, actor: str = "", session: str = "", speak: bool | None = None,
             attachments: list | None = None, timeout: float | None = None) -> ChatResult:
        """Envía y espera la respuesta final (polling). Para ver el texto mientras sale, usa stream()."""
        task = self.start_chat(message, actor=actor, session=session, speak=speak, attachments=attachments)
        return self.wait(task["task_id"], timeout=timeout)

    def wait(self, task_id: str, *, timeout: float | None = None) -> ChatResult:
        deadline = time.monotonic() + (timeout if timeout is not None else self.chat_timeout)
        delay = self.poll_interval
        while True:
            st = self._request("GET", f"/v1/task/{task_id}")
            if st.get("status") != "en_progreso":
                result = st.get("result") or {}
                if st.get("status") == "error" and not result:
                    raise ElipseError(404, str(st.get("detail") or "tarea inexistente"))
                return ChatResult(task_id, str(st.get("status")), str(result.get("reply") or result.get("detail") or ""), result)
            if time.monotonic() > deadline:
                raise ElipseTimeout()
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

    def stream(self, message: str, *, actor: str = "", session: str = "", speak: bool | None = None,
               attachments: list | None = None, timeout: float | None = None) -> Iterator[dict]:
        """
        Genera los eventos del motor por WebSocket: {"type": "progreso"|"chunk"|"plan"|"final"|"error", ...}.
        El último es "final" (con "data") o "error". Los "ping" se descartan.
        """
        try:
            from websockets.sync.client import connect
        except ImportError as e:  # pragma: no cover
            raise ImportError("Falta websockets: pip install elipse-sdk[stream]") from e
        import json as _json

        task = self.start_chat(message, actor=actor, session=session, speak=speak, attachments=attachments)
        u = urlparse(self.url)
        ws = urlunparse(("wss" if u.scheme == "https" else "ws", u.netloc,
                         f"{u.path.rstrip('/')}/v1/ws/{task['task_id']}", "", urlencode({"token": self._key}), ""))
        limit = timeout if timeout is not None else self.chat_timeout
        deadline = time.monotonic() + limit
        with connect(ws, open_timeout=self._timeout, max_size=2 ** 22) as sock:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ElipseTimeout()
                try:
                    raw = sock.recv(timeout=min(left, 60))
                except TimeoutError:
                    continue
                ev = _json.loads(raw)
                if ev.get("type") == "ping":
                    continue
                yield ev
                if ev.get("type") in ("final", "error"):
                    return

    # ------------------------------------------------------------------ confirmaciones y tareas
    def pending_actions(self) -> list:
        return self._request("GET", "/v1/pending-actions").get("actions", [])

    def confirm(self, action_id: str, approved: bool) -> dict:
        """Aprueba o rechaza una acción pendiente (tools write/danger). Normalmente lo decide una persona."""
        return self._request("POST", f"/v1/confirm-action/{action_id}", json={"approved": bool(approved)})

    def cancel(self, task_id: str) -> dict:
        return self._request("POST", f"/v1/task/{task_id}/cancel")

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, json: Any = None) -> dict:
        httpx = _httpx()
        client = self._http or httpx.Client(timeout=self._timeout)
        try:
            r = client.request(method, self.url + path, json=json,
                               headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as e:
            raise ElipseError(0, f"no se pudo conectar con el motor ({type(e).__name__})") from None
        finally:
            if self._http is None:
                client.close()
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise ElipseError(r.status_code, str(detail or r.text[:200] or "error"))
        return data if isinstance(data, dict) else {"data": data}
