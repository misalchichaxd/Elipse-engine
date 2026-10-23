"""
Marca invisible de tema ([[TEMA:CODIGO]] / [[TEMA:GENERAL]]).

Idea: en vez de adivinar con diccionarios de palabras clave si la conversación
sigue siendo sobre código o el usuario ya cambió de tema (posiblemente en otro
idioma, con jerga, o sin usar ninguna palabra "clave"), le pedimos al MISMO
modelo que ya está respondiendo que declare el tema al final de su propia
respuesta. Esto:

  - No cuesta una llamada extra: son ~5 tokens pegados a una respuesta que de
    todas formas se iba a generar y pagar.
  - Entiende negación/cambio de tema de forma natural en cualquier idioma,
    porque lo decide el modelo (que entendió el mensaje), no un regex.
  - Se guarda tal cual en el historial (mensajes "assistant" en la DB), así
    que el router puede leer el ÚLTIMO tema declarado por el asistente en vez
    de re-escanear mensajes viejos del usuario buscando palabras clave.

Lo que esto NO resuelve (por diseño, no por descuido): la decisión de qué
proveedor usa el mensaje ACTUAL tiene que tomarse antes de llamar a cualquier
modelo (huevo y gallina), así que para el mensaje que acaba de llegar seguimos
necesitando una heurística rápida (ver router.py). La marca invisible resuelve
el arrastre de historial, no el primer mensaje de un cambio de tema.
"""

import re
from typing import Callable, Optional

TAG_OPEN = "[[TEMA:"
TAG_RE = re.compile(r"\s*\[\[TEMA:(CODIGO|GENERAL)\]\]\s*$")

INSTRUCTION = (
    "Al terminar CADA respuesta final que le des al usuario (no cuando pidas "
    "usar una herramienta), agregá en una línea aparte, al final de todo, una "
    "de estas dos marcas exactas: [[TEMA:CODIGO]] si tu respuesta trata sobre "
    "programación/código/debugging, o [[TEMA:GENERAL]] si no. Es una marca "
    "técnica invisible para el usuario (el sistema la recorta antes de "
    "mostrarla) — no la menciones, no la expliques, no le preguntes al "
    "usuario por ella, solo agregala."
)


def strip_topic_tag(text: str) -> tuple[str, Optional[str]]:
    """
    Separa la marca invisible del texto visible.
    Devuelve (texto_limpio, tema) donde tema es "code", "general" o None si el
    modelo no la agregó (ej. proveedores que no siguen esta instrucción).
    """
    if not text:
        return text, None
    match = TAG_RE.search(text)
    if not match:
        return text, None
    clean = text[: match.start()]
    topic = "code" if match.group(1) == "CODIGO" else "general"
    return clean, topic


def last_declared_topic(history: list) -> Optional[str]:
    """
    Busca en el historial (mensajes ya guardados, más nuevo primero) la marca
    de tema del último mensaje del asistente que la tenga. None si ninguno la
    tiene (conversación vieja, anterior a esta funcionalidad, o proveedor que
    no la sigue).
    """
    for msg in reversed(history or []):
        if msg.get("role") != "assistant":
            continue
        _, topic = strip_topic_tag(msg.get("content", ""))
        if topic is not None:
            return topic
    return None


class StreamTagFilter:
    """
    Envoltorio de on_delta que retiene los últimos caracteres del streaming
    para que la marca invisible ([[TEMA:...]]) nunca le llegue al usuario,
    aunque venga partida en varios pedacitos (deltas) distintos.

    Uso:
        filt = StreamTagFilter(emit_chunk)
        result = provider.chat_stream(..., on_delta=filt.on_delta)
        clean_reply, topic = filt.finish(result.content)
    """

    # Más que de sobra para "\n[[TEMA:GENERAL]]" (18 caracteres) más margen
    # por si el proveedor manda espacios/saltos de línea extra.
    _HOLD_BACK = 40

    def __init__(self, emit_chunk: Callable[[str], None]):
        self._emit_chunk = emit_chunk
        self._buffer = ""
        self._emitted_len = 0

    def on_delta(self, text: str) -> None:
        self._buffer += text
        if len(self._buffer) > self._HOLD_BACK:
            cut = len(self._buffer) - self._HOLD_BACK
            self._emit_chunk(self._buffer[:cut])
            self._emitted_len += cut
            self._buffer = self._buffer[cut:]

    def finish(self, full_text: str) -> tuple[str, Optional[str]]:
        """
        Al terminar el streaming: usa el texto final autoritativo del
        proveedor (full_text) para no cortar la marca a la mitad, emite lo
        que falte del buffer ya limpio, y devuelve (texto_limpio, tema).
        """
        clean_full, topic = strip_topic_tag(full_text)
        # Lo que falta emitir es la parte limpia que todavía no salió por chunks.
        remaining = clean_full[self._emitted_len:]
        if remaining:
            self._emit_chunk(remaining)
        return clean_full, topic