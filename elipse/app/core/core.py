"""
ELIPSE Core — el cerebro.

Recibe un mensaje y corre el Agent Loop completo:
    analizar -> planificar -> ejecutar -> verificar -> responder
(con un reintento acotado si la verificación falla).

Lo que este módulo NO sabe (a propósito):
  - Qué proveedor de IA hay debajo (local, Claude, OpenAI...): habla con un
    `Provider` (providers.py).
  - De dónde vienen las herramientas (propias o de servidores MCP): pide el
    catálogo a `get_tools_schema()` y ejecuta con `execute_tool()`.
  - Cómo se entregan los eventos de progreso: recibe un callback `emit`
    (tasks.py lo conecta a la cola/WebSocket; una CLI podría imprimirlos).
  - Nada de HTTP ni de hilos: eso es de main.py y tasks.py.

Por eso se puede probar sin Ollama ni internet (ver tests/) y cambiar de
proveedor o sumar herramientas sin tocar el loop.
"""

import time
from typing import Callable, Optional

from app.config import settings
from app.core import memory
from app.core.db import get_connection
from app.core.personality import build_system_prompt
from app.core.providers import Provider
from app.core.router import choose_provider
from app.core.safety import create_pending_action, is_risky
from app.core.tools import execute_tool, get_tools_schema, verify_tool_result

HISTORY_LIMIT = 10
MAX_LOOP_ITERATIONS = 2  # 1 intento + 1 reintento si la verificación falla

EmitFn = Callable[[dict], None]


# ---- Acceso a historial, log y memoria (lo que el Core lee/escribe de sí mismo) ----

def save_message(role: str, content: str):
    conn = get_connection()
    conn.execute("INSERT INTO messages (role, content) VALUES (?, ?)", (role, content))
    conn.commit()
    conn.close()


