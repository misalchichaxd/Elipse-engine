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


def effective(profile=None) -> dict:
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
    out = {
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
    return out if profile is None or profile.is_default else _profile_layers(out, o, profile)


def _apply_flat(out: dict, flat: dict) -> None:
    """Aplica claves planas (enabled, mode, fish.voice_id…) validando; lo inválido se ignora en silencio."""
    for k in ("enabled", "cloud_allowed"):
        if k in flat:
            out[k] = bool(flat[k])
    for k, valid in (("mode", MODES), ("routing", ROUTINGS), ("provider", PROVIDER_NAMES)):
        if flat.get(k) in valid:
            out[k] = flat[k]
    for name in PROVIDER_NAMES:
        vid = flat.get(f"{name}.voice_id")
        if isinstance(vid, str) and vid and len(vid) <= 128 and not any(c.isspace() for c in vid):
            out["providers_cfg"][name]["voice_id"] = vid
        try:
            g = float(flat[f"{name}.gain"])
            if 0.1 <= g <= 4.0:
                out["providers_cfg"][name]["gain"] = g
        except (KeyError, TypeError, ValueError):
            pass


def _profile_layers(out: dict, o: dict, profile) -> dict:
    """Capas de un perfil: .env < voice_settings globales < spec.voice (∩ caps) < filas 'profile:<n>:*'.
    La nube solo sale si el spec (∩ caps) la permite Y data_policy no es local_only: nada puede subirla."""
    from app.core.profiles import Capabilities
    caps = Capabilities(profile)
    out = {**out, "providers_cfg": {n: dict(c) for n, c in out["providers_cfg"].items()}}
    spec_v = caps.voice_config()
    flat = {k: spec_v[k] for k in ("enabled", "mode", "routing", "provider") if k in spec_v}
    if isinstance(spec_v.get("voice_id"), str):
        flat[f"{spec_v.get('provider') or out['provider']}.voice_id"] = spec_v["voice_id"]
    _apply_flat(out, flat)
    prefix = f"profile:{profile.name}:"
    _apply_flat(out, {k[len(prefix):]: v for k, v in o.items() if k.startswith(prefix) and not k.endswith("cloud_allowed")})
    if not caps.voice_allowed():
        out["enabled"] = False
    out["cloud_allowed"] = bool(out["cloud_allowed"]) and bool(spec_v.get("cloud_allowed")) and caps.data_policy() != "local_only"
    return out


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
