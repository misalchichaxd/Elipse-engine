import json
import uuid

from app.core import mcp_client
from app.core.db import get_connection
from app.core.tools import _resolve_safe_path, execute_tool
from app.core.profiles import DEFAULT, Capabilities, exec_profile


def is_risky(tool_name: str, arguments: dict, profile=None):
    """
    Decide si una llamada a herramienta es riesgosa y debe pasar por confirmación
    humana antes de ejecutarse. Devuelve (True, detalle) o (False, None).
    Esta es la única fuente de verdad sobre qué es "riesgoso" — el modelo no
    participa en esta decisión de ninguna forma.
    """
    # Tools del ANFITRIÓN: el riesgo lo declara su SDK y la política (spec.confirm) decide; 'danger' siempre confirma.
    if profile is not None and not profile.is_default:
        caps = Capabilities(profile)
        if caps.is_host_tool(tool_name):
            risk = caps.host_tool(tool_name)["risk"]
            if caps.confirm_for(risk):
                preview = json.dumps(arguments, ensure_ascii=False)
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                return True, f"Ejecutar la herramienta '{tool_name}' de {profile.name} ({risk}) con argumentos: {preview}"
            return False, None

    if tool_name == "write_file":
        path = arguments.get("path", "")
        try:
            target = _resolve_safe_path(path)
        except ValueError:
            # Ruta insegura: write_file la va a rechazar de todos modos, no hace
            # falta pasar por confirmación para algo que ya está bloqueado.
            return False, None
        if target.exists():
            return True, f"Sobreescribir el archivo existente '{path}'."

    if tool_name == "forget_character":
        return True, f"Borrar de la galería visual a '{arguments.get('name', '')}' y sus imágenes de referencia."

    if tool_name == "run_python":
        code = str(arguments.get("code", ""))
        preview = code if len(code) <= 300 else code[:300] + "..."
        return True, f"Ejecutar este código Python en tu máquina:\n{preview}"

    # Herramientas de servidores MCP externos: código de terceros que ELIPSE no
    # controla. Ninguna es de confianza por defecto; solo se ejecutan sin
    # confirmación si el usuario lo declaró en mcp_servers.json ("auto_approve").
    if mcp_client.manager.has_tool(tool_name) and not mcp_client.manager.is_auto_approved(tool_name):
        server = mcp_client.manager.server_of(tool_name)
        preview = json.dumps(arguments, ensure_ascii=False)
        if len(preview) > 200:
            preview = preview[:200] + "..."
        return True, f"Ejecutar la herramienta externa '{tool_name}' (servidor MCP '{server}') con argumentos: {preview}"

    return False, None


def create_pending_action(tool_name: str, arguments: dict, detail: str, profile: str = "elipse",
                          ctx: dict | None = None) -> str:
    action_id = uuid.uuid4().hex[:8]
    if profile != "elipse":
        # Perfiles ≠ 'elipse': los argumentos del modelo van SIEMPRE dentro de "args"; actor/session los pone el
        # código en "ctx". Así nada de lo que escriba el modelo puede hacerse pasar por contexto firmado (R13).
        ctx = ctx or {}
        arguments = {"v": 2, "args": arguments,
                     "ctx": {"actor": str(ctx.get("actor") or "")[:64], "session": str(ctx.get("session") or "")[:64]}}
    conn = get_connection()
    conn.execute(
        "INSERT INTO pending_actions (id, tool_name, arguments, detail, profile) VALUES (?, ?, ?, ?, ?)",
        (action_id, tool_name, json.dumps(arguments), detail, profile)
    )
    conn.commit()
    conn.close()
    return action_id


def get_pending_action(action_id: str):
    conn = get_connection()
    row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_pending_actions(profile: str | None = None):
    """profile=None → todas (admin); con nombre → solo las de ese perfil."""
    conn = get_connection()
    if profile is None:
        rows = conn.execute(
            "SELECT id, tool_name, detail, status, created_at, resolved_at, profile FROM pending_actions "
            "ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, tool_name, detail, status, created_at, resolved_at, profile FROM pending_actions "
            "WHERE profile = ? ORDER BY created_at DESC", (profile,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def resolve_pending_action(action_id: str, approved: bool, caller=None):
    """caller = Profile de la llave que confirma (None o DEFAULT = admin). Una acción se ejecuta con el
    perfil DE LA ACCIÓN (E9), y una llave de anfitrión solo puede resolver las suyas."""
    action = get_pending_action(action_id)
    if not action or (caller is not None and not caller.is_default
                      and (action.get("profile") or "elipse") != caller.name):
        return {"status": "error", "detail": "No existe una acción pendiente con ese id."}
    if action["status"] != "pendiente":
        return {"status": "error", "detail": f"Esta acción ya fue resuelta antes (estado: {action['status']})."}

    if approved:
        arguments = json.loads(action["arguments"])
        ctx = {}
        if (action.get("profile") or "elipse") != "elipse" and isinstance(arguments, dict) \
                and arguments.get("v") == 2 and set(arguments) == {"v", "args", "ctx"}:
            ctx = arguments["ctx"] if isinstance(arguments["ctx"], dict) else {}
            arguments = arguments["args"] if isinstance(arguments["args"], dict) else {}
        # La confirmación humana YA ocurrió aquí mismo, así que para write_file
        # forzamos overwrite=True nosotros — el modelo nunca decide esto.
        if action["tool_name"] == "write_file":
            arguments["overwrite"] = True

        act_profile = exec_profile(action.get("profile") or "elipse", caller)
        if act_profile is None:
            result = "Error: el perfil de esta acción ya no existe o es inválido; no se ejecutó."
        elif act_profile.is_default:
            result = execute_tool(action["tool_name"], arguments)
        else:
            result = execute_tool(action["tool_name"], arguments, act_profile,
                                  str(ctx.get("actor") or ""), str(ctx.get("session") or ""))
        new_status = "aprobada"
    else:
        result = "El usuario rechazó esta acción. No se ejecutó nada."
        new_status = "rechazada"

    conn = get_connection()
    conn.execute(
        "UPDATE pending_actions SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_status, action_id)
    )
    conn.commit()
    conn.close()

    return {"status": new_status, "result": result}