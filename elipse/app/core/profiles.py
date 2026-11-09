"""
Perfiles de ELIPSE (Fase 1: cimientos · Fase 2: capacidades + gestión).

Un perfil es una configuración de trabajo (rol, tools, voz, memoria). 'elipse' es el perfil
implícito: es el comportamiento de siempre, no tiene fila en la tabla y NO se puede definir
por API. Cualquier otro perfil (p. ej. 'rbe') nace SIN permisos (R3: deny-by-default) y solo
puede lo que su spec declare Y el techo (caps) de su llave permita:  efectivo = spec ∩ caps.

Ojo con el glosario: "perfil" aquí NO es el "perfil de modelo" de skillc.py (tiny/small/large).

Este módulo solo depende de db (sin dependencias circulares). Lo usan por igual el servidor
(profiles_api.py, tools.py), el CLI (manage_profiles.py) y, por el CLI, el launcher visual.
"""

import json
import re
import secrets
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.db import get_connection

DEFAULT_NAME = "elipse"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
RESERVED = frozenset({"elipse", "admin", "fish", "local", "default", "system", "all"})
HOST_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")

SPEC_MAX_BYTES = 32 * 1024
PIPELINES = ("code", "docs", "docedit", "plan")
RISKS = ("read", "write", "danger")
# Claves que el modelo/anfitrión nunca puede meter en los argumentos de una tool (R13).
PROFILE_ARG_KEYS = frozenset({"profile", "actor", "session"})

# Nombres de las tools propias (se contrastan con tools.TOOL_FUNCTIONS en los tests).
BUILTIN_TOOL_NAMES = frozenset({
    "describe_image", "read_image_text", "learn_character", "identify_character", "forget_character",
    "get_current_datetime", "calculate", "list_files", "read_file", "write_file", "search_web",
    "search_images", "run_python", "remember", "recall", "research_topic", "write_code", "read_url",
    "write_document",
})
# FASE 2: las únicas tools propias que un perfil ≠ 'elipse' puede usar. El resto (memoria, archivos,
# web, código, visión…) todavía es GLOBAL, no por perfil: se abren de a una en la Fase 3, cuando
# cada una reciba el perfil por código. Mientras tanto, pedirlas se recorta y se informa (R5).
PROFILE_SAFE_BUILTINS = frozenset({"get_current_datetime", "calculate", "remember", "recall"})
# FASE 3: tools que reciben `profile` por código (R13). execute_tool lo inyecta y descarta lo que el modelo mande.
PROFILE_AWARE_TOOLS = frozenset({"remember", "recall"})
# Techo sugerido de una llave de anfitrión: lo que ya traía la Fase 2 (la memoria se concede a propósito).
_BASE_CAPS_TOOLS = ("calculate", "get_current_datetime")

MEMORY_DEFAULTS = {"max_items": 500, "max_distance": None}

DEFAULT_LIMITS = {"rounds": 3, "tool_timeout_s": 10, "max_rows": 100, "history_limit": 8, "queue_timeout_s": 120}
LIMIT_BOUNDS = {"rounds": (1, 6), "tool_timeout_s": (1, 60), "max_rows": (1, 1000),
                "history_limit": (1, 20), "queue_timeout_s": (5, 600)}

# Claves de spec que se conocen. Cualquier otra se descarta y se informa (falla cerrado).
SPEC_KEYS = frozenset({
    "role", "brief", "traits", "rules", "tools", "pipelines", "vision", "attachments", "skills",
    "continuation", "voice", "model", "memory", "limits", "confirm", "data_policy", "retention",
})


@dataclass(frozen=True)
class Profile:
    name: str
    is_default: bool                 # True solo para 'elipse'
    spec: dict                       # spec guardado (el efectivo se calcula con effective_spec)
    caps: dict | None                # techo de la llave; None solo en la llave admin
    host_tools: tuple = ()           # tools del anfitrión YA recortadas por caps (esquema + risk)
    synced: bool = True              # False: la llave existe pero el perfil aún no se sincronizó
    enabled: bool = True             # False: perfil desactivado por el admin


