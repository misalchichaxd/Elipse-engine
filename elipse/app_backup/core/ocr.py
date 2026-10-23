"""
OCR para ELIPSE: lee el texto de una imagen en la CPU, sin gastar VRAM.

Por qué existe: un modelo de visión de 2B-4B es flojo transcribiendo (se salta líneas, cambia
números, se corta a los 450 tokens). Un motor de OCR de verdad es más fiable, no compite por la
GPU con el modelo principal y deja al modelo de visión dedicarse a ENTENDER la imagen.

Motores soportados (se elige con OCR_ENGINE en el .env):
  - "rapidocr"  : pip install rapidocr-onnxruntime   (o el paquete nuevo "rapidocr" + onnxruntime)
                  Muy bueno con capturas de pantalla e interfaces; no necesita instalar nada más.
                  Ojo: con su modelo por defecto puede perder tildes (lee "Vision" por "Visión").
  - "tesseract" : pip install pytesseract + instalar Tesseract en el sistema con el idioma 'spa'
                  (Windows: instalador de UB-Mannheim). Mejor con tildes y documentos impresos.
  - "auto"      : (defecto) prueba rapidocr y, si no está, tesseract.
  - "off"       : sin OCR (ELIPSE vuelve a depender solo del modelo de visión).

Nunca lanza excepciones hacia afuera: si algo falla devuelve un OcrResult con 'error'.
"""

import io
import logging
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger("elipse")


@dataclass
class OcrResult:
    text: str = ""
    engine: str = ""
    lines: int = 0
    coverage: float = 0.0      # fracción del área de la imagen cubierta por cajas de texto (0-1)
    confidence: float = 0.0    # confianza media (0-1)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


_lock = threading.Lock()        # resolución del motor (una vez)
_run_lock = threading.Lock()    # un OCR a la vez: usa CPU y los motores no son thread-safe
_resolved = False
_engine = ""
_reasons: list = []   # por que fallo cada motor (para el aviso de arranque)
_rapid = None
_tess_langs = ""


# --------------------------------------------------------------------------- motores

def _init_rapid() -> bool:
    """Prueba el paquete nuevo ('rapidocr') y el anterior ('rapidocr_onnxruntime')."""
    global _rapid
    for module in ("rapidocr", "rapidocr_onnxruntime"):
        try:
            mod = __import__(module, fromlist=["RapidOCR"])
            _rapid = mod.RapidOCR()
            log.info("OCR: usando %s", module)
            return True
        except Exception as e:  # no instalado, o no pudo bajar/cargar sus modelos
            log.info("OCR: %s no disponible (%s: %s)", module, type(e).__name__, e)
            _reasons.append(f"{module}: {type(e).__name__}: {e}")
    return False


def _tess_cmd() -> Optional[str]:
    cmd = (settings.tesseract_cmd or "").strip() or shutil.which("tesseract")
    if not cmd:
        for candidate in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                          r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if Path(candidate).exists():
                return candidate
    return cmd


def _init_tess() -> bool:
    global _tess_langs
    try:
        import pytesseract
        cmd = _tess_cmd()
        if not cmd:
            raise RuntimeError("no encontré el ejecutable de Tesseract (¿instalado? ¿TESSERACT_CMD?)")
        pytesseract.pytesseract.tesseract_cmd = cmd
        installed = set(pytesseract.get_languages(config=""))
        wanted = [l for l in re.split(r"[+,\s]+", settings.ocr_langs or "spa+eng") if l]
        usable = [l for l in wanted if l in installed] or (["eng"] if "eng" in installed else [])
        if not usable:
            raise RuntimeError(f"Tesseract no tiene los idiomas pedidos ({settings.ocr_langs}); instalados: {sorted(installed)}")
        _tess_langs = "+".join(usable)
        log.info("OCR: usando tesseract (%s)", _tess_langs)
        return True
    except Exception as e:
        log.info("OCR: tesseract no disponible (%s: %s)", type(e).__name__, e)
        _reasons.append(f"tesseract: {type(e).__name__}: {e}")
        return False


