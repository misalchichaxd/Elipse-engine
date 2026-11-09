"""
Autenticación por API key (Fase 5 — Multi-dispositivo) + perfiles (Fase 1 del plan de perfiles).

Mismo patrón que plataformas como OpenRouter o Mistral: generas una llave con
un nombre (ej. "telefono", "reloj", "laptop"), la usas como
'Authorization: Bearer <llave>' en cada request, y puedes revocarla individualmente
sin afectar las demás.

La llave en texto plano SOLO se muestra una vez, al crearla. En la base de
datos solo se guarda su hash (sha256) — igual que una contraseña, nunca el
valor real — así que si alguien lee elipse.db no puede reconstruir las llaves.

Perfiles: cada llave está atada a un perfil (columna api_keys.profile) y puede llevar un techo
de permisos (api_keys.caps). Las llaves de siempre son 'elipse' con caps NULL = llaves admin.

FASE 1: los endpoints existentes (require_api_key, WebSocket, _verify_raw_key) aceptan SOLO
llaves admin. Una llave de anfitrión (p. ej. 'rbe') recibe 403 en todo hasta que la Fase 2 abra,
endpoint por endpoint, la lista blanca con require_principal (R3/R5: falla cerrado).
"""

import hashlib
import json
import secrets

from fastapi import Header, HTTPException, WebSocket, WebSocketException, status

from app.core import profiles
from app.core.db import get_connection
from app.core.profiles import Principal


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_key(name: str, profile: str = "elipse", caps: dict | None = None) -> str:
    """
    Genera una nueva API key para un dispositivo/cliente. Devuelve la llave en
    texto plano — es la ÚNICA vez que se puede ver, guárdala en ese momento.

    profile='elipse' (default) sin caps = llave admin, como siempre. Un perfil distinto exige
    un nombre válido y caps (aunque sea {} = todo apagado): nunca hay llaves de anfitrión sin techo.
    """
    if profile == profiles.DEFAULT_NAME:
        if caps is not None:
            raise ValueError("Una llave del perfil 'elipse' no lleva caps (es la llave admin).")
    else:
        if not profiles.valid_name(profile):
            raise ValueError(f"Nombre de perfil inválido: '{profile}' (a-z, 0-9, _ y -; 2-32 caracteres; no reservado).")
        if not isinstance(caps, dict):
            raise ValueError("Una llave de anfitrión necesita caps (un objeto JSON, aunque sea {}).")

    raw_key = f"elp_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)

    conn = get_connection()
    conn.execute(
        "INSERT INTO api_keys (name, key_hash, profile, caps) VALUES (?, ?, ?, ?)",
        (name.strip() or "sin_nombre", key_hash, profile, None if caps is None else json.dumps(caps)),
    )
    conn.commit()
    conn.close()

    return raw_key


def _parse_caps(text: str | None) -> dict | None:
    """NULL → None (sin techo). JSON roto o que no es objeto → {} (todo apagado): falla cerrado."""
    if text is None:
        return None
    try:
        caps = json.loads(text)
        return caps if isinstance(caps, dict) else {}
    except (TypeError, ValueError):
        return {}


def list_keys():
    """Lista las llaves (sin exponer el valor real, solo metadata)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, revoked, created_at, last_used_at, profile, caps FROM api_keys ORDER BY id DESC"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["caps"] = _parse_caps(d["caps"])
        out.append(d)
    return out


def revoke_key(key_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def _fetch_key(raw_key: str | None):
    """Fila de la llave si es válida y no está revocada (y marca su último uso); si no, None."""
    if not raw_key:
        return None

    key_hash = _hash_key(raw_key)
    conn = get_connection()
    row = conn.execute(
        "SELECT id, name, profile, caps FROM api_keys WHERE key_hash = ? AND revoked = 0", (key_hash,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],)
        )
        conn.commit()
    conn.close()
    return row


def resolve_principal(raw_key: str | None) -> Principal | None:
    """De la llave al Principal (quién + qué perfil). None si la llave no vale o su perfil es inválido."""
    row = _fetch_key(raw_key)
    if row is None:
        return None
    caps = _parse_caps(row["caps"])
    profile = profiles.profile_from_key(row["profile"] or profiles.DEFAULT_NAME, caps)
    if profile is None:
        return None
    return Principal(
        key_id=row["id"],
        key_name=row["name"],
        profile=profile,
        is_admin=profile.is_default and caps is None,
    )


def _verify_raw_key(raw_key: str | None) -> bool:
    """True solo para llaves ADMIN (usado por voice/api.py). Las de anfitrión abren en Fase 2."""
    principal = resolve_principal(raw_key)
    return principal is not None and principal.is_admin and principal.profile.enabled


def _principal_from_header(authorization: str | None) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta el header Authorization: Bearer <api_key>.")

    raw_key = authorization.split(" ", 1)[1].strip()
    principal = resolve_principal(raw_key)
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key inválida o revocada.")
    if not principal.profile.enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="El perfil de esta llave está desactivado.")
    return principal


def require_principal(authorization: str | None = Header(default=None)) -> Principal:
    """
    CUALQUIER llave válida (admin o de anfitrión). Solo para la lista blanca de anfitrión
    (Fase 2, E10). No usar en endpoints que no estén en esa lista.
    """
    return _principal_from_header(authorization)


def require_admin_key(authorization: str | None = Header(default=None)) -> Principal:
    """Solo llaves admin (perfil 'elipse' sin caps). Es el default de todo endpoint."""
    principal = _principal_from_header(authorization)
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Esta llave no tiene permiso para este endpoint.")
    return principal


def require_api_key(authorization: str | None = Header(default=None)) -> Principal:
    """
    Dependency de FastAPI para rutas HTTP normales. Espera:
    Authorization: Bearer elp_xxxxxxxx...
    Misma firma de siempre; ahora devuelve el Principal (los endpoints que lo ignoran siguen igual).
    FASE 1: equivale a require_admin_key (llaves de anfitrión → 403).
    """
    return require_admin_key(authorization)


async def require_principal_ws(websocket: WebSocket) -> Principal:
    """WebSocket para CUALQUIER llave válida y activa (admin o de anfitrión). El dueño de la tarea se
    comprueba aparte (main.py): una tarea ajena se trata como inexistente."""
    principal = resolve_principal(websocket.query_params.get("token"))
    if principal is None or not principal.profile.enabled:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="API key inválida o revocada.")
    return principal


async def require_api_key_ws(websocket: WebSocket) -> Principal:
    """
    Dependency para el endpoint WebSocket. Los navegadores no pueden mandar
    headers custom al abrir un WebSocket, así que aquí la llave viaja como
    query param: /v1/ws/{task_id}?token=elp_xxxxxxxx
    FASE 1: solo llaves admin.
    """
    principal = resolve_principal(websocket.query_params.get("token"))
    if principal is None or not principal.is_admin or not principal.profile.enabled:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="API key inválida o revocada.")
    return principal