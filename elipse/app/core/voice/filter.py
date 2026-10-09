"""
Filtro "hablable": del Markdown de la respuesta deja solo lo que tiene sentido decir en voz alta.
Determinista (regex), sin llamadas al modelo. Quita código, tablas, listas largas, URLs, rutas,
hashes, emojis y marcas de Markdown, y recorta en fin de oración.
"""

import re

_FENCE = re.compile(r"```.*?```", re.S)
_OPEN_FENCE = re.compile(r"```.*\Z", re.S)               # bloque sin cerrar (respuesta cortada)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_WIN_PATH = re.compile(r"\b[A-Za-z]:[\\/]\S*")
_UNIX_PATH = re.compile(r"(?:(?<!\w)\.{0,2}/(?:[\w.\-]+/)*[\w.\-]+)|(?:\b(?:[\w.\-]+/){2,}[\w.\-]*)|(?:\b\w+/[\w\-]+\.\w{1,5}\b)")
_FILE = re.compile(r"\b[\w\-]+\.(?:py|js|jsx|ts|tsx|html|css|json|md|txt|pdf|docx|csv|ya?ml|sh|sql|png|jpe?g|log|env)\b", re.I)
_HASH = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\uFE0F\u200d]")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*")
_SENT_END = re.compile(r"(?<=[.!?…])\s+")

MAX_LIST_ITEMS_SPOKEN = 3      # listas más largas se saltan
LONG_FACTOR = 2                # texto hablable > 2×max_chars = "documento": solo 2-3 oraciones
LONG_MAX_SENTENCES = 3


def _inline_code(m: re.Match) -> str:
    inner = m.group(1).strip()
    # `foo` suelto se lee; `read_file()`, `a/b.py`, `x = 1` no tiene sentido hablarlos
    return inner if re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]{1,30}", inner) else ""


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") or s.count("|") >= 2 or bool(re.fullmatch(r"[\s:\-|+]{3,}", s) and "-" in s)


def _ends_sentence(s: str) -> bool:
    return s.rstrip()[-1:] in ".!?…:;"


def clean(text: str) -> str:
    """Markdown → texto plano hablable (sin recortar)."""
    text = (text or "").replace("\r\n", "\n")
    text = _FENCE.sub("\n", text)
    text = _OPEN_FENCE.sub("\n", text)
    text = _INLINE_CODE.sub(_inline_code, text)
    text = _LINK.sub(lambda m: m.group(1), text)
    for rx in (_URL, _WIN_PATH, _UNIX_PATH, _FILE, _HASH, _EMOJI):
        text = rx.sub("", text)

    out: list[str] = []
    run: list[str] = []                       # ítems de lista consecutivos

    def flush_run():
        if 0 < len(run) <= MAX_LIST_ITEMS_SPOKEN:
            out.extend(i if _ends_sentence(i) else i + "." for i in run)
        run.clear()

    for line in text.split("\n"):
        if _is_table_line(line):
            continue
        if _LIST_ITEM.match(line):
            item = _LIST_ITEM.sub("", line).strip()
            if item:
                run.append(item)
            continue
        flush_run()
        line = _HEADING.sub("", line).lstrip("> ").strip()
        if line:
            out.append(line if _ends_sentence(line) or len(line) > 80 else line + ".")
    flush_run()

    s = " ".join(out)
    s = re.sub(r"\*\*|__|~~", "", s)
    s = re.sub(r"(?<!\w)[*_]|[*_](?!\w)", "", s)           # énfasis, sin tocar snake_case
    s = re.sub(r"\(\s*\)|\[\s*\]", "", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    s = re.sub(r"([.!?])\1+", r"\1", s)
    return re.sub(r"\s+", " ", s).strip(" -–—:;,")


def truncate(text: str, max_chars: int, max_sentences: int | None = None) -> str:
    """Recorta en fin de oración sin pasar de max_chars (y de max_sentences, si se da)."""
    out, total = [], 0
    for sent in _SENT_END.split(text):
        if max_sentences is not None and len(out) >= max_sentences:
            break
        add = len(sent) + (1 if out else 0)
        if total + add > max_chars:
            if not out:                                    # una sola oración enorme: corta en palabra
                cut = sent[:max_chars].rsplit(" ", 1)[0].rstrip(",;:")
                out.append(cut + ".")
            break
        out.append(sent)
        total += add
    return " ".join(out)


def analyze(text: str, max_chars: int) -> dict:
    """
    full:   texto hablable completo (sin recortar)
    spoken: lo que se diría (recortado; 2-3 oraciones si es un documento largo)
    ratio:  fracción del original que es hablable (bajo = mayormente código/tabla/documento)
    long:   True si el hablable es de tamaño documento
    """
    original = (text or "").strip()
    full = clean(original)
    is_long = len(full) > LONG_FACTOR * max_chars
    spoken = truncate(full, max_chars, LONG_MAX_SENTENCES if is_long else None)
    return {
        "full": full,
        "spoken": spoken,
        "ratio": len(full) / max(1, len(original)),
        "long": is_long,
    }
