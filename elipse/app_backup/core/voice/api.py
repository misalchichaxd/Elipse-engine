"""Endpoints /v1/voice/*. Se montan con `app.include_router(voice_router)` en main.py."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core import voice
from app.core.auth import _verify_raw_key, require_api_key

router = APIRouter(prefix="/v1/voice", tags=["voice"])


def _auth_header_or_token(authorization: str | None = Header(default=None), token: str | None = Query(default=None)):
    """El stream acepta ?token= porque <audio> no puede mandar headers."""
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
    if not _verify_raw_key(raw or token):
        raise HTTPException(status_code=401, detail="API key inválida o revocada.")


class ProviderPatch(BaseModel):
    voice_id: str | None = None
    gain: float | None = None


class SettingsPatch(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    routing: str | None = None
    provider: str | None = None
    cloud_allowed: bool | None = None
    providers: dict[str, ProviderPatch] | None = None


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    # False = si de la frase no queda nada hablable (código, tabla...) responde 204 en vez de la frase fija.
    # La voz en vivo manda frase por frase y no quiere un "Listo, te dejé el resultado" por cada trozo de código.
    fallback: bool = True


def _streaming(text: str) -> StreamingResponse:
    try:
        provider, gain, it = voice.open_stream(text)          # def sync → corre en el threadpool
    except voice.VoiceUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return StreamingResponse(
        it,
        media_type=provider.mime,
        headers={
            "Cache-Control": "no-store",
            "X-Voice-Provider": provider.name,
            "X-Voice-Gain": str(gain),
            "Access-Control-Expose-Headers": "X-Voice-Provider, X-Voice-Gain",
        },
    )


@router.get("/status", dependencies=[Depends(require_api_key)])
def voice_status():
    return voice.status()


@router.post("/settings", dependencies=[Depends(require_api_key)])
def voice_settings(patch: SettingsPatch):
    try:
        return voice.update_settings(patch.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/warm", dependencies=[Depends(require_api_key)])
def voice_warm():
    """Precalienta la conexión con el proveedor de voz (el cliente lo llama al pulsar Enviar)."""
    voice.warm()
    return Response(status_code=204)


@router.post("/say", dependencies=[Depends(require_api_key)])
def voice_say(req: SayRequest):
    """Dice un texto suelto (botón de prueba). Pasa por el mismo filtro: a la nube solo llega lo hablable."""
    text = voice.build_text(req.text, force=req.fallback)
    if not text:
        return Response(status_code=204)                       # nada que decir
    return _streaming(text)


@router.get("/{task_id}/stream", dependencies=[Depends(_auth_header_or_token)])
def voice_task_stream(task_id: str, force: bool = False):
    """Audio de la respuesta final de la tarea. `force=1` = botón 🔊 (ignora interruptor y modo)."""
    from app.core.tasks import get_task

    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="La tarea ya no está disponible (se guardan 1 hora).")
    text = task.get("voice_text")
    if not text and force and task["status"] == "completado":
        text = voice.build_text((task.get("result") or {}).get("reply", ""))
    if not text:
        return Response(status_code=204)                       # nada que decir
    return _streaming(text)
