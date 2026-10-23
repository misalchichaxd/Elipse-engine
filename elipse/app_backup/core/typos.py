"""
Corrector de typos SOLO para las palabras que deciden el ruteo (foto, imagen, investigación, informe, página...).

Por qué: los patrones del pipeline (IMAGE_PATTERN, DOC_PATTERN...) son exactos. "2 ftoos" no coincidía con "fotos",
el pedido de imágenes se perdía en silencio y la investigación salía sin las 2 fotos. Los modelos chicos no suplen eso.

Seguridad: solo se corrige a una palabra del vocabulario si está a UNA edición (letras intercambiadas, letra de
más/de menos, o una sustitución por una tecla vecina del teclado) y empieza con la misma letra. Palabras reales
parecidas ("foros", "informa", "fondo") se respetan. Solo afecta al enrutamiento: el historial guarda el texto
original del usuario.
"""

import re
import unicodedata

VOCAB = {
    "foto", "fotos", "imagen", "imagenes", "investigacion", "investiga", "informe", "reporte",
    "documento", "pagina", "codigo", "galeria", "resumen", "seccion", "personajes",
}
KNOWN_OK = {"foro", "foros", "fono", "fonos", "fondo", "fondos", "forma", "formas", "moto", "motos", "informa",
            "informes", "codigos", "paginas", "documentos", "fotografia", "fotografias", "reportes", "secciones"}
_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
_POS = {c: (r, i) for r, row in enumerate(_ROWS) for i, c in enumerate(row)}
_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜáéíóúüñÑ]{4,}")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def _adjacent(a: str, b: str) -> bool:
    if a not in _POS or b not in _POS:
        return False
    (r1, c1), (r2, c2) = _POS[a], _POS[b]
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1


def _one_edit(typo: str, word: str) -> bool:
    if typo == word or typo[0] != word[0] or abs(len(typo) - len(word)) > 1:
        return False
    if len(typo) == len(word):
        diff = [i for i in range(len(typo)) if typo[i] != word[i]]
        if len(diff) == 1:                                   # sustitución: solo si la tecla es vecina
            return _adjacent(typo[diff[0]], word[diff[0]])
        if len(diff) == 2 and diff[1] == diff[0] + 1:        # dos letras intercambiadas (ftoos -> fotos)
            i = diff[0]
            return typo[i] == word[i + 1] and typo[i + 1] == word[i]
        return False
    short, long_ = (typo, word) if len(typo) < len(word) else (word, typo)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))     # letra de más o de menos


def _fix_word(token: str) -> str:
    n = _norm(token)
    if n in VOCAB or n in KNOWN_OK:
        return token
    for w in VOCAB:
        if _one_edit(n, w):
            return w.capitalize() if token[:1].isupper() else w
    return token


def fix_typos(message: str) -> str:
    return _WORD_RE.sub(lambda m: _fix_word(m.group(0)), message or "")