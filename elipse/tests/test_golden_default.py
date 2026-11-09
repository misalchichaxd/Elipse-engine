"""
Fase 0 — Red de seguridad (R1): el perfil 'elipse' no cambia ni un byte.

Recalcula la foto con el código actual y exige igualdad EXACTA contra tests/golden_default.json
(generado con make_golden.py antes de empezar). Cada prueba reporta qué cambió.

    pytest tests/test_golden_default.py -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _snapshot  # noqa: E402


@pytest.fixture(scope="module")
def golden():
    if not _snapshot.GOLDEN_FILE.exists():
        pytest.fail("Falta tests/golden_default.json. Corre `python tests/make_golden.py` con el código SIN modificar.")
    return json.loads(_snapshot.GOLDEN_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def current():
    _snapshot.prepare_env()
    return _snapshot.compute_snapshot()


def _first_diff(a: str, b: str) -> str:
    i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return f"primera diferencia en el car. {i}: …{a[max(0, i - 30):i + 30]!r} ≠ …{b[max(0, i - 30):i + 30]!r}"


def test_prompt_completo(golden, current):
    assert current["prompt_full"] == golden["prompt_full"], _first_diff(current["prompt_full"], golden["prompt_full"])


def test_prompt_compacto(golden, current):
    assert current["prompt_compact"] == golden["prompt_compact"], _first_diff(current["prompt_compact"], golden["prompt_compact"])


def test_catalogo_completo_mismo_orden(golden, current):
    assert current["schema_full"] == golden["schema_full"]


def test_catalogo_compacto_por_mensaje(golden, current):
    assert current["schema_compact"].keys() == golden["schema_compact"].keys()
    cambios = {m: (current["schema_compact"][m]["names"], golden["schema_compact"][m]["names"])
               for m in golden["schema_compact"] if current["schema_compact"][m] != golden["schema_compact"][m]}
    assert not cambios, f"Cambió el catálogo compacto en: {json.dumps(cambios, ensure_ascii=False)}"


def test_reglas_requeridas(golden, current):
    assert current["required_tools"] == golden["required_tools"]


def test_voz(golden, current):
    assert current["voice_effective"] == golden["voice_effective"]
    assert current["voice_brief"] == golden["voice_brief"]