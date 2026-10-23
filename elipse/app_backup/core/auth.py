"""
Autenticación por API key (Fase 5 — Multi-dispositivo).

Mismo patrón que plataformas como OpenRouter o Mistral: generas una llave con
un nombre (ej. "telefono", "reloj", "laptop"), la usas como
'Authorization: Bearer <llave>' en cada request, y puedes revocarla individualmente
sin afectar las demás.

La llave en texto plano SOLO se muestra una vez, al crearla. En la base de
datos solo se guarda su hash (sha256) — igual que una contraseña, nunca el
valor real — así que si alguien lee elipse.db no puede reconstruir las llaves.
"""

import hashlib
import secrets

from fastapi import Header, HTTPException, WebSocket, WebSocketException, status

from app.core.db import get_connection


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_key(name: str) -> str:
    """
    Genera una nueva API key para un dispositivo/cliente. Devuelve la llave en
    texto plano — es la ÚNICA vez que se puede ver, guárdala en ese momento.
    """
    raw_key = f"elp_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)

    conn = get_connection()
    conn.execute(
        "INSERT INTO api_keys (name, key_hash) VALUES (?, ?)",
        (name.strip() or "sin_nombre", key_hash),
    )
    conn.commit()
    conn.close()

    return raw_key


def list_keys():
    """Lista las llaves (sin exponer el valor real, solo metadata)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, revoked, created_at, last_used_at FROM api_keys ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_key(key_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def _verify_raw_key(raw_key: str | None) -> bool:
    if not raw_key:
        return False

    key_hash = _hash_key(raw_key)
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM api_keys WHERE key_hash = ? AND revoked = 0", (key_hash,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],)
        )
        conn.commit()
    conn.close()
    return row is not None


def require_api_key(authorization: str | None = Header(default=None)):
    """
    Dependency de FastAPI para rutas HTTP normales. Espera:
    Authorization: Bearer elp_xxxxxxxx...
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta el header Authorization: Bearer <api_key>.")

    raw_key = authorization.split(" ", 1)[1].strip()
    if not _verify_raw_key(raw_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key inválida o revocada.")


async def require_api_key_ws(websocket: WebSocket):
    """
    Dependency para el endpoint WebSocket. Los navegadores no pueden mandar
    headers custom al abrir un WebSocket, así que aquí la llave viaja como
    query param: /v1/ws/{task_id}?token=elp_xxxxxxxx
    """
    raw_key = websocket.query_params.get("token")
    if not _verify_raw_key(raw_key):
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="API key inválida o revocada.")