def engine_name() -> str:
    """Motor que se va a usar ('rapidocr' | 'tesseract'), o '' si no hay ninguno. Se resuelve una vez."""
    global _resolved, _engine
    if _resolved:
        return _engine
    with _lock:
        if _resolved:
            return _engine
        pref = (settings.ocr_engine or "auto").strip().lower()
        if pref in ("off", "none", "false", "0", "no"):
            order = []
        elif pref == "rapidocr":
            order = ["rapidocr"]
        elif pref == "tesseract":
            order = ["tesseract"]
        else:
            order = ["rapidocr", "tesseract"]
        for name in order:
            if (name == "rapidocr" and _init_rapid()) or (name == "tesseract" and _init_tess()):
                _engine = name
                break
        _resolved = True
        if not _engine and order:
            log.warning(
                "OCR: no hay ningun motor disponible. Python del servidor: %s (%s). Motivos: %s. "
                "Instala con ESE Python: \"%s\" -m pip install rapidocr-onnxruntime",
                sys.version.split()[0], sys.executable, " | ".join(_reasons) or "OCR_ENGINE=off",
                sys.executable,
            )
        return _engine


def available() -> bool:
    return bool(engine_name())


def status() -> str:
    eng = engine_name()
    if eng == "tesseract":
        return f"tesseract ({_tess_langs})"
    return eng or "sin OCR"


# --------------------------------------------------------------------------- imagen

def _load(raw: bytes):
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        alpha = img.split()[-1]
        if alpha.getextrema()[0] < 255:  # hay transparencia real: se pone sobre blanco
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=alpha)
            return bg
    return img.convert("RGB")


def _scale(img):
    """Reduce lo muy grande (lento) y agranda lo muy chico (los recortes de zoom)."""
    from PIL import Image
    w, h = img.size
    side = max(w, h)
    cap = max(int(settings.ocr_max_side), 600)
    if side > cap:
        k = cap / side
        return img.resize((max(int(w * k), 1), max(int(h * k), 1)), Image.LANCZOS)
    if side < 700:
        k = min(3.0, 900 / max(side, 1))
        return img.resize((max(int(w * k), 1), max(int(h * k), 1)), Image.LANCZOS)
    return img


def _is_dark(img) -> bool:
    small = img.convert("L")
    small.thumbnail((64, 64))
    px = list(small.getdata())
    return (sum(px) / max(len(px), 1)) < 110


# --------------------------------------------------------------------------- ejecución

