import hashlib
import hmac
import re
import time

_TS_RE = re.compile(r"^(0|[1-9][0-9]*)$")


def verify(secret: str, profile: str, tool: str, body: bytes, ts_header, sig_header,
           now: float | None = None, max_skew: int = 60) -> bool:
    """body = los bytes EXACTOS recibidos (no vuelvas a serializar el JSON)."""
    if not isinstance(ts_header, str) or not _TS_RE.match(ts_header):
        return False
    ts = int(ts_header)
    if abs((now if now is not None else time.time()) - ts) > max_skew:
        return False
    message = f"{ts}.{profile}.{tool}.{hashlib.sha256(body).hexdigest()}"
    good = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, str(sig_header or ""))
