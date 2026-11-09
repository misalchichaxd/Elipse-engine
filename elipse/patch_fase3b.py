"""
Fase 3 (parte B) — completa lo que patch_fase3.py deja colgando.
  - profiles.py: PROFILE_AWARE_TOOLS, memory_settings(), Capabilities.memory(), abre remember/recall.
  - personality.py: regla técnica de memoria en el prompt de perfiles que tienen remember/recall.
Uso (raíz del proyecto, DESPUÉS de patch_fase3.py):   python patch_fase3b.py
Respeta CRLF/LF; si un reemplazo no coincide exactamente 1 vez, no escribe ese archivo.
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
done = []


def patch(rel, pairs):
    p = ROOT / rel
    raw = p.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    s = raw.replace("\r\n", "\n")
    for i, (old, new) in enumerate(pairs, 1):
        n = s.count(old)
        if n != 1:
            sys.exit(f"✗ {rel}: el reemplazo #{i} coincide {n} veces (debe ser 1). Nada escrito en ese archivo.\n--- buscaba:\n{old[:200]}")
        s = s.replace(old, new)
    p.write_bytes((s.replace("\n", "\r\n") if crlf else s).encode("utf-8"))
    done.append(rel)


patch("app/core/profiles.py", [
# 1) abrir remember/recall (ahora reciben el perfil por código y usan su propia colección)
(r'''PROFILE_SAFE_BUILTINS = frozenset({"get_current_datetime", "calculate"})
''', r'''PROFILE_SAFE_BUILTINS = frozenset({"get_current_datetime", "calculate", "remember", "recall"})
# FASE 3: tools que reciben `profile` por código (R13). execute_tool lo inyecta y descarta lo que el modelo mande.
PROFILE_AWARE_TOOLS = frozenset({"remember", "recall"})
# Techo sugerido de una llave de anfitrión: lo que ya traía la Fase 2 (la memoria se concede a propósito).
_BASE_CAPS_TOOLS = ("calculate", "get_current_datetime")

MEMORY_DEFAULTS = {"max_items": 500, "max_distance": None}
'''),

# 2) default_caps no cambia con el nuevo set
(r'''        "tools": sorted(PROFILE_SAFE_BUILTINS),
        "host_tools": True,''', r'''        "tools": list(_BASE_CAPS_TOOLS),
        "host_tools": True,'''),

# 3) memoria normalizada dentro del spec efectivo
(r'''    limits_in = spec.get("limits") if isinstance(spec.get("limits"), dict) else {}''',
 r'''    mem_in = spec.get("memory") if isinstance(spec.get("memory"), dict) else {}
    max_dist = mem_in.get("max_distance")
    memory = {
        "max_items": _clamp(mem_in.get("max_items"), 1, 5000, MEMORY_DEFAULTS["max_items"]),
        # None = sin umbral (como hoy). Coseno: 0 = idéntico, 2 = opuesto.
        "max_distance": (max(0.0, min(2.0, float(max_dist)))
                         if isinstance(max_dist, (int, float)) and not isinstance(max_dist, bool) else None),
    }

    limits_in = spec.get("limits") if isinstance(spec.get("limits"), dict) else {}'''),

(r'''        "voice": voice, "data_policy": policy, "limits": limits,
    })''', r'''        "voice": voice, "data_policy": policy, "limits": limits, "memory": memory,
    })'''),

# 4) Capabilities.memory()
(r'''    def data_policy(self) -> str:
        return "allow_cloud" if self.is_default else self._eff.get("data_policy", "local_only")
''', r'''    def memory(self) -> dict:
        """Memoria del perfil ({max_items, max_distance}); vacío para 'elipse' (usa settings, como siempre)."""
        return {} if self.is_default else dict(self._eff.get("memory", MEMORY_DEFAULTS))

    def data_policy(self) -> str:
        return "allow_cloud" if self.is_default else self._eff.get("data_policy", "local_only")
'''),

# 5) memory_settings(nombre) para tools.remember/recall
(r'''# --------------------------------------------------------------------------- perfil de una llave
''', r'''def memory_settings(name: str) -> dict:
    """Memoria efectiva de un perfil por nombre ({max_items, max_distance}); defaults si no existe/está mal."""
    row = _load_row(name) if name != DEFAULT_NAME and valid_name(name) else None
    spec = _spec_of(row) if row else {}
    return effective_spec(spec, {})[0]["memory"]


# --------------------------------------------------------------------------- perfil de una llave
'''),
# 5b) exec_profile: con qué perfil se ejecuta una acción pendiente al confirmarla (lo importa safety.py)
(r'''# --------------------------------------------------------------------------- gestión (admin)
''', r'''def exec_profile(name: str, caller=None):
    """
    Perfil con el que se ejecuta una acción pendiente al confirmarla (E9). Falla cerrado.
      - 'elipse' → DEFAULT.
      - confirma una llave de anfitrión → solo si la acción es de SU perfil: se usa el perfil de la llave
        (con su techo real). Una acción ajena no llega aquí (resolve_pending_action ya la rechazó).
      - confirma la llave admin → el perfil de la acción con el techo restrictivo por defecto: si la
        acción pedía algo por encima, execute_tool la rechaza en vez de ampliar permisos.
    None = perfil inexistente/desactivado/inválido (no se ejecuta nada).
    """
    if name == DEFAULT_NAME:
        return DEFAULT
    if caller is not None and not caller.is_default:
        return caller if (caller.name == name and caller.enabled) else None
    return get_profile(name, default_caps())


# --------------------------------------------------------------------------- gestión (admin)
'''),

])

patch("app/core/personality.py", [
(r'''    facts_text = "\n".join(f"- {f}" for f in facts) if facts else "(sin hechos guardados todavía)"
    return "\n".join([
        declaration, "",''', r'''    from app.core.profiles import Capabilities
    caps = Capabilities(profile)
    if caps.tool_allowed("remember") or caps.tool_allowed("recall"):
        tool_rules += "\n" + "- Tu memoria es solo tuya: usa remember para guardar datos que valga la pena conservar y recall para buscar antes de decir que no sabes algo."
    facts_text = "\n".join(f"- {f}" for f in facts) if facts else "(sin hechos guardados todavía)"
    return "\n".join([
        declaration, "",'''),
])

print("✓ Parcheados:", ", ".join(done))
