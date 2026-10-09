"""
Pipeline por plan: el orquestador chico PLANIFICA una vez y el CÓDIGO ejecuta.

Flujo:
    1. plan     -> el orquestador (4B/7B) devuelve un JSON corto:
                   {"pasos": ["research","search_images","write_code"],
                    "tema": "Ninjago", "count": 5, "modificar": false}
    2. validar  -> el plan se sanea EN CÓDIGO (lista blanca, tema presente en el
                   mensaje original, límites numéricos, orden fijo).
    3. ejecutar -> research y search_images corren en paralelo; write_code espera
                   a los dos. Los datos pesados viajan por `state`, nunca por el 4B.
                   Cada paso pasa por verify_tool_result.
    4. cerrar   -> el sistema arma la respuesta (plantilla, o una frase del modelo
                   si pipeline_close_with_model=True) y pega código/notas tal cual.
    5. fallback -> si algo del plan no sirve, try_run() devuelve None y core.py
                   sigue con el Agent Loop de siempre.

Este módulo no conoce HTTP ni hilos de la API: recibe un provider y un `emit`.
"""

import json
import base64
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config import settings
from app.core import typos
from app.core.codegen import load_last_code, run_code_generation, run_code_patch, save_last_code
from app.core.providers import strip_think_blocks
from app.core.research import run_research_pipeline
from app.core.tools import search_images, verify_tool_result
import logging   # arriba, con los otros imports

EmitFn = Callable[[dict], None]

# ---------------------------------------------------------------- constantes

ALLOWED_STEPS = ("research", "search_images", "write_code")
STEP_ALIASES = {
    "research_topic": "research", "investigar": "research", "investigacion": "research",
    "images": "search_images", "imagenes": "search_images", "search_image": "search_images",
    "code": "write_code", "codigo": "write_code",
}
STEP_TO_TOOL = {"research": "research_topic", "search_images": "search_images", "write_code": "write_code"}

MIN_COUNT, MAX_COUNT, DEFAULT_COUNT = 1, 15, 4
IMAGE_SPARE = 2                 # se piden N+2 imágenes para tener margen ante duplicadas/rotas
MAX_PLAN_MESSAGE_CHARS = 600    # lo que ve el planificador del mensaje (es solo para clasificar)
TRUNC_MARKER = "[...salida truncada por longitud...]"   # el que agrega codegen.py
CONTINUATION_TAIL_CHARS = 3000

# ---------------------------------------------------------------- patrones

CODE_PATTERN = re.compile(
    r"\b(c[oó]digo|script|snippet|programa|funci[oó]n|p[aá]gina|html|css|javascript|"
    r"python|java|app|aplicaci[oó]n|web|sitio|landing|galer[ií]a|collage)\b", re.I)
IMAGE_PATTERN = re.compile(r"\b(imagen|im[aá]genes|foto|fotos|galer[ií]a|collage)\b", re.I)
RESEARCH_PATTERN = re.compile(
    r"\b(investig\w*|averigu\w*|inf[oó]rmate|informaci[oó]n\s+(sobre|de|acerca))\b", re.I)
CREATE_CODE_PATTERN = re.compile(
    r"\b(cre[aá]|cre[aá]me|arm[aá]|arma|arm[aá]me|gener[aá]|genera|escrib[eií]|hac[eé]|haz|hazme|"
    r"constru[ií]|dise[nñ]a|program[aá]|dame|quiero|necesito|desarroll[aá])\b"
    r".{0,60}\b(p[aá]gina|html|web|galer[ií]a|script|c[oó]digo|programa|app|aplicaci[oó]n|"
    r"collage|sitio|landing)\b", re.I | re.S)
DOC_PATTERN = re.compile(r"\b(pdf|word|docx|informe|reporte|ensayo|monograf[ií]a|documento)\b", re.I)
MODIFY_PATTERN = re.compile(
    r"\b(m[eé]tele\w*|agr[eé]gale\w*|a[ñn][aá]dele\w*|a[ñn]adile\w*|modific[aá]\w*|c[aá]mbiale\w*|"
    r"cambiale\w*|ahora\s+(agrega|ponle|hac[eé]|haz)\w*|mejor[aá]\w*|corrig[eé]\w*)\b"
    r"|\b(al\s+c[oó]digo\s+anterior|a\s+esa\s+p[aá]gina|ese\s+c[oó]digo|esa\s+p[aá]gina)\b", re.I)
STRONG_MODIFY_PATTERN = re.compile(
    r"\b(m[eé]tele\w*|agr[eé]gale\w*|a[ñn][aá]dele\w*|a[ñn]adile\w*)\b"
    r"|\b(al\s+c[oó]digo\s+anterior|a\s+esa\s+p[aá]gina|ese\s+c[oó]digo|esa\s+p[aá]gina)\b", re.I)
# Mensajes de corrección/seguimiento sobre la página anterior que NO traen palabras de "código" ni de "imagen":
# "el de arriba no es el logo", "debía ser el logo de peanuts", "la imagen no existe", "ponle otro color al header"
PAGE_ELEMENT_RE = re.compile(
    r"\b(logo|logotipo|header|encabezado|footer|banner|fondo|colou?r(?:es)?|colores|t[ií]tulo|fuente|letra|"
    r"bot[oó]n|men[uú]|secci[oó]n|dise[nñ]o|estilo)\b", re.I)
CORRECTION_RE = re.compile(
    r"\b(no\s+(?:es|era|sale|se\s+ve|existe|carga|funciona)|deb[ií]a\s+ser|deb[ií]an\s+ser|est[aá]\s+mal|"
    r"no\s+es\s+el|ese\s+no|esa\s+no|c[aá]mbial\w*|cambiarl\w*|reempl[aá]zal\w*|pon[eé]l\w*)\b", re.I)
# "faltó la imagen", "no está la imagen", "ponsela", "ponle la foto": piden completar la página anterior
ADD_MISSING_RE = re.compile(
    r"\b(falt[oó]|faltan?|no\s+(?:est[aá]n?|aparece\w*|sale\w*|se\s+ven?|hay|existe\w*)|"
    r"pon[eé]?sel[aeo]s?|pon[eé]l[aeo]s?|ponle\w*)\b", re.I)


def is_page_followup(message: str) -> bool:
    """¿El mensaje pide completar/corregir la página anterior? (una sola definición para todo el módulo)"""
    return bool(
        STRONG_MODIFY_PATTERN.search(message) or MORE_IMAGES_RE.search(message)
        or ADD_MISSING_RE.search(message)
        or (PAGE_ELEMENT_RE.search(message) and CORRECTION_RE.search(message))
    )

