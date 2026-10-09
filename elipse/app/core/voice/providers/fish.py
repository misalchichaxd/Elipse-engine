"""Fish Audio (nube). POST https://api.fish.audio/v1/tts → audio por pedazos (HTTP streaming)."""

import threading
from typing import Iterator

import httpx

from app.config import settings
from app.core.voice.providers.base import VoiceError, VoiceProvider

FISH_URL = "https://api.fish.audio/v1/tts"
FISH_HOST = "https://api.fish.audio/"

# Un solo cliente para todo el proceso: reutiliza la conexión (DNS + TCP + TLS una vez, no por frase).
# Con la voz en vivo se piden varias frases seguidas; abrir conexión nueva cada vez suma ~0.3-0.8 s a cada una.
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=90))
        return _client


def _reset_client() -> None:
    global _client
    with _client_lock:
        old, _client = _client, None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


class FishProvider(VoiceProvider):
    name = "fish"
    is_cloud = True
    mime = "audio/mpeg"

    def available(self) -> bool:
        return bool(settings.voice_fish_api_key)

    def warm(self) -> None:
        """Abre la conexión TLS con Fish antes de necesitarla (no gasta crédito: no genera audio)."""
        if not self.available():
            return
        try:
            _get_client().head(FISH_HOST, timeout=5)
        except httpx.HTTPError:
            pass

    def stream(self, text: str, voice_id: str = "") -> Iterator[bytes]:
        headers = {
            "Authorization": f"Bearer {settings.voice_fish_api_key}",
            "Content-Type": "application/json",
            "model": settings.voice_fish_model,          # el header "model" es obligatorio en Fish
        }
        latency = (settings.voice_fish_latency or "").strip().lower()
        body = {
            "text": text,
            "format": "mp3",
            "latency": latency if latency in ("normal", "balanced") else "balanced",
            "temperature": min(1.0, max(0.0, settings.voice_fish_temperature)),
            "top_p": min(1.0, max(0.0, settings.voice_fish_top_p)),
        }
        if voice_id:
            body["reference_id"] = voice_id
        timeout = httpx.Timeout(connect=10, read=settings.voice_timeout_seconds, write=10, pool=10)
        for attempt in (1, 2):
            got = False
            try:
                with _get_client().stream("POST", FISH_URL, headers=headers, json=body, timeout=timeout) as r:
                    if r.status_code != 200:
                        r.read()
                        raise VoiceError(f"Fish respondió {r.status_code}: {r.text[:200]}")
                    for chunk in r.iter_bytes(4096):
                        if chunk:
                            got = True
                            yield chunk
                    if not got:
                        raise VoiceError("Fish devolvió audio vacío.")
                    return
            except httpx.TransportError as e:
                # Conexión reutilizada que el servidor ya había cerrado: se reintenta UNA vez con una nueva,
                # pero solo si todavía no salió ningún audio (si ya salió, repetir lo duplicaría).
                if attempt == 1 and not got:
                    _reset_client()
                    continue
                raise VoiceError(f"Fish: {type(e).__name__}: {e}") from e
            except httpx.HTTPError as e:
                raise VoiceError(f"Fish: {type(e).__name__}: {e}") from e