def get_recent_messages(limit: int = HISTORY_LIMIT):
    conn = get_connection()
    rows = conn.execute(
        "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return list(reversed([dict(r) for r in rows]))


def log_router_decision(message: str, provider: str, reason: str, duration: float):
    conn = get_connection()
    conn.execute(
        "INSERT INTO router_log (message, provider_chosen, reason, duration_seconds) VALUES (?, ?, ?, ?)",
        (message, provider, reason, duration),
    )
    conn.commit()
    conn.close()


def build_memory_context(message: str) -> str:
    """
    RAG mínimo: busca en la memoria semántica algo relacionado con el mensaje
    actual y lo devuelve como texto para inyectar como contexto adicional.
    Si no hay nada relevante (o la memoria está vacía), devuelve "".
    """
    hits = memory.search_memory(message, n_results=settings.memory_retrieval_k)
    if not hits:
        return ""

    lines = [
        "Contexto recuperado de tu memoria semántica de largo plazo "
        "(puede ser relevante o no para este mensaje, úsalo con criterio; "
        "no lo repitas textualmente si no aplica):"
    ]
    for hit in hits:
        tipo = hit["metadata"].get("type", "memoria")
        lines.append(f"- ({tipo}) {hit['content']}")
    return "\n".join(lines)


# ---- El Core ----

class ElipseCore:
    def __init__(self, provider: Provider, code_provider: Optional[Provider] = None):
        """
        provider:      el proveedor por defecto.
        code_provider: opcional; si se da, las consultas que el router clasifica
                       como "código" van a este (ej. Claude para código, local para el resto).
                       Debe soportar tool-calling de forma confiable.
        """
        self.provider = provider
        self.code_provider = code_provider

    def run(self, message: str, emit: Optional[EmitFn] = None) -> tuple[str, dict]:
        """
        Corre el Agent Loop para 'message'. Nunca lanza excepción hacia afuera.

        Devuelve (status, final):
            status: "completado" | "esperando_confirmacion" | "error"
            final:  {"reply", "provider", "reason", "used_tools", ...}
        """
        emit = emit or (lambda event: None)
        try:
            return self._run(message, emit)
        except Exception as e:
            return "error", {
                "reply": f"Error interno: {e}",
                "provider": None,
                "reason": None,
                "used_tools": False,
            }

    def _choose(self, message: str, history: list) -> tuple[Provider, str]:
        """Decide qué proveedor atiende este mensaje y deja el motivo por escrito."""
        provider_type, reason = choose_provider(message, history)
        if provider_type == "code":
            if self.code_provider is not None:
                return self.code_provider, reason
            return self.provider, f"{reason} (sin proveedor de código configurado: se usa el general)"
        return self.provider, reason

    def _run(self, message: str, emit: EmitFn) -> tuple[str, dict]:
        # --- analizar ---
        emit({"type": "progreso", "mensaje": "Analizando tu mensaje..."})
        system_prompt = build_system_prompt()
        history = get_recent_messages()

        emit({"type": "progreso", "mensaje": "Buscando contexto relevante en memoria..."})
        # Con un archivo adjunto, la tarea ya trae todo el contexto necesario: sumarle memoria
        # semántica de conversaciones viejas (a veces relacionada solo por una palabra suelta,
        # como "mapache") distrae más de lo que ayuda a un modelo chico.
        memory_context = "" if message.startswith("Archivo adjunto:") else build_memory_context(message)

        provider, reason = self._choose(message, history)
        tools_schema = get_tools_schema()  # una sola vez por corrida: catálogo estable durante el loop

        save_message("user", message)

        conversation = [{"role": "system", "content": system_prompt}]
        if memory_context:
            conversation.append({"role": "system", "content": memory_context})
        conversation.extend(history)
        conversation.append({"role": "user", "content": message})

        start = time.time()
        used_tools = False
        peak_prompt_tokens = 0
        reply = ""
        truncated = False

        def _stream_to_chunks(text: str):
            emit({"type": "chunk", "texto": text})

        for iteration in range(1, MAX_LOOP_ITERATIONS + 1):
            result = provider.chat_stream(conversation, tools=tools_schema, on_delta=_stream_to_chunks)
            peak_prompt_tokens = max(peak_prompt_tokens, result.prompt_tokens or 0)

            if not result.tool_calls:
                reply = result.content  # no hizo falta ninguna herramienta: esta YA es la respuesta
                truncated = result.truncated
                break

            used_tools = True
            conversation.append(result.as_message())

            # --- planificar: las llamadas que pide el modelo son el plan ---
            emit({
                "type": "plan",
                "intento": iteration,
                "pasos": [{"tool": c.name, "arguments": c.arguments} for c in result.tool_calls],
            })

            # --- ejecutar + verificar ---
            pending, failures = self._execute_calls(result.tool_calls, conversation, iteration, emit)

            if pending:
                return self._await_confirmation(message, provider, reason, start, pending)

            if failures and iteration < MAX_LOOP_ITERATIONS:
                emit({"type": "progreso", "mensaje": "La verificación encontró problemas, reintentando..."})
                conversation.append({
                    "role": "user",
                    "content": (
                        "La verificación automática (basada en evidencia directa, no en lo que reportaste) "
                        "encontró problemas con el resultado anterior:\n" + "\n".join(failures) +
                        "\nCorrige el plan y vuelve a intentarlo."
                    ),
                })
                continue

            # --- responder: solo texto (allow_tools=False), y el loop TERMINA aquí ---
            emit({"type": "progreso", "mensaje": "Generando la respuesta final..."})
            final_result = provider.chat_stream(
                conversation, tools=tools_schema, allow_tools=False, on_delta=_stream_to_chunks
            )
            peak_prompt_tokens = max(peak_prompt_tokens, final_result.prompt_tokens or 0)
            reply = final_result.content
            truncated = final_result.truncated
            break

        duration = time.time() - start
        save_message("assistant", reply)
        log_router_decision(message, provider.name, reason, duration)

        final = {
            "reply": reply,
            "provider": provider.name,
            "reason": reason,
            "used_tools": used_tools,
            "truncated": truncated,
        }
        if peak_prompt_tokens:
            # Útil para saber si el prompt se acerca (o pasa) del num_ctx configurado.
            final["prompt_tokens"] = peak_prompt_tokens
        return "completado", final

    def _execute_calls(self, tool_calls, conversation, iteration, emit):
        """
        Ejecuta cada llamada (salvo las riesgosas, que quedan pendientes de
        confirmación humana) y las verifica con evidencia directa.
        Devuelve (pending_confirmations, verification_failures).
        """
        pending = []
        failures = []

        for call in tool_calls:
            emit({"type": "progreso", "mensaje": f"Ejecutando herramienta: {call.name}..."})

            risky, detail = is_risky(call.name, call.arguments)
            if risky:
                action_id = create_pending_action(call.name, call.arguments, detail)
                pending.append({"id": action_id, "tool": call.name, "detail": detail})
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": (
                        f"Esta acción requiere confirmación humana antes de ejecutarse: {detail} "
                        f"(id: {action_id}). No se ejecutó nada todavía."
                    ),
                })
                continue

            output = execute_tool(call.name, call.arguments)
            conversation.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": str(output),
            })

            ok, verdict = verify_tool_result(call.name, call.arguments, output)
            emit({
                "type": "verificacion", "intento": iteration, "tool": call.name,
                "ok": ok, "detalle": verdict,
            })
            if not ok:
                failures.append(f"- {call.name}: {verdict}")

        return pending, failures

    def _await_confirmation(self, message, provider, reason, start, pending) -> tuple[str, dict]:
        reply = (
            "Esta acción necesita tu confirmación antes de ejecutarse. "
            "Usa POST /v1/confirm-action/{id} con {\"approved\": true} para aprobarla, "
            "o {\"approved\": false} para rechazarla."
        )
        save_message("assistant", reply)
        log_router_decision(message, provider.name, reason, time.time() - start)

        return "esperando_confirmacion", {
            "reply": reply,
            "provider": provider.name,
            "reason": reason,
            "used_tools": False,
            "pending_confirmations": pending,
        }