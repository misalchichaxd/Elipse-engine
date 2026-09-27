import re
from typing import Optional

CODE_KEYWORDS = [
    "código", "codigo", "función", "funcion", "bug", "error",
    "script", "python", "javascript", "clase", "debug",
    "variable", "compilar", "sintaxis", "api", "endpoint",
    "sql", "html", "css", "algoritmo"
]

HISTORY_LOOKBACK = 4  # cuántos mensajes previos del usuario revisar para sostener el rol "código" en un seguimiento

_KEYWORD_PATTERNS = {kw: re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) for kw in CODE_KEYWORDS}

# Palabras que, apareciendo cerca de la palabra clave, indican que en realidad
# se está pidiendo SALIR del modo código, no entrar — p. ej. "no quiero código",
# "sin código", "sal del modo código", "deja el código". Sin esto, pedir salir
# del modo código queda atrapado en loop, porque la frase misma contiene la
# palabra clave que dispara modo código de nuevo.
_NEGATION_WORDS = {
    "no", "nunca", "sin", "sal", "salir", "salgamos", "salte", "salite",
    "deja", "dejá", "dejar", "dejemos", "quita", "quitar", "para", "parale",
    "basta", "olvida", "olvidate", "olvidemos",
}
_NEGATION_WINDOW = 4  # cuántas palabras antes de la keyword se revisan


def _find_code_keyword(text: str) -> Optional[str]:
    lowered = text or ""
    for keyword, pattern in _KEYWORD_PATTERNS.items():
        match = pattern.search(lowered)
        if not match:
            continue
        if _has_negation_before(lowered, match.start()):
            continue  # es un pedido de SALIR del modo código, no de entrar
        return keyword
    return None


def _has_negation_before(lowered: str, pos: int) -> bool:
    words_before = re.findall(r"\w+", lowered[:pos].lower())[-_NEGATION_WINDOW:]
    return any(w in _NEGATION_WORDS for w in words_before)


def _wants_to_exit_code(text: str) -> bool:
    """
    True si el mensaje actual pide explícitamente salir del modo código
    ("no quiero código", "sal del modo código", "sin código"...). Esto tiene
    que ganarle al arrastre de historial: si no, un pedido de código real de
    hace un rato sigue "empujando" código durante HISTORY_LOOKBACK mensajes
    más, aunque el usuario acabe de pedir explícitamente salir — quedando en
    loop sin poder salir nunca.
    """
    lowered = text or ""
    for pattern in _KEYWORD_PATTERNS.values():
        match = pattern.search(lowered)
        if match and _has_negation_before(lowered, match.start()):
            return True
    return False


def choose_provider(message: str, history: Optional[list] = None):
    """
    Devuelve (model_name, reason) según reglas simples.
    Primero mira el mensaje actual; si ahí no hay pistas, mira los últimos
    mensajes *del usuario* en la conversación — así un "¿y en C?" después de
    pedir código en Python sigue yendo al proveedor de código, en vez de caer
    al general solo porque esa frase suelta no repite ninguna palabra clave.

    Ojo: el lookback filtra a role == "user" a propósito. Si mirara también
    las respuestas del propio asistente, cualquier mención casual de "código"
    o "clase" en una explicación del bot (p. ej. listando sus capacidades)
    quedaría "pegada" y arrastraría el modo código a mensajes siguientes que
    no tienen nada que ver — exactamente el bug que esto evita.

    Un pedido explícito de SALIR del modo código en el mensaje actual gana
    siempre, incluso por encima del arrastre de historial — si no, un "no
    quiero código" queda atrapado en loop porque un pedido de código real de
    hace un par de mensajes lo sigue empujando de vuelta a "code".
    """
    if _wants_to_exit_code(message):
        return "general", "se pidió salir del modo código"

    keyword = _find_code_keyword(message)
    if keyword:
        return "code", f"palabra clave de código detectada: '{keyword}'"

    user_history = [m for m in (history or []) if m.get("role") == "user"]
    for prev in reversed(user_history[-HISTORY_LOOKBACK:]):
        content = prev.get("content", "")
        if _wants_to_exit_code(content):
            break  # acá el usuario pidió salir; no mirar pedidos de código más viejos que esto
        keyword = _find_code_keyword(content)
        if keyword:
            return "code", f"sigue una conversación de código (se detectó '{keyword}' antes)"

    return "general", "sin palabras clave de código, usando modelo general"