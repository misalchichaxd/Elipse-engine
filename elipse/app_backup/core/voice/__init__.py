"""
API pública del módulo de voz. El resto de Elipse solo usa esto:

    plan_for_final(status, reply, speak) -> (bloque_voice | None, texto_a_decir | None)
    build_text(text)                     -> texto hablable (o la frase fija) para 🔊 / say
    open_stream(text)                    -> (proveedor, ganancia, iterador de audio)
    status() / update_settings(patch)    -> estado y ajustes en caliente

La voz nunca bloquea al agente: todo esto corre DESPUÉS de que la respuesta existe.
"""

from app.core.voice import filter as _filter
from app.core.voice import router, store
from app.core.voice.router import VoiceUnavailable, open_stream, warm  # noqa: F401


def build_text(text: str, force: bool = True) -> str:
    """Texto hablable de `text`, o la frase fija si no queda nada (solo código/documento)."""
    cfg = store.effective()
    spoken = _filter.analyze(text, cfg["max_chars"])["spoken"]
    return spoken or (cfg["fallback"] if force else "")


def plan_for_final(status: str, reply: str, speak: bool | None = None) -> tuple[dict | None, str | None]:
    """
    Decide si la respuesta final se habla. Prioridad: speak por mensaje > interruptor global > modo.
    Nunca se habla: tareas no completadas (canceladas, errores, esperando confirmación) ni respuestas vacías.
    """
    if status != "completado" or not (reply or "").strip() or speak is False:
        return None, None
    cfg = store.effective()
    if speak is None and (not cfg["enabled"] or cfg["mode"] in ("off", "manual")):
        return None, None

    a = _filter.analyze(reply, cfg["max_chars"])
    if speak is None and cfg["mode"] == "smart":
        # conversacional y corto; calla si es sobre todo código, tabla o documento
        if not a["spoken"] or a["ratio"] < 0.4 or a["long"]:
            return None, None
    text = a["spoken"] or cfg["fallback"]

    pv = router.preview(cfg)
    if not pv:                                          # sin proveedor usable: ni se ofrece audio
        return None, None
    mime, gain = pv
    return {"speak": True, "gain": gain, "mime": mime}, text


def brief() -> dict:
    cfg = store.effective()
    return {"enabled": cfg["enabled"], "mode": cfg["mode"], "routing": cfg["routing"]}


def status() -> dict:
    cfg = store.effective()
    pv = router.preview(cfg)
    return {
        "enabled": cfg["enabled"],
        "mode": cfg["mode"],
        "routing": cfg["routing"],
        "provider": cfg["provider"],
        "cloud_allowed": cfg["cloud_allowed"],
        "max_chars": cfg["max_chars"],
        "gain": pv[1] if pv else 1.0,                  # ganancia del proveedor que hablaría ahora
        "ready": pv is not None,
        "providers": router.status(cfg),
    }


def update_settings(patch: dict) -> dict:
    store.update(patch)
    return status()