_NUM_WORDS = {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
              "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12}
COUNT_PATTERN = re.compile(
    r"\b(\d{1,2}|" + "|".join(_NUM_WORDS) + r")\s+(?:im[aá]gen(?:es)?|fotos?|fotograf[ií]as?|casillas|pics)\b", re.I)

CODE_BLOCK_RE = re.compile(r"```([\w+#.-]*)[ \t]*\n(.*?)```", re.S)
IMG_SRC_RE = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*["'])(https?://[^"']+)""", re.I)
CSS_BG_RE = re.compile(r"""(background(?:-image)?\s*:[^;}]*?url\(\s*["']?)(https?://[^"')\s]+)""", re.I)
IMAGE_URL_LINE_RE = re.compile(r"URL de imagen directa[^:]*:\s*(\S+)")
# "otras 5 fotos", "más imágenes": seguimiento que NO trae tema propio
MAX_COUNT_SEARCH = 30     # tope de candidatos pedidos al buscador (MAX_COUNT limita lo que se USA)
MORE_IMAGES_RE = re.compile(r"\botr[ao]s?\s+(?:\d+\s+|un\s+par\s+de\s+|unas\s+)?(?:fotos?|im[aá]genes|imagen)\b|"
                            r"\bm[aá]s\s+(?:fotos?|im[aá]genes)\b", re.I)

_STOPWORDS = {"los", "las", "del", "una", "uno", "unos", "unas", "con", "para", "sobre", "que", "por",
              "the", "and", "como", "esta", "este", "esos", "esas", "muy", "mas"}
_GENERIC = {"imagen", "imagenes", "foto", "fotos", "fotografia", "fotografias", "pagina", "web", "html",
            "galeria", "collage", "codigo", "script", "programa", "sitio", "app", "aplicacion", "landing",
            "casillas", "quiero", "necesito", "haz", "hazme", "hace", "dame", "crea", "creame", "arma",
            "armame", "genera", "escribe", "busca", "investiga", "investigar", "buscame", "un", "al", "me",
            "mi", "tu", "de", "la", "el", "en", "y", "a", "es", "lo", "se", "su", "informacion", "tema",
            # verbos de orden y comodines de seguimiento: "metele otras 5 fotos" NO tiene tema ("Metele" no es un tema)
            "metele", "metelo", "metela", "meterle", "agregale", "agrega", "agregar", "agregame", "anadele", "anade",
            "anadir", "ponle", "pon", "ponme", "ponlo", "otras", "otros", "otra", "otro", "mas", "tambien", "ahora",
            "cambia", "cambiale", "quita", "quitale", "borra", "elimina", "sigue", "continua", "algunas", "algunos",
            "estas", "estos", "esas", "unas", "puedes", "podrias", "podes", "porfa", "favor", "pls", "ya", "mejor",
            "bien", "bueno", "buena", "correcto", "correcta", "mal", "existe", "existen", "existan", "arriba", "abajo"}


# ---------------------------------------------------------------- utilidades de texto

def _norm(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", _norm(text)) if len(w) >= 3 and w not in _STOPWORDS]


def _clean_topic(raw: str) -> str:
    words = re.findall(r"[\wÀ-ÿ'-]+", raw or "")
    kept = [w for w in words if _norm(w) not in _GENERIC and _norm(w) not in _STOPWORDS and not w.isdigit()]
    return " ".join(kept).strip()


_QUE_PREFIX_RE_CACHE: dict = {}


_LAST_WORD_RE = re.compile(r"[\wÀ-ÿ]+")
_QUE_ANYWHERE_RE = re.compile(r"\bque\b", re.IGNORECASE)
_SUBJUNCTIVE_ENDING_RE = re.compile(r"(an|en)$", re.IGNORECASE)


def _looks_like_subordinate_verb(topic: str, message: str) -> bool:
    """
    Detecta cuando el 'tema' es en realidad el verbo de una subordinada del pedido
    ('que las imágenes EXISTAN', 'que se vean bien', 'que FUNCIONEN') y no un tema
    de búsqueda.

    Caso real que motivó esto: "dame que las 5 imagenes existan" -> 'existan'
    aparece literalmente en el mensaje (pasa topic_matches_message sin problema,
    el verbo no está pegado a 'que' así que un chequeo de adyacencia no alcanza)
    pero buscar imágenes de 'existan' da basura (definiciones, canales de
    YouTube, letras de canciones).

    Señal usada (deliberadamente angosta para no rechazar temas legítimos):
    el tema de UNA sola palabra es exactamente la ÚLTIMA palabra del mensaje,
    el mensaje contiene un 'que' en algún lado (subordinada), y la palabra
    termina en -an/-en (terminación típica del subjuntivo de 3ra persona
    plural en español: existan, funcionen, carguen, aparezcan, muestren...).
    Un tema real de 2+ palabras ('UTT Tijuana') nunca pasa por acá; uno de una
    sola palabra que termina distinto ('Ninjago', 'React', 'Berlín') tampoco.
    """
    words = topic.split()
    if len(words) != 1:
        return False
    word = words[0]
    if not _SUBJUNCTIVE_ENDING_RE.search(word) or len(word) < 4:
        return False
    if not _QUE_ANYWHERE_RE.search(message):
        return False
    msg_words = _LAST_WORD_RE.findall(message)
    return bool(msg_words) and _norm(msg_words[-1]) == _norm(word)


def topic_matches_message(topic: str, message: str) -> bool:
    """El tema del plan tiene que aparecer en lo que el usuario escribió (anti 'Ninjago -> mapache')."""
    if not topic or not topic.strip():
        return False
    toks = _tokens(topic)
    nm = _norm(message)
    if not toks:
        return _norm(topic).strip() in nm
    hits = sum(1 for t in toks if t in nm)
    return hits / len(toks) >= 0.5


_TOPIC_RE = re.compile(
    r"\b(?:de|del|sobre|acerca\s+de)\s+(.+?)"
    r"(?=\s+(?:en|con|para|que|usando|as[ií])\b|\s+y\s+(?:arm|hac|cre|gen|escrib|pon|met)\w*|[,.;:!?\n]|$)",
    re.I)


def fallback_topic(message: str) -> Optional[str]:
    """
    Tema sacado por regex del mensaje original (plan B cuando el del 4B/7B propone
    uno que no aparece en el pedido). Devuelve None si no hay señal confiable — NO
    devuelve "lo que haya sobrado" a cualquier costo.

    Caso real que motivó esto: "dame que las 5 imagenes existan" -> ninguna frase
    "de/sobre X" matchea, así que se cae al modo débil (limpiar el mensaje entero
    de palabras genéricas) y lo único que sobrevive es "existan" — una palabra
    suelta que no es un tema, es el verbo de la frase. Buscar imágenes de "existan"
    da resultados basura (definiciones, canales de YouTube, letras de canciones).

    Regla: la extracción por FRASE ("investigá SOBRE Python" -> "Python") sí se
    acepta aunque quede en una sola palabra, porque la frase misma es la señal.
    La extracción por LIMPIEZA DEL MENSAJE ENTERO solo se acepta si sobreviven 2+
    palabras (más difícil que sea ruido); si sobrevive una sola, se considera baja
    confianza y se devuelve None — quien llama debe abortar ese paso/plan entero
    y dejar que el Agent Loop de siempre lo resuelva con el historial completo.
    """
    for m in _TOPIC_RE.finditer(message):
        cleaned = _clean_topic(m.group(1))
        if cleaned:
            return cleaned[:100]

    cleaned = _clean_topic(message)
    words = cleaned.split()[:6]
    if len(words) >= 2:
        return " ".join(words)
    return None


# ---------------------------------------------------------------- referencias ("este personaje", "ella")
#
# fallback_topic() y compañía sacan el tema SOLO del mensaje actual. Con «hazme un pdf sobre este
# personaje» eso deja la palabra suelta «personaje» y se pierde de quién se hablaba. Estas funciones
# detectan esa referencia y la resuelven con el historial; si no se puede con confianza, el que
# llama debe abandonar el pipeline y dejar que el Agent Loop lo resuelva con la conversación entera.

_DEICTIC_TOPIC_RE = re.compile(
    r"\b(?:sobre|acerca\s+de|de|del|para)\s+(?:este|esta|ese|esa|aquel|aquella|estos|estas|esos|esas)\s+\w+"
    r"|\b(?:sobre|acerca\s+de|de)\s+(?:ella|[eé]l|ellos|ellas)\b", re.I)
_GENERIC_TOPIC_WORDS = {"personaje", "personajes", "tema", "temas", "cosa", "cosas", "chica", "chico",
                        "ella", "el", "esto", "eso", "imagen", "imagenes", "foto", "fotos"}
# nombres que ya confirmó la galería visual (vision.py / gallery.py) en un mensaje del historial
_GALLERY_NAME_RE = re.compile(r"(?:Coincidencia en mi galería:|Aprendí a)\s*«([^»]{2,60})»")


def needs_referent(message: str, topic: str = "") -> bool:
    """¿El tema del pedido es una referencia a la conversación ('este personaje', 'ella') y no un tema propio?"""
    if _DEICTIC_TOPIC_RE.search(message or ""):
        return True
    toks = re.findall(r"[a-z0-9]+", _norm(topic))
    return bool(toks) and all(t in _GENERIC_TOPIC_WORDS for t in toks)


def _clean_referent(raw: str) -> str:
    for line in (raw or "").splitlines():
        line = re.sub(r"^\s*(?:tema|respuesta|personaje)\s*:\s*", "", line.strip(), flags=re.I)
        line = line.strip(" \t\"'`*_«».,;:!?()")
        if line:
            return " ".join(line.split()[:8])[:80]
    return ""


def resolve_referent(provider, message: str, history: list, max_turns: int = 8) -> Optional[str]:
    """
    A quién/qué se refiere «este personaje»/«ella»/«eso» mirando los últimos turnos. Devuelve el
    nombre o tema concreto, o None si no se puede saber con confianza. Orden de fiabilidad:
      1) un nombre que la galería visual ya confirmó en la conversación (sin gastar el modelo);
      2) una pregunta corta al modelo, cuya respuesta se ACEPTA solo si aparece en la conversación
         (anti-alucinación, mismo criterio que topic_matches_message para el plan).
    """
    recent = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-max_turns:]
    if not recent:
        return None
    for m in reversed(recent):
        found = _GALLERY_NAME_RE.findall(m.get("content") or "")
        if found:
            return found[-1].strip()

    lines = []
    for m in recent:
        who = "Usuario" if m["role"] == "user" else "Asistente"
        lines.append(f"{who}: {' '.join((m.get('content') or '').split())[:500]}")
    convo = "\n".join(lines)
    prompt = (
        "Lee esta conversación y el pedido final. El pedido usa una referencia (\"este personaje\", \"ella\", \"eso\"...). "
        "¿A qué persona, personaje o tema CONCRETO se refiere? Responde SOLO con su nombre propio o tema "
        "(máximo 8 palabras), tal como aparece en la conversación, sin explicación ni comillas. "
        "Si la conversación no lo deja claro, responde exactamente: NINGUNO.\n\n"
        f"Conversación:\n{convo}\n\nPedido: {message}\n\nRespuesta:"
    )
    try:
        raw = strip_think_blocks(provider.chat([{"role": "user", "content": prompt}], allow_tools=False).content or "")
    except Exception as e:
        logging.getLogger("elipse").warning("resolve_referent: falló el modelo: %s", e)
        return None
    topic = _clean_referent(raw)
    if not topic or _norm(topic) in ("ninguno", "ninguna", "no se", "no lo se", "no esta claro"):
        return None
    if not topic_matches_message(topic, convo):
        logging.getLogger("elipse").info("resolve_referent: descarté '%s' (no aparece en la conversación)", topic)
        return None
    return topic


def _count_from_message(message: str) -> Optional[int]:
    m = COUNT_PATTERN.search(message)
    if not m:
        return None
    raw = m.group(1).lower()
    return int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)


def _clamp_count(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    return max(MIN_COUNT, min(n, MAX_COUNT))


def _blocks(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in CODE_BLOCK_RE.finditer(text or "")]


def _longest_block(text: str) -> str:
    blocks = _blocks(text)
    return max((b for _, b in blocks), key=len, default="")


def find_previous_block(history: list[dict]) -> tuple[str, str]:
    """(lenguaje, código) del último bloque que ELIPSE mostró; si el historial ya no lo tiene
    (HISTORY_LIMIT, reinicio), el último código guardado en disco. ("", "") si no hay nada."""
    lang, code = load_last_code()          # lo último que se mostró (el historial es corto y se pierde al reiniciar)
    if len(code.strip()) >= 150:
        return lang, code.strip()
    for msg in reversed(history or []):
        if msg.get("role") != "assistant":
            continue
        blocks = [(lang, body) for lang, body in _blocks(msg.get("content", "")) if len(body.strip()) >= 150]
        if blocks:
            lang, body = max(blocks, key=lambda b: len(b[1]))
            return lang, body.strip()
    return "", ""


def find_previous_code(history: list[dict]) -> str:
    """Último código que ELIPSE mostró (para pedidos tipo 'metele hover'). Lo busca el código, no el 4B."""
    return find_previous_block(history)[1]


IMG_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>)\]]+?\.(?:jpe?g|png|webp|gif|svg)(?:\?[^\s\"'<>)\]]*)?", re.I)


def recent_image_urls(history: list[dict], existing_code: str = "", limit: int = 6) -> list[str]:
    """URLs de imagen reales que ELIPSE mostró en los últimos turnos (p. ej. 'busqué la bandera' -> 'ponsela'),
    sin las que la página ya usa. Las más recientes primero."""
    found: list[str] = []
    for msg in reversed((history or [])[-8:]):
        if msg.get("role") != "assistant":
            continue
        text = re.sub(r"```.*?```", "", msg.get("content", "") or "", flags=re.S)   # fuera de bloques de código
        for url in IMG_URL_IN_TEXT_RE.findall(text):
            if url not in found and url not in (existing_code or ""):
                found.append(url)
    return found[:limit]


def _persist_code(text: str) -> None:
    """Guarda el código completo que quedó (para poder editarlo después aunque el historial ya no lo tenga)."""
    blocks = _blocks(text)
    if blocks:
        lang, body = max(blocks, key=lambda b: len(b[1]))
        if len(body.strip()) >= 150:       # un snippet corto de una charla no pisa el código real
            save_last_code(body, lang)


# ---------------------------------------------------------------- plan

@dataclass
class Plan:
    steps: list[str]
    topic: str = ""
    count: int = 0
    modify: bool = False
    notes: list[str] = field(default_factory=list)   # correcciones que hizo el código al plan del modelo
    items: list[str] = field(default_factory=list)   # "imágenes de cada uno de estos: sonic, tails..." -> una imagen por elemento


_ITEM_BULLET_RE = re.compile(r"^[\s\-\*\u2022\d\.\)]+")


def _clean_items(parts: list[str], max_words: int = 4) -> list[str]:
    items, seen = [], set()
    for raw in parts:
        it = _ITEM_BULLET_RE.sub("", raw or "").strip(" \t.,;:!?¿¡\"'«»()")
        words = it.split()
        if not it or len(it) > 40 or len(words) > max_words or it.isdigit():
            continue
        if _norm(it) in seen:
            continue
        seen.add(_norm(it))
        items.append(it)
    return items[:8]


_SEG_START = r"(?:1|una|un|la\s+otr[ao]|el\s+otr[ao]|otr[ao])\s+(?:foto\s+|imagen\s+)?(?:solo\s+|s[oó]lo\s+)?(?:de|del)\s+"
_SEG_END = (r"(?=\s+(?:en|para)\s+(?:un\s+|el\s+|formato\s+)?(?:pdf|word|docx|p[aá]gina|web)\b|"
            r"\s+(?:porfa|por\s+favor|pls)\b|\s+y\s+(?:la\s+|el\s+)?otr[ao]\b|,\s*(?:la\s+|el\s+)?otr[ao]\b|[?!.;\n]|$)")
_DESCRIBED_RE = re.compile(_SEG_START + r"(.+?)" + _SEG_END, re.I)


def _extract_described(message: str) -> list[str]:
    """'2 fotos 1 de cosplay de miku y la otra de cosplay pero de chun-li' -> ['cosplay de miku', 'cosplay de chun-li'].
    Solo cuenta si hay 2+ descripciones (con una sola es una imagen descrita, no una enumeración)."""
    parts = []
    for m in _DESCRIBED_RE.finditer(message or ""):
        it = re.sub(r"^(?:la|el|las|los|una|un)\s+", "", m.group(1).strip(), flags=re.I)
        parts.append(re.sub(r"\bpero\s+", "", it, flags=re.I))
    items = _clean_items(parts, max_words=6)
    return items if len(items) >= 2 else []


def extract_items(message: str) -> list[str]:
    """Lista de elementos del pedido: 'imágenes de cada uno de estos:\nsonic\ntails\namy' -> ['sonic','tails','amy'],
    o imágenes descritas una por una ('1 de cosplay de miku y la otra de chun-li').
    Solo cuenta con 2+ elementos cortos. Sin dos puntos exige 3+ ('fotos de Sonic, Tails y Amy')."""
    text = (message or "").strip()
    described = _extract_described(text)
    if described:
        return described
    if ":" in text:
        tail = text.rsplit(":", 1)[1]
        if "\n" in tail.strip():
            items = _clean_items(tail.split("\n"))
        else:
            items = _clean_items(re.split(r"[;,]+|\s+y\s+|\s+e\s+", tail), max_words=3)
        if len(items) >= 2:
            return items
    m = re.search(r"\b(?:im[aá]genes|imagen|fotos?)\s+(?:de|del)\s+(?:cada\s+(?:uno|una)\s+de\s+)?(?:estos|estas|los|las)?\s*([^\n:]+)$", text, re.I)
    if m:
        items = _clean_items(re.split(r"[;,]+|\s+y\s+|\s+e\s+", m.group(1)), max_words=3)
        if len(items) >= 3:
            return items
    return []


def _last_assistant_text(history: list[dict]) -> str:
    for msg in reversed(history or []):
        if msg.get("role") == "assistant":
            return msg.get("content", "") or ""
    return ""


def followup_context(history: list[dict], prev_code: str) -> tuple[bool, str]:
    """(lo último fue código, tema de la conversación). Para mensajes sin tema propio ('metele otras 5 fotos'):
    si lo último fue una página, su tema es el de la página (<title>/<h1>); si no, el tema del pedido anterior."""
    last = _last_assistant_text(history)
    last_is_code = "```" in last
    if last_is_code and prev_code:
        theme = _page_theme(prev_code)
        if theme:
            return True, theme
    for i in range(len(history or []) - 1, -1, -1):             # último pedido del usuario que SÍ tenía tema
        if history[i].get("role") == "user":
            t = fallback_topic(history[i].get("content", "") or "")
            if t:
                return last_is_code, t
    return last_is_code, ""


def _should_plan(message: str, has_prev_code: bool) -> bool:
    """Pre-filtro barato: no gastar una llamada al 4B en charla normal."""
    if CODE_PATTERN.search(message) or IMAGE_PATTERN.search(message) or RESEARCH_PATTERN.search(message):
        return True
    return has_prev_code and bool(MODIFY_PATTERN.search(message) or is_page_followup(message))


PLANNER_PROMPT = """Sos un planificador. Leé el pedido y respondé SOLO un JSON, sin texto antes ni después.

Pasos posibles:
- "research": investigar un tema en internet.
- "search_images": buscar imágenes reales.
- "write_code": escribir código, una página o un script.

Reglas:
- "pasos" lleva solo lo que el pedido necesita. Si es charla, una pregunta simple, archivos, cálculos, fecha o memoria: "pasos": [].
- "tema": el tema concreto TAL COMO lo escribió el usuario (personaje, serie, juego, librería). No inventes ni cambies palabras.
- "count": cuántas imágenes (1 a 15). Solo si usás search_images.
- "modificar": true solo si pide cambiar un código anterior ("metele", "agregale"...).

Ejemplos:
Pedido: "hazme una galería de 5 imágenes de Ninjago"
{{"pasos":["search_images","write_code"],"tema":"Ninjago","count":5,"modificar":false}}
Pedido: "investiga cómo funciona Spring Security y escríbeme un ejemplo en Java"
{{"pasos":["research","write_code"],"tema":"Spring Security","count":0,"modificar":false}}
Pedido: "busca fotos de Green Lantern"
{{"pasos":["search_images"],"tema":"Green Lantern","count":4,"modificar":false}}
Pedido: "metele efecto hover a las imágenes"
{{"pasos":["write_code"],"tema":"","count":0,"modificar":true}}
Pedido: "hola, cómo estás"
{{"pasos":[],"tema":"","count":0,"modificar":false}}

Hay código previo en la conversación: {prev}
Pedido: "{message}"
JSON:"""


def _extract_json(text: str) -> Optional[dict]:
    text = strip_think_blocks(text or "")
    text = re.sub(r"```(?:json)?", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def validate_plan(data: dict, message: str, has_prev_code: bool, followup_topic: str = "",
                  last_is_code: bool = False) -> Plan:
    """Todas las reglas duras viven acá, en código: no se confía en el modelo.
    followup_topic / last_is_code: de qué venía hablando la conversación (ver followup_context)."""
    notes: list[str] = []

    # 1) lista blanca + alias + sin duplicados
    raw_steps = data.get("pasos", data.get("steps", []))
    if not isinstance(raw_steps, list):
        raw_steps = []
    steps: list[str] = []
    for s in raw_steps:
        if not isinstance(s, str):
            continue
        key = _norm(s).strip().replace(" ", "_")
        key = STEP_ALIASES.get(key, key)
        if key in ALLOWED_STEPS and key not in steps:
            steps.append(key)
        elif key not in ALLOWED_STEPS:
            notes.append(f"paso desconocido descartado: {s!r}")

    # 2) modificación de un código anterior: la detecta el código además del modelo
    if not steps and has_prev_code and is_page_followup(message):
        steps = (["search_images"] if (IMAGE_PATTERN.search(message) or re.search(r"\blogo\b", message, re.I)) else []) + ["write_code"]
        notes.append("plan vacío pero el mensaje pide completar/corregir la página anterior")

    # 2b) seguimiento de la página que se acaba de generar ("metele otras 5 fotos"): el 4B suele devolver solo
    # search_images y entonces las fotos se buscaban sueltas sin tocar la página.
    follow_code = bool(has_prev_code and last_is_code
                       and is_page_followup(message)
                       and not CREATE_CODE_PATTERN.search(message))
    if follow_code and "write_code" not in steps and "research" not in steps:
        steps = ([] if steps else (["search_images"] if IMAGE_PATTERN.search(message) else [])) + steps + ["write_code"]
        notes.append("seguimiento de la página anterior: se agregó write_code")

    # 3) pedido claro de crear código y el 4B se olvidó del último paso
    if steps and "write_code" not in steps and CREATE_CODE_PATTERN.search(message):
        steps.append("write_code")
        notes.append("se agregó write_code: el pedido pide crear código")

    if not steps:
        return Plan(steps=[], notes=notes)

    modify = (
        has_prev_code and "write_code" in steps
        and (bool(data.get("modificar")) or bool(STRONG_MODIFY_PATTERN.search(message)) or follow_code)
    )

    # 3b) "imágenes de cada uno de estos: a, b, c" + código: una imagen POR elemento (no N fotos genéricas del primero)
    items = extract_items(message) if (IMAGE_PATTERN.search(message) and ("write_code" in steps or "research" in steps
                                                                          or "search_images" in steps)) else []
    if items and "search_images" not in steps:
        steps.insert(0, "search_images")
        notes.append("se agregó search_images: el pedido pide una imagen por cada elemento de la lista")

    # 4) tema: tiene que aparecer en el mensaje original, y tiene que ser CONFIABLE.
    # Si no hay ninguna señal confiable de tema, no seguimos con basura: se aborta
    # el plan entero (steps=[]) y el llamador cae al Agent Loop de siempre, que sí
    # tiene el historial completo para resolver algo ambiguo como un follow-up
    # ("que existan", "que se vean bien") en vez de buscar a ciegas.
    topic = ""
    if items and "search_images" in steps and "research" not in steps:
        cand = str(data.get("tema", data.get("topic", "")) or "").strip()[:100]
        topic = cand if cand and topic_matches_message(cand, message) else items[0]
        notes.append(f"modo por elemento: {', '.join(items)}")
    elif "research" in steps or "search_images" in steps:
        topic = str(data.get("tema", data.get("topic", "")) or "").strip()[:100]

        if topic and not topic_matches_message(topic, message):
            notes.append(f"tema {topic!r} rechazado (no aparece en el pedido)")
            topic = ""

        if topic and _looks_like_subordinate_verb(topic, message):
            notes.append(f"tema {topic!r} rechazado (es el verbo de una subordinada, no un tema real)")
            topic = ""

        if topic and not _clean_topic(topic):
            notes.append(f"tema {topic!r} rechazado (son solo verbos/comodines, no un tema)")
            topic = ""

        if not topic:
            fallback = fallback_topic(message)
            if fallback and _looks_like_subordinate_verb(fallback, message):
                notes.append(f"tema de respaldo {fallback!r} también es verbo de subordinada; se descarta")
                fallback = None
            if fallback is None and followup_topic:
                fallback = followup_topic
                notes.append(f"el pedido no trae tema propio: se usa el de la conversación ({followup_topic!r})")
            if fallback is None:
                notes.append(
                    "no hay un tema confiable para este pedido — se aborta el pipeline para "
                    "este turno, sigue el Agent Loop normal (con el historial completo)."
                )
                return Plan(steps=[], notes=notes)
            notes.append(f"se usa tema de respaldo: {fallback!r}")
            topic = fallback

    # 4b) el tema se enriquece con el de la página: "logo" -> "Peanuts logo", no "logo bien" a secas
    if topic and "search_images" in steps and followup_topic:
        theme = re.split(r"\s[-–—|:]\s", followup_topic)[0].strip()      # "Snoopy - Página temática" -> "Snoopy"
        if theme and _norm(theme) not in _norm(topic) and (follow_code or len(topic.split()) <= 2):
            topic = f"{theme} {topic}"[:100]
            notes.append(f"tema enriquecido con el de la página: {topic!r}")
    if topic and re.search(r"\blogo\b", message, re.I) and "logo" not in _norm(topic):
        topic += " logo"

    # 5) límites numéricos (lo que dice el usuario le gana al modelo)
    count = 0
    if "search_images" in steps:
        from_msg = _count_from_message(message)
        count = _clamp_count(from_msg if from_msg is not None else data.get("count", DEFAULT_COUNT))
        if items:
            count = _clamp_count(len(items))

    return Plan(steps=steps, topic=topic, count=count, modify=modify, notes=notes, items=items)


def build_plan(provider, message: str, has_prev_code: bool, followup_topic: str = "",
               last_is_code: bool = False) -> Optional[Plan]:
    prompt = PLANNER_PROMPT.format(
        prev="sí" if has_prev_code else "no",
        message=message.strip()[:MAX_PLAN_MESSAGE_CHARS].replace('"', "'"),
    )
    try:
        # El plan es un JSON de pocas líneas: tope de salida chico (más rápido en una RTX 2050).
        planner = (provider.with_limits(settings.ollama_planner_num_predict)
                   if hasattr(provider, "with_limits") else provider)
        result = planner.chat([{"role": "user", "content": prompt}], allow_tools=False)
    except Exception:
        return None
    data = _extract_json(result.content)
    if data is None:
        return None
    plan = validate_plan(data, message, has_prev_code, followup_topic, last_is_code)
    return plan if plan.steps else None


# ---------------------------------------------------------------- ejecución de pasos

def _emit_verification(emit: EmitFn, step: str, args: dict, output: str) -> bool:
    tool = STEP_TO_TOOL[step]
    ok, verdict = verify_tool_result(tool, args, output)
    preview = output if len(output) <= 400 else output[:400] + "…"
    emit({"type": "verificacion", "intento": 1, "tool": tool, "arguments": args,
          "ok": ok, "detalle": verdict, "preview": preview})
    return ok


def _parse_images(output: str) -> list[dict]:
    """Saca {title,url} del texto de search_images. Sin duplicados, solo http(s)."""
    items, seen = [], set()
    title = ""
    for line in (output or "").splitlines():
        m_title = re.match(r"\s*\d+\.\s+(.*)$", line)
        if m_title and "URL de imagen" not in line:
            title = m_title.group(1).strip()
            continue
        m = IMAGE_URL_LINE_RE.search(line)
        if m:
            url = m.group(1).strip()
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                items.append({"title": title or "sin título", "url": url})
    return items


def _page_theme(code: str) -> str:
    """Tema de una página existente (su <title> o <h1>), para buscar 'tails Sonic the Hedgehog' y no solo 'tails'."""
    m = re.search(r"<title>(.*?)</title>", code or "", re.I | re.S) or re.search(r"<h1[^>]*>(.*?)</h1>", code or "", re.I | re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()[:60] if m else ""


def _step_item_images(state: dict, emit: EmitFn) -> dict:
    """Una imagen DISTINTA y que realmente baja por cada elemento de la lista del usuario."""
    from app.core import docedit   # import tardío: docedit importa este módulo
    items = state["items"]
    norm_items = {_norm(i) for i in items}
    theme = _page_theme(state.get("existing_code", ""))
    if not theme and state.get("topic") and _norm(state["topic"]) not in norm_items:
        theme = state["topic"]
    seen = {m.group(2) for m in IMG_SRC_RE.finditer(state.get("existing_code", "") or "")}   # no repetir las de la página
    found, missing, unconfirmed = [], [], []
    for it in items:
        emit({"type": "progreso", "mensaje": f"Buscando una imagen de: {it}..."})
        got = _pick_item_image(it, theme, seen, emit)
        if got:
            seen.add(got["url"])
            found.append({"title": it[:1].upper() + it[1:], "url": got["url"]})
            if not got.get("confirmed", True):
                unconfirmed.append(it)
        else:
            missing.append(it)
    warnings = [f"No encontré una imagen que se descargara para: {', '.join(missing)}."] if missing else []
    if unconfirmed:
        warnings.append("No pude confirmar que estas imágenes sean de lo pedido (su título/página no lo menciona): "
                        + ", ".join(unconfirmed) + ". Revísalas.")
    return {"images": found, "missing": missing, "line": f"{len(found)} de {len(items)} imágenes por elemento",
            "warnings": warnings, "ok": bool(found)}


_GENERIC_ITEM_WORDS = {"cosplay", "juego", "serie", "anime", "foto", "imagen", "personaje", "protagonista", "pelicula",
                       "persona", "chica", "mujer", "hombre", "fotos", "imagenes", "version", "oficial"}


def _pick_item_image(item: str, theme: str, seen: set, emit: EmitFn) -> Optional[dict]:
    """Una imagen que baja y que SÍ habla del elemento. Primero 'elemento + tema' (p. ej. 'tails Sonic the
    Hedgehog'); si ninguna menciona el elemento, 'elemento' solo (p. ej. 'cosplay de miku', sin 'Tijuana')."""
    from app.core import docedit
    toks = _tokens(item)
    distinct = [t for t in toks if t not in _GENERIC_ITEM_WORDS] or toks
    tries = []
    if theme and not set(_tokens(theme)) <= set(toks):
        tries.append(f"{item} {theme}")
    tries.append(item)
    best = None
    for q in tries:
        got = docedit._pick_working(q, item, 1, seen, emit, distinct)
        if got:
            if got[0].get("confirmed", True):
                return got[0]
            best = best or got[0]
    return best


def _step_images(state: dict, emit: EmitFn) -> dict:
    if state.get("items"):
        return _step_item_images(state, emit)
    topic, count = state["topic"], state["count"]
    query = state.get("image_query") or topic    # tema + calificador del pedido ("... cosplay"), si lo hay
    # "metele otras 5 fotos": las que la página ya tiene no cuentan (se piden de más y se descartan las repetidas)
    present = {m.group(2) for m in IMG_SRC_RE.finditer(state.get("existing_code", "") or "")}
    present |= {m.group(2) for m in CSS_BG_RE.finditer(state.get("existing_code", "") or "")}
    want = min(count + IMAGE_SPARE + len(present), MAX_COUNT_SEARCH)
    emit({"type": "progreso", "mensaje": f"Buscando imágenes de: {query}..."})
    args = {"query": query, "count": want}
    output = search_images(query=query, count=want)
    ok = _emit_verification(emit, "search_images", args, output)
    items = [it for it in (_parse_images(output) if ok else []) if it["url"] not in present]

    if not items:  # una sola reformulación determinística, no un reintento del modelo
        args = {"query": f"{query} images", "count": want}
        output = search_images(query=args["query"], count=want)
        ok = _emit_verification(emit, "search_images", args, output)
        items = [it for it in (_parse_images(output) if ok else []) if it["url"] not in present]

    warnings = []
    items = items[:count]
    if not items:
        warnings.append("No se pudieron conseguir imágenes reales; no se inventaron URLs.")
    elif len(items) < count:
        warnings.append(f"Se pidieron {count} imágenes y solo se encontraron {len(items)} distintas.")
    line = f"{len(items)} URLs reales" if items else "sin imágenes"
    return {"images": items, "line": line, "warnings": warnings, "ok": bool(items)}


def _step_research(state: dict, emit: EmitFn) -> dict:
    topic = state["topic"]
    args = {"topic": topic}
    result = run_research_pipeline(topic, emit=emit)
    if result.get("status") != "ok":
        output = f"Error en la investigación: {result.get('detail')}"
    else:
        output = (f"Investigación completada sobre '{result['topic']}'. "
                  f"Resumen guardado en memoria (id: {result['memory_id']}):\n{result['summary']}")
    ok = _emit_verification(emit, "research", args, output)
    if not ok:
        return {"research": None, "line": "investigación fallida",
                "warnings": [f"La investigación falló: {result.get('detail')}. Se siguió sin ella."], "ok": False}
    return {"research": {"summary": result["summary"], "sources": result.get("sources", [])},
            "line": "investigación lista", "warnings": [], "ok": True}


# ---------------------------------------------------------------- código: armado, reparación, continuación

def build_code_inputs(state: dict) -> tuple[str, str]:
    """spec + context para write_code, armados por código con el mensaje ORIGINAL literal."""
    message = state["message"]
    if state.get("existing_code"):
        spec = ("MODIFICACIÓN de un código existente. Hacé solo los cambios que pide el usuario y devolvé "
                f"el código COMPLETO actualizado.\nPedido literal del usuario:\n{message}")
    else:
        spec = f"Pedido literal del usuario (respetalo tal cual):\n{message}"
        if state.get("images"):
            spec += (f"\nUsá las imágenes reales del contexto (una casilla por URL distinta, "
                     f"hasta {state['count']}).")

    parts = [f"PEDIDO ORIGINAL DEL USUARIO (literal): {message}"]
    if state.get("topic"):
        parts.append(f"TEMA: {state['topic']}")
    images = state.get("images") or []
    if images and state.get("items"):
        lines = ["IMÁGENES POR ELEMENTO (cada URL es del elemento indicado: ponela en la tarjeta/sección de ESE "
                 "elemento, tal cual, sin cambiarla):"]
        lines += [f"- {it['title']}: {it['url']}" for it in images]
        if state.get("missing_items"):
            lines.append("Sin imagen real para: " + ", ".join(state["missing_items"]) +
                         ". Para esos usá un bloque de color CSS; NO inventes URLs.")
        parts.append("\n".join(lines))
    elif images:
        asked = state["count"] or len(images)
        lines = [f"IMÁGENES REALES ({asked} pedidas, {len(images)} encontradas). "
                 "Usá estas URLs tal cual, cada una es una imagen distinta:"]
        lines += [f"{i}. {it['url']}  ({it['title'][:60]})" for i, it in enumerate(images, 1)]
        if state.get("existing_code"):
            lines.append("Insertá la imagen con <img src=\"URL\" alt=\"...\"> dentro de la página, en el lugar "
                         "que corresponda (p. ej. junto al tema principal). NO agregues enlaces de texto ni "
                         "textos nuevos que el usuario no pidió.")
        parts.append("\n".join(lines))
    elif "search_images" in state["plan"].steps:
        parts.append("NO se consiguieron imágenes reales. NO uses URLs de imágenes; usá bloques de color CSS "
                     "como marcador de posición.")
    research = state.get("research")
    if research:
        parts.append("INVESTIGACIÓN (datos verificados, usalos):\n" + research["summary"])
    elif "research" in state["plan"].steps:
        parts.append("La investigación falló: no inventes datos concretos que no conozcas con certeza.")
    return spec, "\n\n".join(parts)


def repair_image_urls(text: str, real_urls: list[str]) -> tuple[str, int, int]:
    """
    Si el código usa una URL de imagen que NO viene de la búsqueda (placeholder, inventada),
    la cambia por una URL real todavía sin usar. Devuelve (texto, reemplazadas, sin_resolver).
    """
    if not real_urls:
        return text, 0, 0
    allowed = set(real_urls)
    used = {u for _, u in IMG_SRC_RE.findall(text) if u in allowed}
    used |= {u for _, u in CSS_BG_RE.findall(text) if u in allowed}
    spare = [u for u in real_urls if u not in used]
    counters = {"fixed": 0, "left": 0}

    def sub(m):
        url = m.group(2)
        if url in allowed:
            return m.group(0)
        if spare:
            counters["fixed"] += 1
            return m.group(1) + spare.pop(0)
        counters["left"] += 1
        return m.group(0)

    text = IMG_SRC_RE.sub(sub, text)
    text = CSS_BG_RE.sub(sub, text)
    return text, counters["fixed"], counters["left"]


_PLACEHOLDER_SVG = (b"<svg xmlns='http://www.w3.org/2000/svg' width='400' height='300' viewBox='0 0 400 300'>"
                    b"<rect width='400' height='300' fill='#2b3a55'/><circle cx='200' cy='130' r='46' fill='#3d5278'/>"
                    b"<rect x='120' y='200' width='160' height='16' rx='8' fill='#3d5278'/></svg>")
PLACEHOLDER_IMG = "data:image/svg+xml;base64," + base64.b64encode(_PLACEHOLDER_SVG).decode()
MAX_URL_CHECKS = 8


def neutralize_dead_images(text: str, real_urls: list[str]) -> tuple[str, int]:
    """Las URLs de imagen que NO vienen de la búsqueda se comprueban: si no bajan (el modelo las inventó), se
    cambian por un marcador gris en vez de dejar una imagen rota. -> (texto, reemplazadas)"""
    from app.core.documents import fetch_image
    allowed, checked, count = set(real_urls or []), {}, [0]

    def alive(url: str) -> bool:
        if url in allowed:
            return True
        if url not in checked:
            if len(checked) >= MAX_URL_CHECKS:
                return True                      # tope de comprobaciones: lo demás queda como está
            try:
                checked[url] = bool(fetch_image(url))
            except Exception:
                checked[url] = True              # ante la duda, no tocar
        return checked[url]

    def sub(m):
        if alive(m.group(2)):
            return m.group(0)
        count[0] += 1
        return m.group(1) + PLACEHOLDER_IMG

    text = IMG_SRC_RE.sub(sub, text)
    text = CSS_BG_RE.sub(sub, text)
    return text, count[0]


def looks_truncated(text: str, flag: bool) -> bool:
    if flag or TRUNC_MARKER in text or text.count("```") % 2 == 1:
        return True
    for _, body in _blocks(text):
        if re.match(r"\s*(<!doctype html|<html)", body, re.I) and "</html>" not in body.lower():
            return True
    return False


def _merge_continuation(head: str, cont: str) -> str:
    if head.endswith("\n"):
        cont = cont.lstrip("\n")
    for k in range(min(len(head), len(cont), 400), 19, -1):  # quita el solapamiento si el modelo repitió el final
        if head.endswith(cont[:k]):
            return head + cont[k:]
    return head + cont


def continue_code(text: str) -> tuple[str, bool]:
    """Pide UNA sola continuación y la pega. Devuelve (texto, se_pudo)."""
    text = text.replace(TRUNC_MARKER, "").rstrip()
    if text.count("```") % 2 == 1:
        text += "\n```"
    main = _longest_block(text)
    if not main:
        return text, False
    res = run_code_generation(
        "CONTINUACIÓN: el código cuyo final se muestra abajo se cortó por límite de longitud. "
        "Devolvé SOLO lo que falta, empezando exactamente donde termina ese final. No repitas nada "
        "de lo anterior, no expliques nada y cerrá todas las etiquetas y llaves que queden abiertas.",
        context="", existing_code=main[-CONTINUATION_TAIL_CHARS:])
    if res.get("status") != "ok":
        return text, False
    cont = _longest_block(res["code"]) or res["code"].strip()
    if not cont.strip():
        return text, False
    merged = _merge_continuation(main, cont)
    return text.replace(main, merged, 1), True


def _step_code(state: dict, emit: EmitFn) -> dict:
    spec, context = build_code_inputs(state)
    existing = state.get("existing_code", "")
    args = {"spec": spec, "context": context, "existing_code": existing}
    warnings: list[str] = []

    # Edición de código existente: primero por PARCHES (el modelo devuelve solo lo que cambia y el código
    # lo aplica). Si no hay parches válidos o alguno no calza, se cae a la regeneración completa de siempre.
    result = None
    patches_applied = 0
    if existing:
        emit({"type": "progreso", "mensaje": "Editando el código: aplicando solo los cambios pedidos..."})
        pr = run_code_patch(f"Pedido literal del usuario:\n{state['message']}", existing, context)
        if pr.get("status") == "ok":
            lang = state.get("existing_lang", "")
            result = {"status": "ok", "code": f"```{lang}\n{pr['code'].strip()}\n```",
                      "provider": pr["provider"], "truncated": False}
            patches_applied = pr["patches"]
        else:
            emit({"type": "progreso", "mensaje": f"Los parches no sirvieron ({pr.get('detail')}); regenero el código completo..."})

    if result is None:
        emit({"type": "progreso", "mensaje": "Generando el código con el modelo de código..."})
        result = run_code_generation(spec, context=context, existing_code=existing)
        if result.get("status") != "ok":  # un solo reintento ante error explícito
            result = run_code_generation(spec, context=context, existing_code=existing)

    if result.get("status") != "ok":
        output = f"Error generando código: {result.get('detail')}"
        _emit_verification(emit, "write_code", args, output)
        return {"code": "", "line": "código fallido", "ok": False, "truncated": False,
                "warnings": [f"No se pudo generar el código: {result.get('detail')}"]}

    text = result["code"]
    output = f"Código generado (modelo: {result['provider']}).\n{text}"
    _emit_verification(emit, "write_code", args, output)

    truncated = False
    if not patches_applied and looks_truncated(text, result.get("truncated", False)):
        emit({"type": "progreso", "mensaje": "El código llegó cortado, pidiendo la continuación..."})
        text, done = continue_code(text)
        if not done or looks_truncated(text, False):
            truncated = True
            warnings.append("El código puede estar incompleto (se cortó por longitud y no se pudo completar).")

    real_urls = [it["url"] for it in state.get("images") or []]
    if state.get("items"):      # una imagen por elemento: una URL inventada NO se "repara" con la foto de otro personaje
        fixed, left = 0, 0
    else:
        text, fixed, left = repair_image_urls(text, real_urls)
    if fixed:
        warnings.append(f"{fixed} URL(s) de imagen del código no venían de la búsqueda y se reemplazaron por URLs reales.")
    if left:
        text, dead = neutralize_dead_images(text, real_urls)
        if dead:
            warnings.append(f"{dead} imagen(es) del código apuntaban a una URL inventada que no existe; las cambié por un "
                            "marcador gris (pídeme la imagen concreta si la quieres).")
        if left - dead > 0:
            warnings.append(f"{left - dead} imagen(es) del código usan una URL que no viene de la búsqueda, pero sí descarga.")
    elif not real_urls or state.get("items"):   # sin búsqueda de imágenes (o por elemento): igual se revisan las URLs que el modelo escribió
        text, dead = neutralize_dead_images(text, real_urls)
        if dead:
            warnings.append(f"{dead} imagen(es) del código apuntaban a una URL inventada que no existe; las cambié por un "
                            "marcador gris (pídeme la imagen concreta si la quieres).")

    if state.get("existing_code") and real_urls and not any(u in text for u in real_urls):
        img = (f'<img src="{real_urls[0]}" alt="{(state.get("topic") or "imagen")[:60]}" '
               'style="max-width:100%;height:auto;display:block;margin:1rem auto;border-radius:8px">')
        for anchor in ("</h1>", "</header>", "<body>"):
            if anchor in text:
                text = text.replace(anchor, anchor + "\n  " + img, 1)
                warnings.append("El modelo no insertó la imagen; la puse yo en la página.")
                break

    lines = sum(len(b.splitlines()) for _, b in _blocks(text)) or len(text.splitlines())
    if not truncated:
        _persist_code(text)
    note = f" (editado con {patches_applied} parche(s), sin regenerar todo)" if patches_applied else ""
    return {"code": text.strip(), "line": f"código de {lines} líneas{note}", "ok": True,
            "truncated": truncated, "warnings": warnings}


# ---------------------------------------------------------------- cierre

@dataclass
class PipelineOutcome:
    reply: str
    tools_called: list[str]
    ok: bool
    truncated: bool
    summary: str
    history_note: str = ""            # si no está vacío, es lo que se guarda en el historial en vez de `reply`
    replace_previous: bool = False    # la continuación trae el texto completo y el cliente debe reemplazar
    from_continuation: bool = False


def _template_intro(state: dict, ok_flags: dict) -> str:
    done = []
    if ok_flags.get("research"):
        done.append("investigué el tema")
    if ok_flags.get("search_images"):
        done.append(f"encontré {len(state['images'])} imágenes reales")
    if ok_flags.get("write_code"):
        done.append("armé la modificación del código" if state.get("existing_code") else "armé el código")
    if not done:
        return "No pude completar el pedido."
    return "Listo: " + ", ".join(done) + "."


def _model_intro(provider, message: str, lines: list[str]) -> Optional[str]:
    prompt = ("Escribí 1 o 2 frases cortas avisándole al usuario qué se hizo con su pedido. "
              "NO incluyas código, URLs ni listas: el sistema los pega aparte.\n"
              f"Pedido: {message[:300]}\nResultado de los pasos: {'; '.join(lines)}\nFrases:")
    try:
        text = strip_think_blocks(provider.chat([{"role": "user", "content": prompt}], allow_tools=False).content)
    except Exception:
        return None
    if not text or len(text) > 400 or "```" in text or "http" in text:
        return None
    return text.strip()


def compose_reply(state: dict, ok_flags: dict, warnings: list[str], provider=None) -> str:
    intro = None
    if settings.pipeline_close_with_model and provider is not None and any(ok_flags.values()):
        intro = _model_intro(provider, state["message"], state["lines"])
    parts = [intro or _template_intro(state, ok_flags)]

    if state.get("research"):
        block = "Resumen de la investigación:\n" + state["research"]["summary"]
        sources = [s for s in state["research"].get("sources", []) if s][:5]
        if sources:
            block += "\nFuentes:\n" + "\n".join(f"- {s}" for s in sources)
        parts.append(block)

    if state.get("code"):
        parts.append(state["code"])        # tal cual lo devolvió el modelo de código (ya reparado)
    elif state.get("images"):
        parts.append("Imágenes:\n" + "\n".join(
            f"{i}. {it['title']}\n   {it['url']}" for i, it in enumerate(state["images"], 1)))

    if warnings:
        parts.append("Notas:\n" + "\n".join(f"- {w}" for w in warnings))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- orquestación

def execute_plan(plan: Plan, message: str, history: list[dict], emit: EmitFn, provider=None) -> PipelineOutcome:
    state: dict = {
        "message": message,                  # SIEMPRE el literal del usuario, nunca el tema del 4B
        "plan": plan, "topic": plan.topic, "count": plan.count,
        "images": [], "research": None, "code": "", "lines": [],
        "existing_code": "", "existing_lang": "",
        "items": plan.items, "missing_items": [],
    }
    if plan.modify:
        state["existing_lang"], state["existing_code"] = find_previous_block(history)
        if "search_images" not in plan.steps:
            urls = recent_image_urls(history, state["existing_code"])
            if urls:
                state["images"] = [{"title": "imagen de la conversación", "url": u} for u in urls]
                state["images_from_history"] = True
    warnings: list[str] = []
    ok_flags: dict[str, bool] = {}
    tools_called: list[str] = []
    truncated = False

    def merge(name: str, res: dict):
        ok_flags[name] = res["ok"]
        state["lines"].append(res["line"])
        warnings.extend(res["warnings"])
        tools_called.append(STEP_TO_TOOL[name])

    # a) pasos independientes en paralelo
    parallel = [s for s in ("research", "search_images") if s in plan.steps]
    fns = {"research": _step_research, "search_images": _step_images}
    if parallel:
        ex = ThreadPoolExecutor(max_workers=len(parallel))
        futures = {name: ex.submit(fns[name], state, emit) for name in parallel}
        timeout = settings.pipeline_step_timeout_seconds
        for name in parallel:
            try:
                res = futures[name].result(timeout=timeout)
            except FutureTimeout:
                res = {"ok": False, "line": f"{name} sin respuesta", "warnings": [f"El paso {name} tardó demasiado y se omitió."]}
            except Exception as e:
                res = {"ok": False, "line": f"{name} fallido", "warnings": [f"El paso {name} falló: {e}"]}
            if name == "search_images":
                state["images"] = res.get("images", [])
                state["missing_items"] = res.get("missing", [])
            else:
                state["research"] = res.get("research")
            merge(name, res)
        ex.shutdown(wait=False, cancel_futures=True)

    # b) write_code espera a los dos
    if "write_code" in plan.steps:
        try:
            res = _step_code(state, emit)
        except Exception as e:
            res = {"ok": False, "line": "código fallido", "code": "", "truncated": False,
                   "warnings": [f"Error interno generando código: {e}"]}
        state["code"] = res.get("code", "")
        truncated = res.get("truncated", False)
        merge("write_code", res)

    reply = compose_reply(state, ok_flags, warnings, provider)
    summary = " + ".join(plan.steps) + " | " + ", ".join(state["lines"])
    return PipelineOutcome(reply=reply, tools_called=tools_called, ok=any(ok_flags.values()),
                           truncated=truncated, summary=summary)


def try_run(provider, message: str, history: list[dict], emit: EmitFn) -> Optional[PipelineOutcome]:
    """
    Punto de entrada para core.py. Devuelve None si NO corresponde usar el pipeline
    (deshabilitado, adjuntos, charla normal, JSON inválido o plan vacío): en ese caso
    core.py sigue con el Agent Loop de siempre.
    """
    if not settings.pipeline_enabled or message.startswith("Archivo adjunto:"):
        return None
    message = typos.fix_typos(message)      # "2 ftoos" -> "2 fotos": solo para enrutar; el historial guarda el original
    try:   # ¿pide cambiar un documento que ya se generó? -> se edita su fuente, no se regenera (docedit.py)
        from app.core import docedit
        edited = docedit.try_edit_doc(provider, message, history, emit)
        if edited is not None:
            return edited
    except Exception:
        logging.getLogger("elipse").exception("docedit falló")
    if DOC_PATTERN.search(message):
        from app.core import docpipeline   # pedidos de PDF/Word: pipeline de documentos (docpipeline.py)
        return docpipeline.try_run_doc(provider, message, history, emit)
    try:
        from app.core import docedit as _de
        last_was_doc = _de.last_artifact(history) == "doc"
    except Exception:
        last_was_doc = False
    try:
        # Si lo último que entregué fue un PDF/Word, el código viejo guardado en disco
        # no es "el código anterior" de esta conversación.
        prev_code = "" if last_was_doc else find_previous_code(history)
        if not _should_plan(message, bool(prev_code)):
            return None
        last_is_code, follow_topic = followup_context(history, prev_code)
        emit({"type": "progreso", "mensaje": "Planificando los pasos..."})
        plan = build_plan(provider, message, bool(prev_code), follow_topic, last_is_code)
    except Exception:
        logging.getLogger("elipse").exception("planificación falló")
        return None

    if plan is None:
        emit({"type": "progreso", "mensaje": "Sin plan de pipeline, sigo con el modo normal..."})
        return None

    emit({"type": "plan", "intento": 1, "pipeline": True, "notas": plan.notes, "pasos": [
        {"tool": STEP_TO_TOOL[s], "arguments": (
            {"topic": plan.topic} if s == "research"
            else {"query": plan.topic, "count": plan.count} if s == "search_images"
            else {"modificar": plan.modify})}
        for s in plan.steps]})

    try:
        outcome = execute_plan(plan, message, history, emit, provider)
    except Exception as e:
        outcome = PipelineOutcome(reply=f"Error ejecutando el pipeline: {e}", tools_called=[],
                                  ok=False, truncated=False, summary=f"error: {e}")
    emit({"type": "chunk", "texto": outcome.reply})
    return outcome