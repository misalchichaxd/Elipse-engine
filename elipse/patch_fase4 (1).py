"""
Fase 4 — callback HMAC al anfitrión. Aplica reemplazos exactos (respeta CRLF/LF de cada archivo).
Uso (raíz del proyecto, con respaldo hecho, DESPUÉS de las fases 3 y 3b):   python patch_fase4.py
Si un reemplazo no coincide exactamente 1 vez, no escribe ese archivo y dice cuál.
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


# ============================================================ profiles.py  (confirm_for: lo usa safety.is_risky y no existía)
patch("app/core/profiles.py", [
(r'''    def data_policy(self) -> str:
        return "allow_cloud" if self.is_default else self._eff.get("data_policy", "local_only")
''', r'''    def confirm_for(self, risk: str) -> bool:
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
'''),
])

# ============================================================ tools.py
patch("app/core/tools.py", [
(r'''from app.core import profiles as _profiles
''', r'''from app.core import profiles as _profiles
from app.core import callback as host_callback
'''),

(r'''def execute_tool(name: str, arguments: dict, profile=DEFAULT):''',
 r'''def execute_tool(name: str, arguments: dict, profile=DEFAULT, actor: str = "", session: str = ""):'''),

(r'''        if caps.is_host_tool(name):
            return "Error: las herramientas del anfitrión aún no están disponibles (llegan con el callback)."''',
 r'''        if caps.is_host_tool(name):
            # Fase 4: callback HMAC. actor/session vienen del llamador autenticado, nunca de `arguments`.
            return host_callback.call_host_tool(profile, name, arguments, actor, session)'''),
])

# ============================================================ core.py  (actor/session hasta execute_tool y la acción pendiente)
patch("app/core/core.py", [
(r'''self._execute_calls(result.tool_calls, conversation, iteration, emit, seen_calls, profile)''',
 r'''self._execute_calls(result.tool_calls, conversation, iteration, emit, seen_calls, profile, session, actor)'''),

(r'''    def _execute_calls(self, tool_calls, conversation, iteration, emit, seen_calls, profile=DEFAULT):''',
 r'''    def _execute_calls(self, tool_calls, conversation, iteration, emit, seen_calls, profile=DEFAULT,
                       session: str = "", actor: str = ""):'''),

(r'''                             else create_pending_action(call.name, call.arguments, detail, profile.name))''',
 r'''                             else create_pending_action(call.name, call.arguments, detail, profile.name,
                                                        ctx={"actor": actor, "session": session}))'''),

(r'''                      else execute_tool(call.name, call.arguments, profile))''',
 r'''                      else execute_tool(call.name, call.arguments, profile, actor, session))'''),
])

# ============================================================ safety.py  (la acción pendiente recuerda actor/session)
patch("app/core/safety.py", [
(r'''def create_pending_action(tool_name: str, arguments: dict, detail: str, profile: str = "elipse") -> str:
    action_id = uuid.uuid4().hex[:8]''', r'''def create_pending_action(tool_name: str, arguments: dict, detail: str, profile: str = "elipse",
                          ctx: dict | None = None) -> str:
    action_id = uuid.uuid4().hex[:8]
    if profile != "elipse":
        # Perfiles ≠ 'elipse': los argumentos del modelo van SIEMPRE dentro de "args"; actor/session los pone el
        # código en "ctx". Así nada de lo que escriba el modelo puede hacerse pasar por contexto firmado (R13).
        ctx = ctx or {}
        arguments = {"v": 2, "args": arguments,
                     "ctx": {"actor": str(ctx.get("actor") or "")[:64], "session": str(ctx.get("session") or "")[:64]}}'''),

(r'''        arguments = json.loads(action["arguments"])
''', r'''        arguments = json.loads(action["arguments"])
        ctx = {}
        if (action.get("profile") or "elipse") != "elipse" and isinstance(arguments, dict) \
                and arguments.get("v") == 2 and set(arguments) == {"v", "args", "ctx"}:
            ctx = arguments["ctx"] if isinstance(arguments["ctx"], dict) else {}
            arguments = arguments["args"] if isinstance(arguments["args"], dict) else {}
'''),

(r'''            result = execute_tool(action["tool_name"], arguments, act_profile)''',
 r'''            result = execute_tool(action["tool_name"], arguments, act_profile,
                                  str(ctx.get("actor") or ""), str(ctx.get("session") or ""))'''),
])

print("✓ Parcheados:", ", ".join(done))