def _run_rapid(img) -> list:
    import numpy as np
    out = _rapid(np.array(img))
    lines = []
    if isinstance(out, tuple):                    # rapidocr_onnxruntime: (resultado, tiempos)
        for item in (out[0] or []):
            box, text, score = item[0], item[1], item[2]
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            lines.append({"text": str(text), "conf": float(score),
                          "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)})
    else:                                         # rapidocr >= 2: objeto con boxes/txts/scores
        boxes = getattr(out, "boxes", None)
        txts = getattr(out, "txts", None)
        scores = getattr(out, "scores", None)
        if boxes is not None and txts is not None:
            for box, text, score in zip(boxes, txts, scores):
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                lines.append({"text": str(text), "conf": float(score),
                              "x0": float(min(xs)), "y0": float(min(ys)),
                              "x1": float(max(xs)), "y1": float(max(ys))})
    return [l for l in lines if l["conf"] >= float(settings.ocr_min_confidence) and l["text"].strip()]


def _run_tess(img) -> list:
    import pytesseract
    from PIL import ImageOps
    proc = img.convert("L")
    if _is_dark(img):                             # Tesseract espera texto oscuro sobre fondo claro
        proc = ImageOps.invert(proc)
    data = pytesseract.image_to_data(proc, lang=_tess_langs, config="--oem 1 --psm 3",
                                     output_type=pytesseract.Output.DICT)
    grouped = {}
    order = []
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in grouped:
            grouped[key] = {"words": [], "confs": [], "x0": 1e9, "y0": 1e9, "x1": 0, "y1": 0}
            order.append(key)
        g = grouped[key]
        g["words"].append(word)
        g["confs"].append(conf / 100.0)
        left, top, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        g["x0"], g["y0"] = min(g["x0"], left), min(g["y0"], top)
        g["x1"], g["y1"] = max(g["x1"], left + w), max(g["y1"], top + h)
    lines = []
    for key in order:  # Tesseract ya los entrega en orden de lectura (bloque, párrafo, línea)
        g = grouped[key]
        conf = sum(g["confs"]) / len(g["confs"])
        if conf >= min(float(settings.ocr_min_confidence), 0.4):
            lines.append({"text": " ".join(g["words"]), "conf": conf,
                          "x0": g["x0"], "y0": g["y0"], "x1": g["x1"], "y1": g["y1"]})
    return lines


def _rows_to_text(lines: list) -> str:
    """Ordena cajas sueltas (RapidOCR) en filas de lectura: arriba-abajo y, dentro de la fila, izquierda-derecha."""
    if not lines:
        return ""
    heights = sorted(max(l["y1"] - l["y0"], 1) for l in lines)
    med_h = heights[len(heights) // 2]
    ordered = sorted(lines, key=lambda l: (l["y0"] + l["y1"]) / 2)
    rows, cur = [], []
    for l in ordered:
        cy = (l["y0"] + l["y1"]) / 2
        if cur:
            row_cy = sum((x["y0"] + x["y1"]) / 2 for x in cur) / len(cur)
            if abs(cy - row_cy) > 0.6 * med_h:
                rows.append(cur)
                cur = []
        cur.append(l)
    if cur:
        rows.append(cur)
    out = []
    for row in rows:
        row.sort(key=lambda l: l["x0"])
        text = row[0]["text"]
        for prev, l in zip(row, row[1:]):
            text += ("   " if (l["x0"] - prev["x1"]) > 2.5 * med_h else " ") + l["text"]
        out.append(text)
    return "\n".join(out)


def _clean(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def run(raw: bytes) -> OcrResult:
    """OCR de una imagen (bytes). Nunca lanza: ante un fallo devuelve OcrResult(error=...)."""
    eng = engine_name()
    if not eng:
        return OcrResult(error="no hay un motor de OCR instalado (pip install rapidocr-onnxruntime)")
    try:
        img = _scale(_load(raw))
    except Exception as e:
        return OcrResult(engine=eng, error=f"no pude abrir la imagen para el OCR ({type(e).__name__})")
    try:
        with _run_lock:
            lines = _run_rapid(img) if eng == "rapidocr" else _run_tess(img)
    except Exception as e:
        log.warning("OCR (%s) falló: %s: %s", eng, type(e).__name__, e)
        return OcrResult(engine=eng, error=f"el OCR falló ({type(e).__name__}: {e})")

    if not lines:
        return OcrResult(engine=eng)
    area = float(img.size[0] * img.size[1]) or 1.0
    covered = sum(max(l["x1"] - l["x0"], 0) * max(l["y1"] - l["y0"], 0) for l in lines)
    text = _rows_to_text(lines) if eng == "rapidocr" else "\n".join(l["text"] for l in lines)
    return OcrResult(
        text=_clean(text), engine=eng, lines=len(lines),
        coverage=min(covered / area, 1.0),
        confidence=sum(l["conf"] for l in lines) / len(lines),
    )


# --------------------------------------------------------------------------- paginado

def paginate(text: str, page: int, size: int) -> tuple[str, int]:
    """Parte el texto en páginas de ~size caracteres cortando en saltos de línea.
    Devuelve (texto de la página pedida, total de páginas). 'page' empieza en 1."""
    size = max(int(size), 200)
    pages, cur, cur_len = [], [], 0
    for line in text.split("\n"):
        while len(line) > size:                        # una línea gigante se parte a la fuerza
            if cur:
                pages.append("\n".join(cur))
                cur, cur_len = [], 0
            pages.append(line[:size])
            line = line[size:]
        if cur and cur_len + len(line) + 1 > size:
            pages.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        pages.append("\n".join(cur))
    pages = pages or [""]
    page = min(max(int(page), 1), len(pages))
    return pages[page - 1], len(pages)