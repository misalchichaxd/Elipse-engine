"""
Firma y verificación del callback (PROTOCOL.md §4–§5). Solo biblioteca estándar.

Equivale a docs/protocol/ref/verify_python.py y a app/core/callback.py del motor
(con la regla estricta del timestamp que exige el protocolo).
"""

import hashlib
import hmac
import re
import time

MAX_SKEW_S = 60
_TS_RE = re.compile(r"^(0|[1-9][0-9]*)$")


def sign(secret: str, ts, profile: str, tool: str, body: bytes) -> str:
    """hex( HMAC_SHA256(secreto, f"{ts}.{perfil}.{tool}.{sha256_hex(body)}") )"""
    msg = f"{ts}.{profile}.{tool}.{hashlib.sha256(body).hexdigest()}"
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_request(secret: str, profile: str, tool: str, body: bytes, ts_header, sig_header,
                   now: float | None = None, max_skew: int = MAX_SKEW_S) -> bool:
    """
    `body` = los bytes EXACTOS recibidos (nunca un JSON vuelto a serializar).
    True solo si el timestamp es un entero canónico, está en la ventana y la firma coincide.
    """
    if not isinstance(ts_header, str) or not _TS_RE.match(ts_header):
        return False
    ts = int(ts_header)
    if abs((now if now is not None else time.time()) - ts) > max_skew:
        return False
    good = sign(secret, ts, profile, tool, body)
    # Se compara en bytes: compare_digest con str no ASCII lanzaría TypeError.
    return hmac.compare_digest(good.encode("ascii"), str(sig_header or "").encode("utf-8"))
