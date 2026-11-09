"""
Callback HMAC al anfitrión (Fase 4, plan §4.9, R12).

Cuando el modelo llama a una tool DEL ANFITRIÓN (una que corre dentro de su sistema, p. ej. Django de RBE),
el engine hace un POST firmado a  <callback_url>/<tool>  y devuelve el resultado como texto.

  Headers:  X-Elipse-Ts:  epoch en segundos
            X-Elipse-Sig: hex( HMAC_SHA256(secret, f"{ts}.{perfil}.{tool}.{sha256_hex(body)}") )
  Body:     {"call_id", "tool", "args", "actor", "session"}
  Respuesta: {"ok": true, "result": <json>}  |  {"ok": false, "error": "texto corto"}

Garantías (todas fallan cerrado y devuelven "Error: ..." en vez de lanzar excepción):
  - La URL y el secreto los fijó el admin (profiles.set_callback). El modelo y el anfitrión no los eligen.
  - `actor` y `session` vienen de la petición autenticada, NUNCA de los argumentos del modelo.
  - Sin redirects, sin proxies del entorno, timeout total = limits.tool_timeout_s, respuesta acotada.
  - El secreto y la URL nunca aparecen en un mensaje de error, en un log ni en lo que ve el modelo.

Este módulo NO importa tools.py (tools.py es quien lo importa).
"""

import hashlib
import hmac
import json
import time
import uuid

import httpx

from app.config import settings
from app.core import profiles

MAX_SKEW_S = 60                 # ventana de tiempo que acepta el anfitrión al verificar
MAX_ARGS_BYTES = 32 * 1024      # tope de argumentos que el engine acepta mandar


# --------------------------------------------------------------------------- firma / verificación

def sign(secret: str, ts: int | str, profile: str, tool: str, body: bytes) -> str:
    msg = f"{ts}.{profile}.{tool}.{hashlib.sha256(body).hexdigest()}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_request(secret: str, profile: str, tool: str, body: bytes, ts, sig,
                   now: float | None = None, max_skew: int = MAX_SKEW_S) -> bool:
    """
    Lado del ANFITRIÓN (el SDK de la Fase 5 trae una copia). True solo si la firma coincide Y el
    timestamp está dentro de la ventana. Comparación en tiempo constante.
    """
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts_i) > max_skew:
        return False
    return hmac.compare_digest(sign(secret, ts_i, profile, tool, body), str(sig or ""))


# --------------------------------------------------------------------------- llamada

class _CallError(Exception):
    """Mensaje corto y SEGURO (sin URL ni secreto) para devolver como 'Error: ...'."""


def _post(url: str, headers: dict, body: bytes, timeout: float, cap: int) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", url, content=body, headers=headers) as r:
                if 300 <= r.status_code < 400:
                    raise _CallError("el anfitrión respondió con una redirección (no se siguen)")
                data = bytearray()
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > cap:
                        raise _CallError("la respuesta del anfitrión es demasiado grande")
                    if time.monotonic() > deadline:
                        raise _CallError(f"el anfitrión no respondió a tiempo ({timeout:g} s)")
                return r.status_code, bytes(data)
    except httpx.TimeoutException:
        raise _CallError(f"el anfitrión no respondió a tiempo ({timeout:g} s)")
    except httpx.HTTPError:
        raise _CallError("no se pudo conectar con el anfitrión")


def call_host_tool(profile, tool: str, arguments: dict, actor: str = "", session: str = "") -> str:
    """Ejecuta una tool del anfitrión por callback firmado. SIEMPRE devuelve texto; nunca lanza."""
    try:
        return _call(profile, tool, arguments, actor, session)
    except _CallError as e:
        return f"Error: {e}."
    except Exception:
        # Defensa en profundidad: ni siquiera un bug aquí debe filtrar URL/secreto ni tumbar el turno.
        return "Error: falló la llamada a la herramienta del anfitrión."


def _call(profile, tool: str, arguments: dict, actor: str, session: str) -> str:
    cb = profiles.get_callback(profile.name)
    if cb is None:
        raise _CallError("las herramientas del anfitrión aún no están disponibles (el perfil no tiene callback configurado)")
    url_base, secret = cb
    try:
        url_base = profiles.validate_callback_url(url_base)
    except ValueError:
        raise _CallError("el callback configurado no es válido")

    args = arguments if isinstance(arguments, dict) else {}
    # R13: nunca viajan en `args`; actor/session van aparte y vienen del llamador autenticado.
    args = {k: v for k, v in args.items() if k not in profiles.PROFILE_ARG_KEYS}
    try:
        body = json.dumps({"call_id": uuid.uuid4().hex, "tool": tool, "args": args,
                           "actor": actor or "", "session": session or ""},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise _CallError("los argumentos no son JSON válido")
    if len(body) > MAX_ARGS_BYTES:
        raise _CallError("los argumentos son demasiado grandes")

    ts = int(time.time())
    headers = {"Content-Type": "application/json; charset=utf-8", "X-Elipse-Ts": str(ts),
               "X-Elipse-Sig": sign(secret, ts, profile.name, tool, body)}
    max_chars = int(settings.mcp_max_output_chars)
    timeout = float(profiles.Capabilities(profile).limits().get("tool_timeout_s", 10))

    status, raw = _post(f"{url_base}/{tool}", headers, body, timeout, cap=max_chars * 4 + 4096)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict) or "ok" not in payload:
        if status in (401, 403):
            raise _CallError("el anfitrión rechazó la firma (revisa que el secreto coincida)")
        raise _CallError(f"respuesta inválida del anfitrión (HTTP {status})")
    if payload["ok"] is not True:
        err = " ".join(str(payload.get("error") or "la herramienta falló").split())[:300]
        raise _CallError(err.rstrip("."))

    result = payload.get("result")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...salida truncada...]"
    return text
