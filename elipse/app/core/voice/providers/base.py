"""Interfaz común de los proveedores de voz. El resto de Elipse solo conoce "voz", no "Fish"."""

from typing import Iterator


class VoiceError(Exception):
    """Falla de un proveedor (red, auth, cuota, respuesta vacía...)."""


class VoiceProvider:
    name: str = "base"
    is_cloud: bool = False      # True si el texto sale de tu PC
    mime: str = "audio/mpeg"    # "audio/mpeg", "audio/wav"

    def available(self) -> bool:
        """Barato y sin red: ¿está configurado (clave / URL)?"""
        return False

    def warm(self) -> None:
        """Precalienta la conexión (opcional). Por defecto no hace nada."""

    def stream(self, text: str, voice_id: str = "") -> Iterator[bytes]:
        """Audio por pedazos. Los errores se lanzan como VoiceError al pedir el primer pedazo."""
        raise NotImplementedError
