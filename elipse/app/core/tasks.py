"""
Registro de tareas en background y puente entre la API y el Core.

Aquí NO vive el Agent Loop (eso es core.py). Este módulo solo:
  - lleva el registro de tareas (estado, resultado y cola de eventos),
  - conecta el `emit` del Core con la cola que lee el WebSocket,
  - corre el pipeline de investigación con el mismo mecanismo.
"""

import queue
import threading
import time
import uuid
from typing import Optional

from app.core import codegen, research
from app.core.core import ElipseCore
from app.core.providers import build_role_provider


def _build_core() -> ElipseCore:
    """
    Único punto donde se decide QUÉ proveedor usa cada ROL, según el .env:
        DEFAULT_PROVIDER=ollama | anthropic | openai | gemini   (rol "general")
        CODE_PROVIDER=      (vacío = hereda "general"; o p. ej. "anthropic" solo para código)
        RESEARCH_PROVIDER=  (idem, para investigación)
    Si la configuración es inválida (falta una API key, nombre desconocido) falla
    AL ARRANCAR con un mensaje claro, no a mitad de una conversación.

    "general" es el ÚNICO proveedor que atiende turnos de chat (ElipseCore ya
    no recibe code_provider): "code" y "research" ahora son proveedores por
    detrás de tools (write_code, research_topic) que el orquestador general
    llama cuando decide que hacen falta — ver core.py y codegen.py para el
    porqué del cambio de diseño.
    """
    general = build_role_provider("general")
    core = ElipseCore(provider=general)

    # write_code (tools.py) usa este proveedor por dentro: si "code" no tiene
    # uno propio en el .env, hereda el general (mismo mecanismo de siempre).
    codegen.configure_provider(build_role_provider("code") or general)

    # research_topic (tools.py) usa este, ídem.
    research.configure_provider(build_role_provider("research") or general)

    return core


_core = _build_core()


def describe_providers() -> dict:
    """Qué proveedores hay activos (para /v1/status)."""
    return {
        "general": _core.provider.name,
        "code": codegen._provider.name if codegen._provider else None,
        "research": research._provider.name if research._provider else None,
    }

# Almacén de tareas en memoria. Se pierde si reinicias el servidor — suficiente
# para esta fase; una versión futura podría guardar esto en SQLite también.
_tasks: dict[str, dict] = {}


class TaskCancelled(BaseException):
    """BaseException a propósito: los `except Exception` del core/pipeline no la tragan."""


_TASK_TTL = 3600  # 1 hora


def create_task() -> str:
    now = time.time()
    for tid in [t for t, v in _tasks.items() if v["status"] != "en_progreso" and now - v.get("created", now) > _TASK_TTL]:
        _tasks.pop(tid, None)
    task_id = uuid.uuid4().hex[:8]
    _tasks[task_id] = {
        "status": "en_progreso",
        "result": None,
        "queue": queue.Queue(),
        "cancel": threading.Event(),
        "created": now,
    }
    return task_id


def cancel_task(task_id: str) -> bool:
    """Marca la tarea como cancelada. El hilo se corta en el próximo evento (chunk/progreso/verificación)."""
    task = _tasks.get(task_id)
    if not task or task["status"] != "en_progreso":
        return False
    task["cancel"].set()
    return True


def get_task(task_id: str) -> Optional[dict]:
    return _tasks.get(task_id)


def _emit(task_id: str, event: dict):
    """Manda un evento a la cola de la tarea. El WebSocket la va leyendo en vivo."""
    task = _tasks.get(task_id)
    if task:
        task["queue"].put(event)


def run_agent_task(task_id: str, message: str):
    """
    Corre el Core para 'message' dentro de una tarea. Diseñada para correr en un
    hilo aparte (ver main.py) — nunca bloquea el servidor principal.
    Soporta cancelación: cada emit() revisa el flag y lanza TaskCancelled.
    """
    from app.core.core import get_recent_messages, save_message

    task = _tasks[task_id]
    partial: list[str] = []

    def emit(event: dict):
        if task["cancel"].is_set():
            raise TaskCancelled()
        if event.get("type") == "chunk":
            partial.append(event.get("texto", ""))
        _emit(task_id, event)

    try:
        status, final = _core.run(message, emit=emit)
    except TaskCancelled:
        status = "cancelado"
        final = {
            "reply": "".join(partial) + "\n\n⏹ Detenido.",
            "provider": None, "reason": "detenido por el usuario", "used_tools": False,
        }
        # Si quedó un mensaje del usuario sin respuesta, se cierra el par para no romper el historial.
        recent = get_recent_messages(1)
        if recent and recent[-1]["role"] == "user":
            save_message("assistant", "(respuesta detenida por el usuario)")

    task["status"] = status
    task["result"] = final
    _emit(task_id, {"type": "final", "data": final})


def run_research_task(task_id: str, topic: str):
    """
    Corre el pipeline de investigación (buscar -> resumir -> guardar) dentro de
    una tarea con el mismo mecanismo de progreso/WebSocket que run_agent_task,
    para reusar /v1/task/{id} y /v1/ws/{id} sin duplicar infraestructura.
    """
    def emit_cb(event):
        _emit(task_id, event)

    try:
        result = research.run_research_pipeline(topic, emit=emit_cb)

        if result["status"] != "ok":
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["result"] = result
            _emit(task_id, {"type": "final", "data": result})
            return

        _tasks[task_id]["status"] = "completado"
        _tasks[task_id]["result"] = result
        _emit(task_id, {"type": "final", "data": result})

    except Exception as e:
        final = {"status": "error", "detail": f"Error interno: {e}"}
        _tasks[task_id]["status"] = "error"
        _tasks[task_id]["result"] = final
        _emit(task_id, {"type": "final", "data": final})