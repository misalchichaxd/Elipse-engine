"""
Fase 0 — Foto del comportamiento de ELIPSE (perfil por defecto).

Lo comparten make_golden.py (que guarda la foto con el código ACTUAL) y
test_golden_default.py (que la recalcula con el código modificado y exige igualdad exacta).

Trabaja siempre sobre una COPIA de elipse.db y con MCP apagado. Importante: este archivo NO
importa nada de `app` al cargarse; prepare_env() fija el entorno y recién entonces se importa.
Ejecutar siempre desde la raíz del proyecto (donde están app/ y .env).
"""

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_FILE = Path(__file__).resolve().parent / "golden_default.json"

# Mensajes de muestra: cubren código, imágenes, investigación, documentos, saludo, URL,
# ejecución, visión/OCR, memoria, archivos y modificación de código previo.
SAMPLES = [
    "hola, cómo estás?",
    "hazme una página html con un botón que cambie de color",
    "busca imágenes de gatos y arma una galería",
    "investiga cómo funciona la fotosíntesis",
    "escribe un informe en pdf sobre el cambio climático",
    "lee https://example.com y resúmelo",
    "ejecuta este código python: print(2+2)",
    "cuánto es 15% de 340?",
    "qué hora es?",
    "recuerda que me gusta el café sin azúcar",
    "qué sabes de mí? te conté algo ayer",
    "lista los archivos de la carpeta workspace",
    "qué ves en la captura adjunta? lee el texto",
    "quién es este personaje? aprende que se llama Luffy",
    "metele un hover más bonito a los botones",
    "Archivo adjunto: notas.txt — corrige la ortografía",
]
# Muestras con historial que contiene código (cambia la selección de write_code).
SAMPLES_WITH_CODE_HISTORY = ["agregale un fondo oscuro", "ahora cambiale el título"]


def prepare_env(source_db: str | None = None) -> Path:
    """Copia consistente de la BD a un directorio temporal y apunta el entorno a ella."""
    src = Path(source_db or os.environ.get("ELIPSE_TEST_DB") or ROOT / "elipse.db")
    if not src.exists():
        raise FileNotFoundError(
            f"No encuentro la base a copiar: {src}. Pasa la ruta con ELIPSE_TEST_DB=<ruta a una COPIA de elipse.db>."
        )
    tmp = Path(tempfile.mkdtemp(prefix="elipse_golden_"))
    dst = tmp / "elipse.db"
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        s.backup(d)            # consistente aunque la base esté en WAL
    finally:
        s.close(); d.close()

    os.environ["ELIPSE_DB_PATH"] = str(dst)
    os.environ["MCP_CONFIG_FILE"] = "no_existe_mcp.json"       # MCP apagado
    os.environ["CHROMA_DIR"] = str(tmp / "chroma")
    os.environ["WORKSPACE_DIR"] = str(tmp / "workspace")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return dst


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def compute_snapshot() -> dict:
    """Foto del comportamiento actual. Solo usa la API pública de siempre (sin argumentos de perfil)."""
    from app.core import core as core_mod, personality, tools
    from app.core.db import init_db
    from app.core.voice import store, brief

    init_db()   # camino real de arranque (con la Fase 1 también ejerce la migración)

    snap = {
        "prompt_full": personality.build_system_prompt(False),
        "prompt_compact": personality.build_system_prompt(True),
        "schema_full": tools.get_tools_schema(),
        "schema_compact": {},
        "required_tools": {},
        "voice_effective": store.effective(),
        "voice_brief": brief(),
    }

    for msg in SAMPLES:
        chosen = tools.get_tools_schema_compact(msg)
        snap["schema_compact"][msg] = {
            "names": [t["function"]["name"] for t in chosen],
            "sha256": _sha(chosen),
        }
        snap["required_tools"][msg] = [r["name"] for r in core_mod._missing_required_tools(msg, [])]

    for msg in SAMPLES_WITH_CODE_HISTORY:
        chosen = tools.get_tools_schema_compact(msg, history_has_code=True)
        snap["schema_compact"][f"[hist_code] {msg}"] = {
            "names": [t["function"]["name"] for t in chosen],
            "sha256": _sha(chosen),
        }
    return snap