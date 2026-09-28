import re
from typing import Optional

from app.core.topic_tag import last_declared_topic

# Palabras clave de código en varios idiomas (es, en, pt, fr, it, de). No es
# perfecto —ningún diccionario lo es—, pero cubre bastante más que "código" a
# secas: sirve para el mensaje ACTUAL, donde todavía no hay ninguna respuesta
# del modelo de la que colgarnos (ver topic_tag.py para por qué el arrastre de
# historial ya no depende de esta lista).
CODE_KEYWORDS = [
    # español
    "código", "codigo", "codear", "codeas", "programar", "programa",
    "programación", "programacion", "función", "funcion", "bug", "error",
    "script", "clase", "depurar", "variable", "compilar", "sintaxis",
    "algoritmo", "repositorio",
    # inglés
    "code", "coding", "coder", "program", "programming", "function",
    "debug", "debugging", "class", "compile", "syntax", "algorithm",
    "repo", "repository",
    # portugués
    "codificar", "programação", "programacao", "função", "funcao",
    # francés
    "programmation", "fonction", "déboguer", "deboguer",
    # italiano
    "programmazione", "funzione",
    # alemán
    "programmieren", "funktion", "fehler",
    # comunes / técnicos, no ligados a un idioma
    "python", "javascript", "typescript", "java", "sql", "html", "css",
    "api", "endpoint", "git", "github",
]
CODE_KEYWORDS = sorted(set(CODE_KEYWORDS))  # sin duplicados (p. ej. "función" en es/pt)

HISTORY_LOOKBACK = 4  # cuántos mensajes previos del usuario revisar para sostener el rol "código" en un seguimiento

_KEYWORD_PATTERNS = {kw: re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) for kw in CODE_KEYWORDS}

# Palabras que, apareciendo cerca de la palabra clave, indican que en realidad
# se está pidiendo SALIR del modo código, no entrar — p. ej. "no quiero código",
# "sin código", "sal del modo código", "deja el código", "I don't want code",
# "não quero código". Multi-idioma por la misma razón que CODE_KEYWORDS: sin
# esto, pedir salir del modo código en otro idioma queda atrapado en loop.
_NEGATION_WORDS = {
    # español
    "no", "nunca", "sin", "sal", "salir", "salgamos", "salte", "salite",
    "deja", "dejá", "dejar", "dejemos", "quita", "quitar", "para", "parale",
    "basta", "olvida", "olvidate", "olvidemos", "cambiemos", "otro", "otra",
    # inglés (el tokenizador parte los apóstrofes: "don't" -> "don" + "t",
    # "won't" -> "won" + "t", etc., por eso están las formas partidas también)
    "not", "don't", "dont", "don", "won't", "wont", "won", "can't", "cant",
    "isn't", "isnt", "never", "without", "stop", "quit",
    "leave", "forget", "enough", "another",
    # portugués
    "não", "nao", "sem", "sair", "chega", "esquece", "esqueça",
    # francés
    "pas", "jamais", "sans", "arrête", "arrete", "assez",
    # italiano
    "non", "mai", "senza", "smettila",
    # alemán
    "nicht", "nie", "ohne", "genug",
}
_NEGATION_WINDOW = 4  # cuántas palabras antes de la keyword se revisan

# Frases sueltas de "cambio de tema" que NO necesitan estar cerca de una
# palabra clave de código para valer como salida explícita — p. ej. "cambiemos
# de tema" no menciona "código" en absoluto. Cubren varios idiomas a propósito.
_TOPIC_SHIFT_PHRASES = [
    "cambiemos de tema", "cambiemos de tópico", "hablemos de otra cosa",
    "hablemos de otro tema", "dejemos el código", "olvida el código",
    "change the subject", "change topic", "let's talk about something else",
    "lets talk about something else", "forget about the code",
    "mudemos de assunto", "vamos falar de outra coisa",
    "changeons de sujet", "parlons d'autre chose",
    "cambiamo argomento", "parliamo d'altro",
]


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


def _has_topic_shift_phrase(lowered: str) -> bool:
    return any(phrase in lowered for phrase in _TOPIC_SHIFT_PHRASES)


def _wants_to_exit_code(text: str) -> bool:
    """
    True si el mensaje actual pide explícitamente salir del modo código
    ("no quiero código", "I don't want code", "cambiemos de tema"...). Esto
    tiene que ganarle a cualquier arrastre: si no, un pedido de código real de
    hace un rato lo sigue "empujando" de vuelta, aunque el usuario acabe de
    pedir explícitamente salir — quedando en loop sin poder salir nunca.
    """
    lowered = (text or "").lower()
    if _has_topic_shift_phrase(lowered):
        return True
    for pattern in _KEYWORD_PATTERNS.values():
        match = pattern.search(lowered)
        if match and _has_negation_before(lowered, match.start()):
            return True
    return False


def choose_provider(message: str, history: Optional[list] = None):
    """
    Devuelve (model_name, reason) según estas prioridades:

    1. El mensaje ACTUAL pide explícitamente salir del modo código (en
       cualquiera de los idiomas cubiertos) -> gana siempre.
    2. El mensaje ACTUAL trae una palabra clave de código -> "code".
    3. Si ninguna de las dos anteriores da una señal clara, se usa el ÚLTIMO
       tema que el propio asistente declaró en su respuesta anterior (marca
       invisible [[TEMA:...]], ver topic_tag.py). Esto reemplaza el viejo
       re-escaneo de mensajes de usuario con palabras clave: es más confiable
       porque lo decidió un modelo que entendió la conversación completa, no
       un diccionario — y funciona en cualquier idioma sin agregar una sola
       palabra clave más.
    4. Si tampoco hay marca declarada (conversación vieja, o el proveedor no
       la sigue), fallback al re-escaneo de mensajes de usuario por palabra
       clave, como antes.
    """
    if _wants_to_exit_code(message):
        return "general", "se pidió salir del modo código"

    keyword = _find_code_keyword(message)
    if keyword:
        return "code", f"palabra clave de código detectada: '{keyword}'"

    declared = last_declared_topic(history or [])
    if declared == "code":
        return "code", "sigue una conversación de código (tema declarado por el asistente)"
    if declared == "general":
        return "general", "tema declarado como general por el asistente"

    # Fallback: sin marca disponible, volvemos al re-escaneo clásico.
    user_history = [m for m in (history or []) if m.get("role") == "user"]
    for prev in reversed(user_history[-HISTORY_LOOKBACK:]):
        content = prev.get("content", "")
        if _wants_to_exit_code(content):
            break  # acá el usuario pidió salir; no mirar pedidos de código más viejos que esto
        keyword = _find_code_keyword(content)
        if keyword:
            return "code", f"sigue una conversación de código (se detectó '{keyword}' antes)"

    return "general", "sin palabras clave de código, usando modelo general"