@dataclass(frozen=True)
class Principal:
    """Quién hace la petición. Sale SIEMPRE de la API key, nunca del texto ni de las tools (R4)."""
    key_id: int
    key_name: str
    profile: Profile
    is_admin: bool                   # profile.is_default and caps is None


DEFAULT = Profile(DEFAULT_NAME, True, {}, None, ())


# --------------------------------------------------------------------------- utilidades

def valid_name(name: str) -> bool:
    """Nombre válido para un perfil NUEVO (excluye 'elipse' y los reservados)."""
    return isinstance(name, str) and bool(NAME_RE.match(name)) and name not in RESERVED


def reserved_tool_names() -> frozenset:
    """Nombres que una tool de anfitrión no puede usar: las propias (y, con el servidor arriba, las reales)."""
    names = set(BUILTIN_TOOL_NAMES)
    tools_mod = sys.modules.get("app.core.tools")
    funcs = getattr(tools_mod, "TOOL_FUNCTIONS", None)
    if isinstance(funcs, dict):
        names |= set(funcs)
    return frozenset(names)


def _strs(value) -> list:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _spec_of(row) -> dict:
    try:
        spec = json.loads(row["spec"])
        return spec if isinstance(spec, dict) else {}
    except (TypeError, ValueError):
        return {}


