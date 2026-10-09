"""
Ajustes de voz en caliente. Los valores del .env son el punto de partida; lo que cambias desde la
interfaz se guarda en SQLite (tabla voice_settings, se crea sola) y pisa al .env.
"""

import json
import threading

from app.config import settings
from app.core.db import get_connection

PROVIDER_NAMES = ("fish", "local")
MODES = ("off", "manual", "smart", "always")
ROUTINGS = ("manual", "failover", "round_robin")

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            conn = get_connection()
            conn.execute("CREATE TABLE IF NOT EXISTS voice_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.commit()
            rows = conn.execute("SELECT key, value FROM voice_settings").fetchall()
            conn.close()
            _cache = {r["key"]: json.loads(r["value"]) for r in rows}
        return _cache


def effective() -> dict:
    """Configuración vigente = .env + overrides guardados."""
    o = _load()
    providers = [p.strip() for p in settings.voice_providers.split(",") if p.strip() in PROVIDER_NAMES]
    pcfg = {
        "fish": {"voice_id": settings.voice_fish_voice_id, "gain": settings.voice_fish_gain},
        "local": {"voice_id": settings.voice_local_voice_id, "gain": settings.voice_local_gain},
    }
    for name in PROVIDER_NAMES:
        for field in ("voice_id", "gain"):
            key = f"{name}.{field}"
            if key in o:
                pcfg[name][field] = o[key]
    return {
        "enabled": o.get("enabled", settings.voice_enabled),
        "mode": o.get("mode", settings.voice_mode),
        "routing": o.get("routing", settings.voice_routing),
        "provider": o.get("provider") or (providers[0] if providers else ""),   # el elegido en modo manual
        "cloud_allowed": o.get("cloud_allowed", settings.voice_cloud_allowed),
        "providers": providers,
        "providers_cfg": pcfg,
        "max_chars": settings.voice_max_chars,
        "fallback": settings.voice_spoken_fallback,
        "cooldown": settings.voice_cooldown_seconds,
        "fail_threshold": settings.voice_fail_threshold,
    }


def _save(key: str, value) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO voice_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()


def update(patch: dict) -> None:
    """Valida y guarda. Lanza ValueError con un mensaje claro si algo no cuadra."""
    flat: dict = {}
    if "enabled" in patch:
        flat["enabled"] = bool(patch["enabled"])
    if "cloud_allowed" in patch:
        flat["cloud_allowed"] = bool(patch["cloud_allowed"])
    if "mode" in patch:
        if patch["mode"] not in MODES:
            raise ValueError(f"mode debe ser uno de {MODES}")
        flat["mode"] = patch["mode"]
    if "routing" in patch:
        if patch["routing"] not in ROUTINGS:
            raise ValueError(f"routing debe ser uno de {ROUTINGS}")
        flat["routing"] = patch["routing"]
    if "provider" in patch:
        if patch["provider"] not in PROVIDER_NAMES:
            raise ValueError(f"provider debe ser uno de {PROVIDER_NAMES}")
        flat["provider"] = patch["provider"]
    for name, pp in (patch.get("providers") or {}).items():
        if name not in PROVIDER_NAMES:
            raise ValueError(f"proveedor desconocido: {name}")
        if "voice_id" in pp:
            vid = str(pp["voice_id"]).strip()
            if len(vid) > 128 or any(c.isspace() for c in vid):
                raise ValueError("voice_id inválido")
            flat[f"{name}.voice_id"] = vid
        if "gain" in pp:
            g = float(pp["gain"])
            if not 0.1 <= g <= 4.0:
                raise ValueError("gain debe estar entre 0.1 y 4.0")
            flat[f"{name}.gain"] = g
    _load()
    for k, v in flat.items():
        _save(k, v)
    with _lock:
        _cache.update(flat)
