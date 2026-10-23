"""
Enrutador de proveedores: manual / failover / round_robin, con cortacircuitos.

- failover: si un proveedor falla ANTES del primer pedazo de audio, pasa al siguiente.
- Si falla a mitad del audio, se corta ahí (no se mezcla otra voz en la misma frase).
- Cortacircuitos: tras N fallos seguidos, el proveedor se aparta `cooldown` segundos.
- Modo privado (cloud_allowed=false): se ignoran los proveedores en la nube.
"""

import threading
import time
from typing import Iterator

from app.core.voice import store
from app.core.voice.providers.base import VoiceError, VoiceProvider
from app.core.voice.providers.fish import FishProvider
from app.core.voice.providers.local_http import LocalHttpProvider


class VoiceUnavailable(Exception):
    """Ningún proveedor pudo (o puede) hablar ahora mismo."""


_PROVIDERS: dict[str, VoiceProvider] = {"fish": FishProvider(), "local": LocalHttpProvider()}
_lock = threading.Lock()
_state: dict[str, dict] = {
    n: {"fails": 0, "until": 0.0, "ok": 0, "errors": 0, "last_error": "", "first_chunk_ms": None, "total_ms": None}
    for n in _PROVIDERS
}
_rr = 0


def _in_cooldown(name: str) -> bool:
    return _state[name]["until"] > time.time()


def candidates(cfg: dict | None = None, rotate: bool = True) -> list[VoiceProvider]:
    """Proveedores que se intentarían ahora, en orden. rotate=False no avanza el turno de round_robin."""
    global _rr
    cfg = cfg or store.effective()
    names = cfg["providers"]
    if cfg["routing"] == "manual":
        names = [cfg["provider"]] if cfg["provider"] else []
    out = []
    for n in names:
        p = _PROVIDERS.get(n)
        if not p or not p.available():
            continue
        if p.is_cloud and not cfg["cloud_allowed"]:
            continue
        if cfg["routing"] != "manual" and _in_cooldown(n):
            continue                                   # en modo manual tú mandas: no se aparta
        out.append(p)
    if cfg["routing"] == "round_robin" and len(out) > 1:
        with _lock:
            k = _rr % len(out)
            if rotate:
                _rr += 1
        out = out[k:] + out[:k]
    return out


def preview(cfg: dict | None = None) -> tuple[str, float] | None:
    """(mime, gain) del primer candidato, sin consumir el turno de round_robin. None si no hay."""
    cfg = cfg or store.effective()
    cands = candidates(cfg, rotate=False)
    if not cands:
        return None
    p = cands[0]
    return p.mime, float(cfg["providers_cfg"][p.name]["gain"])


def _fail(name: str, err: Exception, cfg: dict) -> None:
    with _lock:
        s = _state[name]
        s["fails"] += 1
        s["errors"] += 1
        s["last_error"] = str(err)[:200]
        if s["fails"] >= cfg["fail_threshold"]:
            s["until"] = time.time() + cfg["cooldown"]
            s["fails"] = 0


def _ok(name: str, **stats) -> None:
    with _lock:
        s = _state[name]
        s["fails"] = 0
        s["until"] = 0.0
        s.update(stats)


def open_stream(text: str) -> tuple[VoiceProvider, float, Iterator[bytes]]:
    """Abre el audio. Bloquea hasta el primer pedazo (ahí se decide el failover). Llamar desde un hilo."""
    cfg = store.effective()
    cands = candidates(cfg)
    if not cands:
        raise VoiceUnavailable(
            "No hay proveedor de voz disponible (sin configurar, en pausa por fallos, o modo privado sin proveedor local)."
        )
    last: Exception | None = None
    for p in cands:
        pc = cfg["providers_cfg"][p.name]
        t0 = time.perf_counter()
        it = p.stream(text, pc["voice_id"])
        try:
            first = next(it)
        except StopIteration:
            last = VoiceError("audio vacío")
            _fail(p.name, last, cfg)
            continue
        except Exception as e:                         # VoiceError u otra: se prueba el siguiente
            last = e
            _fail(p.name, e, cfg)
            continue
        ms = round((time.perf_counter() - t0) * 1000)
        _ok(p.name, first_chunk_ms=ms)
        return p, float(pc["gain"]), _chain(p, first, it, t0, cfg)
    raise VoiceUnavailable(f"Falló la voz: {last}")


def _chain(p: VoiceProvider, first: bytes, it: Iterator[bytes], t0: float, cfg: dict) -> Iterator[bytes]:
    try:
        yield first
        for chunk in it:
            yield chunk
        with _lock:
            _state[p.name]["ok"] += 1
            _state[p.name]["total_ms"] = round((time.perf_counter() - t0) * 1000)
    except VoiceError as e:                            # a mitad del audio: se corta, sin cambiar de voz
        _fail(p.name, e, cfg)
    finally:
        close = getattr(it, "close", None)
        if close:
            close()


def warm() -> None:
    """Precalienta el proveedor que hablaría ahora (la voz en vivo lo llama al enviar el mensaje)."""
    for p in candidates(rotate=False)[:1]:
        try:
            p.warm()
        except Exception:
            pass


def status(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or store.effective()
    now = time.time()
    rows = []
    for name, p in _PROVIDERS.items():
        s = _state[name]
        rows.append({
            "name": name,
            "is_cloud": p.is_cloud,
            "available": p.available(),
            "in_cooldown": s["until"] > now,
            "cooldown_left_s": max(0, round(s["until"] - now)),
            "enabled_in_list": name in cfg["providers"],
            "voice_id": cfg["providers_cfg"][name]["voice_id"],
            "gain": cfg["providers_cfg"][name]["gain"],
            "ok": s["ok"],
            "errors": s["errors"],
            "last_error": s["last_error"],
            "first_chunk_ms": s["first_chunk_ms"],
            "total_ms": s["total_ms"],
        })
    return rows
