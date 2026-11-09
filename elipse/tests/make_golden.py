"""
Guarda la foto dorada con el código ACTUAL (sin tocar). Correr UNA vez, ANTES de la Fase 1:

    set ELIPSE_TEST_DB=C:\\ruta\\a\\una_copia_de_elipse.db      (Windows cmd; en PowerShell: $env:ELIPSE_TEST_DB="...")
    python tests/make_golden.py

Si no pones ELIPSE_TEST_DB usa <raíz>/elipse.db (igual la COPIA a un temporal; no la modifica).
Genera tests/golden_default.json — guárdalo en git; es la referencia de "elipse no cambió".
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _snapshot  # noqa: E402

if __name__ == "__main__":
    _snapshot.prepare_env()
    snap = _snapshot.compute_snapshot()
    _snapshot.GOLDEN_FILE.write_text(json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"Foto guardada en {_snapshot.GOLDEN_FILE}")
    print(f"  prompt completo: {len(snap['prompt_full'])} car. | compacto: {len(snap['prompt_compact'])} car.")
    print(f"  tools en el catálogo: {len(snap['schema_full'])} | mensajes de muestra: {len(snap['schema_compact'])}")