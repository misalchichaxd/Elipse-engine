import json
import uuid

from app.core import mcp_client
from app.core.db import get_connection
from app.core.tools import _resolve_safe_path, execute_tool


def is_risky(tool_name: str, arguments: dict):
    """
    Decide si una llamada a herramienta es riesgosa y debe pasar por confirmación
    humana antes de ejecutarse. Devuelve (True, detalle) o (False, None).
    Esta es la única fuente de verdad sobre qué es "riesgoso" — el modelo no
    participa en esta decisión de ninguna forma.
    """
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


def create_pending_action(tool_name: str, arguments: dict, detail: str) -> str:
    action_id = uuid.uuid4().hex[:8]
    conn = get_connection()
    conn.execute(
        "INSERT INTO pending_actions (id, tool_name, arguments, detail) VALUES (?, ?, ?, ?)",
        (action_id, tool_name, json.dumps(arguments), detail)
    )
    conn.commit()
    conn.close()
    return action_id


def get_pending_action(action_id: str):
    conn = get_connection()
    row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_pending_actions():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, tool_name, detail, status, created_at, resolved_at FROM pending_actions ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def resolve_pending_action(action_id: str, approved: bool):
    action = get_pending_action(action_id)
    if not action:
        return {"status": "error", "detail": "No existe una acción pendiente con ese id."}
    if action["status"] != "pendiente":
        return {"status": "error", "detail": f"Esta acción ya fue resuelta antes (estado: {action['status']})."}

    if approved:
        arguments = json.loads(action["arguments"])
        # La confirmación humana YA ocurrió aquí mismo, así que para write_file
        # forzamos overwrite=True nosotros — el modelo nunca decide esto.
        if action["tool_name"] == "write_file":
            arguments["overwrite"] = True

        result = execute_tool(action["tool_name"], arguments)
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