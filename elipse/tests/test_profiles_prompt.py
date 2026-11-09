"""Fase 4b — el prompt de un perfil no arrastra cosas de ELIPSE y sí trae la fecha; el de 'elipse' no cambia."""
import re
from datetime import datetime

import pytest

from app.core import db, profiles
from app.core.personality import build_system_prompt
from app.core.profiles import DEFAULT, Profile


@pytest.fixture(autouse=True)
def entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    from app.core.seed_personality import seed
    seed()


def _rbe():
    return Profile("rbe", False, {"role": "Asistente interno de RBE."}, {}, ())


@pytest.mark.parametrize("compact", [False, True])
def test_prompt_de_perfil_sin_creador_ni_relleno(compact):
    p = build_system_prompt(compact=compact, profile=_rbe())
    assert "Asistente interno de RBE." in p
    assert "Misael" not in p and "creador" not in p.lower()
    assert "hazle preguntas" not in p.lower() and "curiosidad genuina" not in p.lower()
    assert "Responde breve y directo" in p
    assert "nunca inventes fechas" in p


@pytest.mark.parametrize("compact", [False, True])
def test_prompt_de_perfil_trae_la_fecha_de_hoy(compact):
    p = build_system_prompt(compact=compact, profile=_rbe())
    m = re.search(r"Fecha y hora actual: \w+ (\d{4}-\d{2}-\d{2}) \d{2}:\d{2}\.", p)
    assert m and m.group(1) == datetime.now().strftime("%Y-%m-%d")


def test_elipse_conserva_creador_y_no_lleva_fecha():
    for compact in (False, True):
        p = build_system_prompt(compact=compact)
        assert "Misael Urquidez" in p and "Fecha y hora actual" not in p
        assert p == build_system_prompt(compact=compact, profile=DEFAULT)
