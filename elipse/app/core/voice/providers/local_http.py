"""Proveedor local: cualquier servidor HTTP propio que reciba texto y devuelva audio (ver LEEME §5.2)."""

from typing import Iterator

import httpx

from app.config import settings
from app.core.voice.providers.base import VoiceError, VoiceProvider


class LocalHttpProvider(VoiceProvider):
    name = "local"
    is_cloud = False

    @property
    def mime(self) -> str:                                # configurable: audio/mpeg o audio/wav
        return settings.voice_local_mime

    def available(self) -> bool:
        return bool(settings.voice_local_url)

    def stream(self, text: str, voice_id: str = "") -> Iterator[bytes]:
        body = {"text": text}
        if voice_id:
            body["voice_id"] = voice_id
        timeout = httpx.Timeout(connect=5, read=settings.voice_timeout_seconds, write=10, pool=5)
        try:
            with httpx.stream("POST", settings.voice_local_url, json=body, timeout=timeout) as r:
                if r.status_code != 200:
                    r.read()
                    raise VoiceError(f"TTS local respondió {r.status_code}: {r.text[:200]}")
                got = False
                for chunk in r.iter_bytes(4096):
                    if chunk:
                        got = True
                        yield chunk
                if not got:
                    raise VoiceError("El TTS local devolvió audio vacío.")
        except httpx.HTTPError as e:
            raise VoiceError(f"TTS local: {type(e).__name__}: {e}") from e