def _load_row(name: str):
    conn = get_connection()
    try:
        return conn.execute("SELECT spec, enabled FROM profiles WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()


# --------------------------------------------------------------------------- spec ∩ caps

def default_caps() -> dict:
    """Techo sugerido para una llave de anfitrión (restrictivo). El launcher lo usa de punto de partida."""
    return {
        "tools": list(_BASE_CAPS_TOOLS),
        "host_tools": True,
        "max_host_tools": 60,
        "pipelines": [],
        "vision": False,
        "attachments": False,
        "continuation": False,
        "mcp": [],
        "skills": [],
        "voice": {"allowed": False, "cloud": False},
        "data_policy": "local_only",
    }


def _norm_host_tool(raw: dict, reserved: frozenset):
    """(tool normalizada | None, motivo de rechazo | None)."""
    name = raw.get("name")
    if not isinstance(name, str) or not HOST_TOOL_RE.match(name):
        return None, "nombre inválido (a-z, 0-9, _; 2-48 caracteres)"
    if name in reserved:
        return None, "el nombre choca con una tool propia"
    params = raw.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        params = {"type": "object", "properties": {}}
    if len(json.dumps(params)) > 4000:
        return None, "parámetros demasiado grandes"
    risk = raw.get("risk") if raw.get("risk") in RISKS else "write"     # dudoso → confirma
    return {"name": name, "description": str(raw.get("description") or "")[:300],
            "parameters": params, "risk": risk}, None


def effective_spec(spec, caps, reserved: frozenset | None = None):
    """
    spec ∩ caps → (efectivo, rechazados). `caps=None` NO significa "sin techo" aquí: se trata
    como {} (todo apagado). Solo el perfil 'elipse' (llave admin) se salta esta función.
    """
    spec = spec if isinstance(spec, dict) else {}
    caps = caps if isinstance(caps, dict) else {}
    reserved = reserved if reserved is not None else reserved_tool_names()
    rejected: list[str] = []
    tools = spec.get("tools") if isinstance(spec.get("tools"), dict) else {}

    # --- tools propias
    cap_b = set(_strs(caps.get("tools")))
    builtin = []
    for t in _strs(tools.get("builtin")):
        if t not in BUILTIN_TOOL_NAMES:
            rejected.append(f"tools.builtin:{t} (no existe)")
        elif t not in PROFILE_SAFE_BUILTINS:
            rejected.append(f"tools.builtin:{t} (aún no disponible para perfiles)")
        elif t not in cap_b:
            rejected.append(f"tools.builtin:{t} (fuera del techo de la llave)")
        elif t not in builtin:
            builtin.append(t)

    # --- tools del anfitrión
    host, seen = [], set()
    host_in = [h for h in tools.get("host", []) if isinstance(h, dict)] if isinstance(tools.get("host"), list) else []
    max_host = _clamp(caps.get("max_host_tools"), 0, 200, 60)
    for h in host_in:
        label = f"tools.host:{h.get('name')}"
        if not caps.get("host_tools"):
            rejected.append(f"{label} (la llave no permite tools del anfitrión)")
            continue
        norm, why = _norm_host_tool(h, reserved)
        if norm is None:
            rejected.append(f"{label} ({why})")
        elif norm["name"] in seen:
            rejected.append(f"{label} (repetida)")
        elif len(host) >= max_host:
            rejected.append(f"{label} (pasa el máximo de {max_host})")
        else:
            seen.add(norm["name"])
            host.append(norm)

    # --- MCP / skills / pipelines (lista ∩ techo)
    def inter(key, want, cap, label):
        out = []
        for x in want:
            if x in cap and x not in out:
                out.append(x)
            elif x not in cap:
                rejected.append(f"{label}:{x} (fuera del techo de la llave)")
        return out

    mcp = inter("mcp", _strs(tools.get("mcp")), set(_strs(caps.get("mcp"))), "tools.mcp")
    skills = inter("skills", _strs(spec.get("skills")), set(_strs(caps.get("skills"))), "skills")
    pipes = [p for p in _strs(spec.get("pipelines")) if p in PIPELINES]
    pipelines = inter("pipelines", pipes, set(_strs(caps.get("pipelines"))), "pipelines")

    def flag(key):
        want, allowed = bool(spec.get(key)), bool(caps.get(key))
        if want and not allowed:
            rejected.append(f"{key} (fuera del techo de la llave)")
        return want and allowed

    # --- voz
    voice_in = spec.get("voice") if isinstance(spec.get("voice"), dict) else {}
    caps_voice = caps.get("voice") if isinstance(caps.get("voice"), dict) else {}
    voice = dict(voice_in)
    if not caps_voice.get("allowed"):
        if voice_in.get("enabled"):
            rejected.append("voice (fuera del techo de la llave)")
        voice = {"enabled": False}
    else:
        voice["cloud_allowed"] = bool(voice_in.get("cloud_allowed")) and bool(caps_voice.get("cloud"))

    # --- política de datos: local_only manda si CUALQUIERA de los dos lo pide (default: local_only)
    policy = "local_only" if "local_only" in (spec.get("data_policy", "local_only"),
                                               caps.get("data_policy", "local_only")) else "allow_cloud"
    if policy == "local_only":
        voice["cloud_allowed"] = False

    mem_in = spec.get("memory") if isinstance(spec.get("memory"), dict) else {}
    max_dist = mem_in.get("max_distance")
    memory = {
        "max_items": _clamp(mem_in.get("max_items"), 1, 5000, MEMORY_DEFAULTS["max_items"]),
        # None = sin umbral (como hoy). Coseno: 0 = idéntico, 2 = opuesto.
        "max_distance": (max(0.0, min(2.0, float(max_dist)))
                         if isinstance(max_dist, (int, float)) and not isinstance(max_dist, bool) else None),
    }

    limits_in = spec.get("limits") if isinstance(spec.get("limits"), dict) else {}
    limits = {k: _clamp(limits_in.get(k), lo, hi, DEFAULT_LIMITS[k]) for k, (lo, hi) in LIMIT_BOUNDS.items()}

    eff = dict(spec)
    eff.update({
        "tools": {"builtin": builtin, "host": host, "mcp": mcp},
        "pipelines": pipelines, "skills": skills,
        "vision": flag("vision"), "attachments": flag("attachments"), "continuation": flag("continuation"),
        "voice": voice, "data_policy": policy, "limits": limits, "memory": memory,
    })
    return eff, rejected


class Capabilities:
    """
    Fachada que consultan TODOS los puntos de enforcement. Para 'elipse' todo está permitido y
    filter_schema devuelve el catálogo sin copiarlo ni reordenarlo (R1).
    """

    def __init__(self, profile: Profile):
        self.profile = profile
        self.is_default = profile.is_default
        self._eff = {} if self.is_default else effective_spec(profile.spec, profile.caps)[0]
        tools = self._eff.get("tools", {})
        self._builtin = frozenset(tools.get("builtin", ()))
        self._mcp = frozenset(tools.get("mcp", ()))
        self._host = {t["name"]: t for t in tools.get("host", ())}

    def tool_allowed(self, name: str) -> bool:
        return self.is_default or name in self._builtin or name in self._mcp or name in self._host

    def is_host_tool(self, name: str) -> bool:
        return (not self.is_default) and name in self._host

    def host_tool(self, name: str) -> dict | None:
        return self._host.get(name)

    def pipeline_allowed(self, kind: str) -> bool:
        return self.is_default or kind in self._eff.get("pipelines", ())

    def vision_allowed(self) -> bool:
        return self.is_default or bool(self._eff.get("vision"))

    def attachments_allowed(self) -> bool:
        return self.is_default or bool(self._eff.get("attachments"))

    def continuation_allowed(self) -> bool:
        return self.is_default or bool(self._eff.get("continuation"))

    def skills_allowed(self) -> list:
        return [] if self.is_default else list(self._eff.get("skills", ()))   # 'elipse': lógica de siempre

    def voice_config(self) -> dict:
        return {} if self.is_default else dict(self._eff.get("voice", {}))

    def memory(self) -> dict:
        """Memoria del perfil ({max_items, max_distance}); vacío para 'elipse' (usa settings, como siempre)."""
        return {} if self.is_default else dict(self._eff.get("memory", MEMORY_DEFAULTS))

    def confirm_for(self, risk: str) -> bool:
        """
        ¿Una tool del anfitrión con este riesgo pide confirmación humana? 'danger' SIEMPRE (no se puede apagar);
        el resto según spec.confirm (default: read→never, write→always). Valor desconocido → confirma (falla cerrado).
        """
        if risk == "danger" or self.is_default:
            return True
        conf = self._eff.get("confirm")
        conf = conf if isinstance(conf, dict) else {}
        mode = conf.get(risk, "never" if risk == "read" else "always")
        return mode != "never"

    def data_policy(self) -> str:
        return "allow_cloud" if self.is_default else self._eff.get("data_policy", "local_only")

    def limits(self) -> dict:
        return {} if self.is_default else dict(self._eff.get("limits", DEFAULT_LIMITS))

    def filter_schema(self, schema: list) -> list:
        """Idempotente: dejar pasar solo lo permitido y añadir (una vez) las tools del anfitrión."""
        if self.is_default:
            return schema
        allowed = self._builtin | self._mcp | frozenset(self._host)
        out = [t for t in schema if t.get("function", {}).get("name") in allowed]
        present = {t.get("function", {}).get("name") for t in out}
        for name, t in self._host.items():
            if name not in present:
                out.append({"type": "function", "function": {
                    "name": name, "description": t["description"], "parameters": t["parameters"]}})
        return out


def memory_settings(name: str) -> dict:
    """Memoria efectiva de un perfil por nombre ({max_items, max_distance}); defaults si no existe/está mal."""
    row = _load_row(name) if name != DEFAULT_NAME and valid_name(name) else None
    spec = _spec_of(row) if row else {}
    return effective_spec(spec, {})[0]["memory"]


# --------------------------------------------------------------------------- perfil de una llave

def get_profile(name: str, caps: dict | None = None) -> Profile | None:
    """Perfil por nombre. None si no existe, está desactivado o el nombre no es válido (→ 403)."""
    if name == DEFAULT_NAME:
        return DEFAULT
    if not valid_name(name):
        return None
    row = _load_row(name)
    if not row or not row["enabled"]:
        return None
    spec, caps = _spec_of(row), (caps if caps is not None else {})
    return Profile(name, False, spec, caps, tuple(effective_spec(spec, caps)[0]["tools"]["host"]))


def profile_from_key(name: str, caps: dict | None) -> Profile | None:
    """
    Perfil efectivo para una API key. Falla cerrado:
      - 'elipse' con caps → inválido (None). Solo la llave admin (caps NULL) es 'elipse'.
      - otro perfil sin caps → caps = {} (todo apagado), nunca "sin techo".
      - perfil sin fila → Profile(synced=False): la llave puede sincronizar su perfil, nada más.
      - perfil desactivado → Profile(enabled=False): los endpoints responden 403.
    """
    if name == DEFAULT_NAME:
        return DEFAULT if caps is None else None
    if not valid_name(name):
        return None
    caps = caps if caps is not None else {}
    row = _load_row(name)
    if not row:
        return Profile(name, False, {}, caps, (), synced=False)
    spec = _spec_of(row)
    host = tuple(effective_spec(spec, caps)[0]["tools"]["host"])
    return Profile(name, False, spec, caps, host, enabled=bool(row["enabled"]))


def exec_profile(name: str, caller=None):
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

def sanitize_spec(spec) -> tuple[dict, list]:
    """Validación estructural. Devuelve (spec limpio, rechazados). Lanza ValueError si no es un spec usable."""
    if not isinstance(spec, dict):
        raise ValueError("El spec debe ser un objeto JSON.")
    if len(json.dumps(spec, ensure_ascii=False).encode("utf-8")) > SPEC_MAX_BYTES:
        raise ValueError(f"El spec pesa más de {SPEC_MAX_BYTES // 1024} KB.")
    clean, rejected = {}, []
    for k, v in spec.items():
        if k in SPEC_KEYS:
            clean[k] = v
        elif k in ("callback_url", "callback_secret"):
            rejected.append(f"{k} (solo el admin lo fija, con set-callback)")
        elif k != "name":
            rejected.append(f"{k} (clave desconocida)")
    for key, limit in (("role", 2000), ("brief", 300)):
        if key in clean:
            if not isinstance(clean[key], str):
                raise ValueError(f"'{key}' debe ser texto.")
            clean[key] = clean[key].strip()[:limit]
    if "rules" in clean:
        rules = _strs(clean["rules"])
        clean["rules"] = [r.strip()[:300] for r in rules if r.strip()][:30]
    if "traits" in clean:
        tr = clean["traits"] if isinstance(clean["traits"], dict) else {}
        clean["traits"] = {str(k): max(0.0, min(1.0, float(v))) for k, v in tr.items()
                           if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return clean, rejected


def _exists(name: str) -> bool:
    conn = get_connection()
    try:
        return conn.execute("SELECT 1 FROM profiles WHERE name = ?", (name,)).fetchone() is not None
    finally:
        conn.close()


def sync_profile(name: str, spec: dict, caps: dict | None, *, create_only=False, update_only=False,
                 enabled: bool | None = None) -> dict:
    """
    Crea o actualiza un perfil. `caps=None` = escribe el ADMIN (se guarda tal cual, ya limpio).
    `caps=dict` = escribe la llave del anfitrión: se guarda tal cual también (el techo se aplica al
    LEER, con effective_spec), pero la respuesta informa lo que el techo de ESA llave descartaría.
    Nunca toca callback_url / callback_secret (R12). Devuelve {"profile", "rejected"}.
    """
    if not valid_name(name):
        raise ValueError(f"Nombre de perfil inválido: '{name}' (a-z, 0-9, _ y -; 2-32 caracteres; no reservado).")
    exists = _exists(name)
    if create_only and exists:
        raise ValueError(f"Ya existe un perfil '{name}'.")
    if update_only and not exists:
        raise LookupError(f"No existe el perfil '{name}'.")
    clean, rejected = sanitize_spec(spec)
    if caps is not None:
        rejected += effective_spec(clean, caps)[1]
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO profiles (name, spec, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET spec = excluded.spec, updated_at = CURRENT_TIMESTAMP"
            + (", enabled = excluded.enabled" if enabled is not None else ""),
            (name, json.dumps(clean, ensure_ascii=False), 1 if enabled is None or enabled else 0),
        )
        conn.commit()
    finally:
        conn.close()
    return {"profile": name, "rejected": rejected}


def set_enabled(name: str, enabled: bool) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE profiles SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
                           (1 if enabled else 0, name))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_profile(name: str, revoke_keys: bool = True) -> dict:
    """Borra la fila del perfil y (por defecto) revoca sus llaves. Mensajes y logs quedan (son historial)."""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM profiles WHERE name = ?", (name,))
        revoked = 0
        if revoke_keys:
            revoked = conn.execute("UPDATE api_keys SET revoked = 1 WHERE profile = ? AND revoked = 0", (name,)).rowcount
        conn.commit()
        return {"deleted": cur.rowcount > 0, "keys_revoked": revoked}
    finally:
        conn.close()


def validate_callback_url(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("La URL del callback debe ser http(s)://host[:puerto]/ruta.")
    if p.username or p.password or p.fragment:
        raise ValueError("La URL del callback no puede llevar usuario, contraseña ni #fragmento.")
    if len(url) > 300:
        raise ValueError("La URL del callback es demasiado larga (máx. 300).")
    return url.rstrip("/")


def set_callback(name: str, url: str, secret: str | None = None, generate: bool = False) -> dict:
    """
    Fija callback_url y secreto (SOLO admin, R12). El secreto se devuelve únicamente si se generó aquí
    (es la única vez que se ve). Sin secret ni generate, se conserva el que ya tenía.
    """
    url = validate_callback_url(url)
    if secret is not None and len(secret) < 16:
        raise ValueError("El secreto debe tener al menos 16 caracteres.")
    generated = None
    if generate and secret is None:
        generated = secret = secrets.token_hex(32)
    conn = get_connection()
    try:
        if not conn.execute("SELECT 1 FROM profiles WHERE name = ?", (name,)).fetchone():
            raise LookupError(f"No existe el perfil '{name}'.")
        if secret is not None:
            conn.execute("UPDATE profiles SET callback_url=?, callback_secret=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                         (url, secret, name))
        else:
            conn.execute("UPDATE profiles SET callback_url=?, updated_at=CURRENT_TIMESTAMP WHERE name=?", (url, name))
        conn.commit()
        has_secret = conn.execute("SELECT callback_secret FROM profiles WHERE name=?", (name,)).fetchone()[0] is not None
    finally:
        conn.close()
    out = {"profile": name, "callback_url": url, "has_secret": has_secret}
    if generated:
        out["secret"] = generated
    return out


def clear_callback(name: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("UPDATE profiles SET callback_url=NULL, callback_secret=NULL, updated_at=CURRENT_TIMESTAMP WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_callback(name: str):
    """(url, secreto) para el despachador de tools del anfitrión (Fase 3). Uso interno: NUNCA exponer por API."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT callback_url, callback_secret FROM profiles WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    return (row["callback_url"], row["callback_secret"]) if row and row["callback_url"] and row["callback_secret"] else None


def _public(row, key_counts: dict) -> dict:
    spec = _spec_of(row)
    tools = spec.get("tools") if isinstance(spec.get("tools"), dict) else {}
    return {
        "name": row["name"], "enabled": bool(row["enabled"]), "synced": True,
        "brief": spec.get("brief", ""), "role": spec.get("role", ""),
        "builtin_tools": _strs(tools.get("builtin")),
        "host_tools": [h.get("name") for h in tools.get("host", []) if isinstance(h, dict)] if isinstance(tools.get("host"), list) else [],
        "callback_url": row["callback_url"], "has_secret": row["callback_secret"] is not None,
        "keys": key_counts.get(row["name"], 0),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "spec": spec,
    }


def list_profiles() -> list[dict]:
    """Vista de admin. Nunca incluye el secreto. Añade perfiles 'huérfanos': tienen llave pero aún no sincronizaron."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        keys = {r["profile"]: r["n"] for r in conn.execute(
            "SELECT profile, COUNT(*) AS n FROM api_keys WHERE revoked = 0 AND profile != ? GROUP BY profile", (DEFAULT_NAME,))}
    finally:
        conn.close()
    out = [_public(r, keys) for r in rows]
    known = {p["name"] for p in out}
    for prof, n in keys.items():
        if prof not in known:
            out.append({"name": prof, "enabled": True, "synced": False, "brief": "", "role": "", "builtin_tools": [],
                        "host_tools": [], "callback_url": None, "has_secret": False, "keys": n,
                        "created_at": None, "updated_at": None, "spec": {}})
    return sorted(out, key=lambda p: p["name"])


def get_profile_info(name: str) -> dict | None:
    return next((p for p in list_profiles() if p["name"] == name), None)