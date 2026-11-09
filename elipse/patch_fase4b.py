"""
Fase 4b — prompt de perfiles (solo personality.py; 'elipse' no cambia ni un byte).
  - Quita del prompt de un perfil las reglas que son de ELIPSE: la del creador, y las de "hazle preguntas / curiosidad".
  - Quita la mención del creador de la declaración (un asistente interno de RBE no tiene por qué hablar de eso).
  - Añade la fecha actual (si no, el modelo inventa fechas al llamar tools) y reglas de brevedad y de no inventar.
Uso (raíz del proyecto):   python patch_fase4b.py        (después de patch_fase4.py)
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")


def patch(rel, pairs):
    p = ROOT / rel
    raw = p.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    s = raw.replace("\r\n", "\n")
    for i, (old, new) in enumerate(pairs, 1):
        n = s.count(old)
        if n != 1:
            sys.exit(f"✗ {rel}: el reemplazo #{i} coincide {n} veces (debe ser 1). Nada escrito.\n--- buscaba:\n{old[:200]}")
        s = s.replace(old, new)
    p.write_bytes((s.replace("\n", "\r\n") if crlf else s).encode("utf-8"))
    print("✓ Parcheado:", rel)


patch("app/core/personality.py", [
# 1) reglas compartidas: fuera las que hablan del creador o mandan a hacer preguntas de relleno
(r'''        rules = [r["rule"] for r in conn.execute("SELECT rule FROM style_rules")]
        facts = [r["content"] for r in conn.execute(
            "SELECT content FROM (SELECT id, content FROM facts WHERE profile = ?''',
 r'''        rules = [r["rule"] for r in conn.execute("SELECT rule FROM style_rules")
                 if not any(k in r["rule"].lower() for k in ("creador", "pregunta", "curios"))]
        facts = [r["content"] for r in conn.execute(
            "SELECT content FROM (SELECT id, content FROM facts WHERE profile = ?'''),

# 2) brevedad (reglas del perfil van después, así que el anfitrión puede matizarla)
(r'''    role = str(spec.get("role") or "").strip()

    if compact:
        declaration = (f"Eres {identity['name']}, creada por {identity['creator']}. {role} "
                       f"Valores: {identity['core_values']}. Nunca niegues que {identity['creator']} es tu creador.")''',
 r'''    role = str(spec.get("role") or "").strip()
    rules.insert(0, "Responde breve y directo. No cierres con preguntas de relleno, ofertas genéricas de ayuda ni emojis, salvo que el usuario los use.")

    if compact:
        declaration = f"Eres {identity['name']}. {role} Valores: {identity['core_values']}."'''),

(r'''            f"Mi nombre es {identity['name']}.",
            f"Fui creado y desarrollado por {identity['creator']}, quien es mi creador y desarrollador.",
            f"En este sistema mi función es la siguiente: {role}",''',
 r'''            f"Mi nombre es {identity['name']}.",
            f"En este sistema mi función es la siguiente: {role}",'''),

# 3) fecha actual + regla de no inventar fechas
(r'''        "- Si una herramienta devuelve un error, cuéntalo con claridad en vez de disimularlo.",
    ])''', r'''        "- Si una herramienta devuelve un error, cuéntalo con claridad en vez de disimularlo.",
        "- Si una herramienta necesita una fecha y el usuario no la dio, usa la fecha actual indicada abajo; nunca inventes fechas.",
    ])'''),

(r'''        "Hechos que debes recordar sobre este sistema:", facts_text,
    ])


def build_system_prompt(''', r'''        "Hechos que debes recordar sobre este sistema:", facts_text, "",
        _fecha_actual(),
    ])


def _fecha_actual() -> str:
    """Línea con la fecha de hoy (hora local del servidor). Solo la usan los perfiles ≠ 'elipse'."""
    from datetime import datetime
    ahora = datetime.now()
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    return f"Fecha y hora actual: {dias[ahora.weekday()]} {ahora:%Y-%m-%d %H:%M}."


def build_system_prompt('''),
])
