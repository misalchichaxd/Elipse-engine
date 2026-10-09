"""
Prueba rápida del OCR y del tipo de imagen, SIN modelo de visión ni servidor.
Uso (desde la carpeta que contiene la carpeta app/):
    python app/probar_ocr.py ruta\\captura.png [zona]
Ejemplos de zona: abajo, arriba-izquierda, tercio-2, 0.5,0,1,0.5
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import ocr, vision  # noqa: E402

if len(sys.argv) < 2:
    sys.exit(__doc__)

raw = Path(sys.argv[1]).read_bytes()
if len(sys.argv) > 2:
    raw, zona = vision.crop_region(raw, sys.argv[2])
    print(f"Zona: {zona}")

res = ocr.run(raw)
print(f"Motor OCR : {ocr.status()}")
if res.error:
    sys.exit(f"ERROR     : {res.error}")
img = vision.open_rgb(raw)
kind = vision.decide_kind("", Path(sys.argv[1]).name, vision.image_stats(img),
                          {"lines": res.lines, "coverage": res.coverage})
print(f"Tamaño    : {img.size[0]}x{img.size[1]}  |  tipo detectado: {kind}")
print(f"Líneas    : {res.lines}  |  confianza media: {res.confidence:.2f}  |  texto cubre: {res.coverage:.0%}")
print("---- texto ----")
print(res.text or "(no encontró texto)")
