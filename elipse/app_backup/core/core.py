"""
ELIPSE Core — el cerebro.

Recibe un mensaje y corre el Agent Loop completo:
    analizar -> planificar -> ejecutar (0..N rondas de tools) -> responder

Arquitectura (cambio importante respecto a versiones anteriores):
  Antes, un router clasificaba el mensaje COMPLETO como "código" o "general"
  y mandaba el turno entero a un proveedor distinto según eso — exclusivo,
  una sola etiqueta por turno. Eso hacía imposible resolver bien un pedido
  compuesto ("investigá X y con eso escribime el parser"): el router tenía
  que elegir uno solo de los dos modos.

  Ahora hay UN SOLO orquestador (self.provider) que atiende todo el turno y
  tiene acceso a TODAS las tools, incluidas write_code y research_topic
  (ver tools.py / codegen.py / research.py). Es el propio modelo, razonando
  con tool-calling real, el que decide si necesita investigar, escribir
  código, ambas cosas en cadena, o ninguna — no un clasificador externo
  adivinando por palabras clave antes de que cualquier modelo vea el mensaje.

  Por eso el loop de abajo ya NO fuerza allow_tools=False apenas una ronda
  de tools sale bien: eso es lo que antes impedía encadenar investigación
  seguida de código en el mismo turno (una sola ronda de tools por turno,
  pase lo que pase). Ahora el modelo puede seguir llamando tools ronda tras
  ronda hasta que decida que ya tiene lo que necesita y responde en texto —
  el bloque allow_tools=False solo se usa como salvavidas si se llega al
  tope de rondas sin que el modelo haya terminado solo.

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

import json
import re
import time
from typing import Callable, Optional

from app.config import settings
from app.core import memory
from app.core.db import get_connection
from app.core import continuation, pipeline
from app.core.personality import build_system_prompt
from app.core import vision
from app.core.providers import Provider
from app.core.safety import create_pending_action, is_risky
from app.core.tools import execute_tool, get_tools_schema, get_tools_schema_compact, verify_tool_result

import logging
log = logging.getLogger("elipse")

HISTORY_LIMIT = 10
# Tope de RONDAS de tools dentro de un mismo turno (una ronda = el modelo pide
# 1+ tool calls, se ejecutan, se le devuelven los resultados). No es "1 intento
# + 1 reintento" como antes: es cuántas veces seguidas el modelo puede decidir
# "necesito llamar algo más" antes de que el sistema lo obligue a responder en
# texto. 4 alcanza para cadenas típicas (ej. research_topic -> write_code ->
# write_file) con margen para un reintento si algo falla, sin dejar que un
# proveedor que se cuelga en tool-calling loopee para siempre.
MAX_TOOL_ROUNDS = 4

# Cuántas veces como máximo insistimos en un mismo turno cuando el modelo
# intenta cerrar con texto sin haber llamado una tool que el pedido claramente
# necesitaba (ver REQUIRED_TOOLS abajo). Con modelos chicos locales (ej.
# Qwen3-4B) se observó que el system prompt por sí solo ("REGLA FIJA: usá
# write_code para cualquier código...") no alcanza: el modelo junta bien los
# datos (busca, investiga) pero se olvida del último paso — o peor, responde
# como si no tuviera lo que él mismo acaba de traer. Esto NO es un router
# (no clasifica el turno ni elige un proveedor distinto, ver router.py
# deprecado): es un empujón puntual, acotado, para ese caso específico.
MAX_FORCED_NUDGES = 1

EmitFn = Callable[[dict], None]

TOOL_PREVIEW_CHARS = 400  # cuánto del resultado crudo de una tool se manda al cliente para mostrar


def _preview(output) -> str:
    """
    Recorta el resultado de una tool a un tamaño mostrable en el evento
    'verificacion' — así el cliente puede probar que la tool corrió de
    verdad (ej. ver 'Código generado (modelo: anthropic)...' de write_code,
    o el resumen real que trajo research_topic) sin mandar el output
    completo por WebSocket cada vez.
    """
    text = str(output) if output is not None else ""
    if len(text) > TOOL_PREVIEW_CHARS:
        return text[:TOOL_PREVIEW_CHARS] + "…"
    return text


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


def log_turn_metadata(message: str, provider: str, reason: str, duration: float):
    """
    Sigue escribiendo en la tabla 'router_log' (mismo nombre de tabla y de
    columnas que antes, para no romper /v1/router-log ni el schema de la
    DB) pero 'reason' ya no es "qué modo eligió el router": ahora describe
    qué tools encadenó el orquestador en este turno, o que no usó ninguna.
    """
    conn = get_connection()
    conn.execute(
        "INSERT INTO router_log (message, provider_chosen, reason, duration_seconds) VALUES (?, ?, ?, ?)",
        (message, provider, reason, duration),
    )
    conn.commit()
    conn.close()


def _is_local_provider(provider) -> bool:
    return str(getattr(provider, "name", "")).startswith("ollama")


def _clip_text(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + " …[recortado]"


def _clip_history(history: list, limit: int, max_chars: int) -> list:
    """Historial para el Agent Loop LOCAL: últimos `limit` mensajes, cada uno recortado
    (los 2 últimos admiten el doble). El pipeline sigue recibiendo el historial completo
    para poder recuperar el código anterior."""
    recent = history[-limit:] if limit else list(history)
    out = []
    for i, m in enumerate(recent):
        cap = max_chars * 2 if i >= len(recent) - 2 else max_chars
        out.append({**m, "content": _clip_text(m.get("content", ""), cap)})
    return out


def _estimate_tokens(text: str) -> int:
    return int(len(text) / 3.0) + 1  # conservador: español + JSON rondan 3-4 caracteres por token


def _conversation_tokens(conversation: list, tools_schema: Optional[list]) -> int:
    total = _estimate_tokens(json.dumps(tools_schema, ensure_ascii=False)) if tools_schema else 0
    for m in conversation:
        total += _estimate_tokens(str(m.get("content") or ""))
        if m.get("tool_calls"):
            total += _estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False, default=str))
    return total


def _fit_to_context(conversation: list, tools_schema: Optional[list], num_ctx: int,
                    budget: float = 0.75, current_idx: Optional[int] = None) -> None:
    """Ajusta `conversation` (in place) al presupuesto de prompt ANTES de llamar al modelo, en vez
    de dejar que Ollama recorte en silencio el inicio (system prompt + tools). Orden de sacrificio:
    1) resultados de tools largos, recortados; 2) historial más viejo; 3) el bloque de memoria."""
    if not num_ctx:
        return
    limit = int(num_ctx * budget)
    before = _conversation_tokens(conversation, tools_schema)
    if before <= limit:
        return
    if current_idx is None:
        current_idx = max((i for i, m in enumerate(conversation) if m.get("role") == "user"), default=0)

    for m in conversation:  # 1) tool results largos
        if _conversation_tokens(conversation, tools_schema) <= limit:
            break
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > 1200:
            m["content"] = _clip_text(str(m["content"]), 1200)

    while _conversation_tokens(conversation, tools_schema) > limit:  # 2) historial viejo
        first = next((i for i, m in enumerate(conversation) if m.get("role") != "system"), None)
        if first is None or first >= current_idx:
            break
        conversation.pop(first)
        current_idx -= 1

    if _conversation_tokens(conversation, tools_schema) > limit:  # 3) memoria
        for i, m in enumerate(conversation):
            if i > 0 and m.get("role") == "system":
                conversation.pop(i)
                break

    after = _conversation_tokens(conversation, tools_schema)
    log.warning("contexto ajustado al presupuesto: ~%s -> ~%s tokens (tope %s de num_ctx=%s)",
                before, after, limit, num_ctx)


def build_memory_context(message: str, k: Optional[int] = None, max_chars: Optional[int] = None) -> str:
    """
    RAG mínimo: busca en la memoria semántica algo relacionado con el mensaje
    actual y lo devuelve como texto para inyectar como contexto adicional.
    Si no hay nada relevante (o la memoria está vacía), devuelve "".
    """
    hits = memory.search_memory(message, n_results=k or settings.memory_retrieval_k)
    if not hits:
        return ""

    lines = [
        "Contexto recuperado de tu memoria semántica de largo plazo "
        "(puede ser relevante o no para este mensaje, úsalo con criterio; "
        "no lo repitas textualmente si no aplica):"
    ]
    for hit in hits:
        tipo = hit["metadata"].get("type", "memoria")
        content = _clip_text(hit["content"], max_chars) if max_chars else hit["content"]
        lines.append(f"- ({tipo}) {content}")
    return "\n".join(lines)


_TRIVIAL_MSG = re.compile(
    r"^\W*(hola|holi|buenas|buen[oa]s?( d[ií]as| tardes| noches)?|hey|ok|okay|vale|gracias|listo|"
    r"s[ií]|no|dale|perfecto|genial|bien|entiendo|claro)\W*$", re.I)


def _memory_for_turn(message: str, compact: bool) -> str:
    """Memoria semántica para este turno, omitida cuando no puede aportar valor."""
    text = (message or "").strip()
    if message.startswith("Archivo adjunto:") or len(text) < 8 or _TRIVIAL_MSG.match(text):
        return ""
    try:
        if compact:
            return build_memory_context(message, settings.ollama_memory_k, settings.ollama_memory_hit_chars)
        return build_memory_context(message)
    except Exception:
        log.exception("memoria semántica falló; sigo sin ella")
        return ""


# ---- Tools "obligatorias" no usadas: empujón puntual, no un router ----
#
# El system prompt (personality.py) ya le dice al modelo "usá SIEMPRE
# write_code para código" o "SIEMPRE search_images para imágenes, nunca
# inventes una URL". Con proveedores grandes (Claude, GPT, Gemini) eso
# alcanza. Con un modelo chico local, se vio en pruebas reales que el
# modelo puede:
#   (a) juntar toda la data que necesita (buscar, investigar, buscar
#       imágenes) pero nunca dar el último paso de generar el entregable, o
#   (b) directamente "olvidarse" de lo que juntó y responder como si no
#       tuviera nada.
#
# REQUIRED_TOOLS es una lista de reglas MUY simples: "si el mensaje del
# usuario matchea este patrón, y esta tool no fue llamada todavía en el
# turno, avisale al modelo explícitamente antes de dejarlo cerrar". Es
# fácil de extender (agregar una entrada más) o desactivar (vaciar la
# lista) sin tocar el resto del loop.

CODE_REQUEST_PATTERN = re.compile(
    r"\b(c[oó]digo|script|snippet|programa|funci[oó]n|p[aá]gina\s*(web|html)?|"
    r"html|css|javascript|python|\bjava\b|\bapp\b|aplicaci[oó]n)\b",
    re.IGNORECASE,
)

IMAGE_REQUEST_PATTERN = re.compile(
    r"\b(imagen|im[aá]genes|foto|fotos|galer[ií]a|collage)\b",
    re.IGNORECASE,
)
DOC_REQUEST_PATTERN = re.compile(r"\b(pdf|word|docx|informe|reporte|documento|ensayo|monograf[ií]a)\b", re.IGNORECASE)

REQUIRED_TOOLS = [
    {
        "name": "write_code",
        "trigger": CODE_REQUEST_PATTERN,
        "nudge": (
            "Todavía no llamaste a write_code. El pedido del usuario es sobre código "
            "(una página, un script, una función, HTML, etc.) y la regla fija del "
            "sistema es usar write_code para eso, nunca escribirlo vos directamente "
            "en la respuesta. Si ya investigaste algo o buscaste imágenes, pasá eso "
            "en el argumento 'context' de write_code y llamala ahora con todo lo que "
            "ya reuniste — no lo descartes."
        ),
    },
    {
        "name": "search_images",
        "trigger": IMAGE_REQUEST_PATTERN,
        "nudge": (
            "Todavía no llamaste a search_images. El pedido menciona imágenes/fotos, "
            "y la regla fija del sistema es que nunca inventes una URL de imagen: "
            "llamá a search_images primero y usá las URLs reales que te devuelve."
        ),
    },

    {
        "name": "write_document",
        "trigger": DOC_REQUEST_PATTERN,
        "nudge": ("Todavía no llamaste a write_document. El usuario pidió un documento/informe: "
                  "generalo ahora en Markdown completo con lo que investigaste, no lo pegues en el chat."),
    },
]


def _missing_required_tools(message: str, tools_called: list[str], has_visual: bool = False) -> list[dict]:
    """
    Reglas de REQUIRED_TOOLS cuyo patrón matchea el MENSAJE ORIGINAL del
    usuario (no la conversación entera, para no dispararse por algo que
    se mencionó de pasada varios turnos atrás) pero cuya tool todavía no
    fue llamada en este turno. Vacío si no falta nada.
    """
    is_doc = bool(DOC_REQUEST_PATTERN.search(message))
    return [
        rule for rule in REQUIRED_TOOLS
        if rule["trigger"].search(message) and rule["name"] not in tools_called
        and not (is_doc and rule["name"] == "write_code")  # un informe no es código
        # Con un adjunto visual, "esta foto"/"imagen" habla del adjunto, no pide buscar imágenes en internet.
        and not (has_visual and rule["name"] == "search_images")
    ]


# ---- El Core ----

class ElipseCore:
    def __init__(self, provider: Provider):
        """
        provider: el único proveedor que atiende turnos de chat (el
                  orquestador). Debe soportar tool-calling de forma
                  confiable — de él depende que las cadenas de tools
                  (investigar, escribir código, etc.) funcionen bien.

                  Los proveedores de "código" e "investigación" (si están
                  configurados distinto al general) ya NO se pasan acá:
                  viven detrás de las tools write_code / research_topic
                  (ver codegen.py / research.py), configurados aparte en
                  tasks.py. Este Core ni se entera de que existen.
        """
        self.provider = provider

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

    def _run(self, message: str, emit: EmitFn) -> tuple[str, dict]:
        # --- analizar ---
        emit({"type": "progreso", "mensaje": "Analizando tu mensaje..."})
        compact = _is_local_provider(self.provider) and settings.ollama_compact_prompt
        history = get_recent_messages()

        provider = self.provider
        start = time.time()

        # --- Visión: el CÓDIGO mira los adjuntos visuales antes de que el modelo principal piense ---
        # Si hay imagen, la describe el rol "vision" y el texto se suma al mensaje; el modelo
        # principal (que no ve) razona sobre esa descripción. Ver vision.py.
        vision_block, vision_history = vision.describe_attachments_ex(message, emit)

        # --- Pipeline por plan (Fase 7) ---
        # Antes de meter al orquestador en el Agent Loop ronda a ronda, se le pide
        # UNA sola pasada de planificación corta (JSON: qué pasos hacen falta). Si
        # el plan sale útil, el CÓDIGO ejecuta la cadena (research/search_images en
        # paralelo, write_code al final) y arma la respuesta — el modelo chico no
        # tiene que sostener el hilo completo del turno (ver pipeline.py para el
        # porqué). Si no corresponde planificar, el plan sale vacío/inválido, o
        # cualquier cosa falla, pipeline.try_run() devuelve None y este método
        # sigue exactamente como antes con el Agent Loop completo.
        # Antes del pipeline: «sigue/continúa/...» retoma lo que quedó cortado, y un mensaje que es solo
        # puntuación («...») se contesta sin despertar al orquestador (continuation.py).
        try:
            pipeline_outcome = continuation.handle_turn(provider, message, emit)
        except Exception:
            log.exception("continuation.handle_turn falló")
            pipeline_outcome = None
        # Con imagen se salta el pipeline: su planificador solo ve el texto del pedido y podría
        # planear una búsqueda de imágenes en internet en vez de usar la que adjuntó el usuario.
        if pipeline_outcome is None and not vision_block:
            try:
                pipeline_outcome = pipeline.try_run(provider, message, history, emit)
            except Exception as e:
                log.exception("pipeline.try_run falló")
                emit({"type": "progreso",
                      "mensaje": f"⚠ el pipeline falló ({type(e).__name__}: {e}); sigo con el modo normal"})
                pipeline_outcome = None

        if pipeline_outcome is not None:
            save_message("user", message)
            save_message("assistant", getattr(pipeline_outcome, "history_note", "") or pipeline_outcome.reply)
            if getattr(pipeline_outcome, "from_continuation", False):
                can_continue = bool(pipeline_outcome.truncated)       # continuation.py ya guardó lo pendiente
            elif pipeline_outcome.truncated and "write_document" not in pipeline_outcome.tools_called:
                kind = "code" if "```" in pipeline_outcome.reply else "text"
                can_continue = continuation.remember(kind, pipeline_outcome.reply, message)
            else:
                can_continue = False
            reason = (
                f"pipeline por plan: {pipeline_outcome.summary}"
                if pipeline_outcome.ok else
                f"pipeline por plan (con fallas): {pipeline_outcome.summary}"
            )
            log_turn_metadata(message, provider.name, reason, time.time() - start)
            final = {
                "reply": pipeline_outcome.reply,
                "provider": provider.name,
                "reason": reason,
                "used_tools": bool(pipeline_outcome.tools_called),
                "truncated": pipeline_outcome.truncated,
                "can_continue": can_continue,
                "replace_previous": bool(getattr(pipeline_outcome, "replace_previous", False)),
            }
            return "completado", final

        # Solo si el pipeline no resolvió el turno se necesita construir este contexto.
        system_prompt = build_system_prompt(compact=compact)
        emit({"type": "progreso", "mensaje": "Buscando contexto relevante en memoria..."})
        memory_context = _memory_for_turn(message, compact)

        if compact:
            # Modelo local: solo las tools que este pedido necesita, con descripciones recortadas.
            forced = tuple(r["name"] for r in REQUIRED_TOOLS
                           if r["trigger"].search(message) and not (vision_block and r["name"] == "search_images"))
            has_code = any("```" in (m.get("content") or "") for m in history[-2:])
            tools_schema = get_tools_schema_compact(
                message, max_tools=settings.ollama_max_tools, extra=forced, history_has_code=has_code)
        else:
            tools_schema = get_tools_schema()  # una sola vez por corrida: catálogo estable durante el loop

        if vision_block and "busca" not in message.lower():
            # El usuario ya trajo su imagen: ofrecer search_images solo invita a buscar otra en internet.
            tools_schema = [t for t in tools_schema if t.get("function", {}).get("name") != "search_images"]

        # El historial guarda el mensaje CON la descripción: así las preguntas de seguimiento
        # ("¿y de qué color era el techo?") todavía la tienen a mano.
        message_for_model = f"{message}\n\n{vision_block}" if vision_block else message
        if memory_context:
            # La memoria cambia entre turnos; al quedar al final permite reutilizar la KV cache.
            message_for_model = (
                f"{message_for_model}\n\n[Nota del sistema, no la escribió el usuario]\n{memory_context}"
            )
        # Un fallo de visión NO se guarda en el historial: si no, el modelo lo repite en cada turno.
        # (en el historial va la versión corta: el texto OCR completo queda en el caché y se
        # recupera con read_image_text, así el historial no se llena de texto de capturas)
        save_message("user", message if (vision.FAIL_MARK in (vision_block or "") or not vision_history)
                     else f"{message}\n\n{vision_history}")

        conversation = [{"role": "system", "content": system_prompt}]
        conversation.extend(
            _clip_history(history, settings.ollama_history_limit, settings.ollama_history_msg_chars)
            if compact else history
        )
        conversation.append({"role": "user", "content": message_for_model})
        current_idx = len(conversation) - 1

        used_tools = False
        tools_called: list[str] = []  # nombres de tools usadas en este turno, en orden, para el log
        peak_prompt_tokens = 0
        reply = ""
        truncated = False
        tool_rounds = 0          # rondas con tool_calls REALES ya ejecutadas (tope: MAX_TOOL_ROUNDS)
        nudges_used = 0          # empujones ya gastados por tool obligatoria faltante (tope: MAX_FORCED_NUDGES)
        closed_by_model = False  # True si el modelo cerró solo con texto (con o sin empujón de por medio)
        seen_calls: dict = {}    # {(tool, args_json): output} — llamadas IDÉNTICAS ya ejecutadas en este turno

        def _stream_to_chunks(text: str):
            emit({"type": "chunk", "texto": text})

        iteration = 0
        while True:
            iteration += 1
            if compact:
                _fit_to_context(conversation, tools_schema, getattr(provider, "_num_ctx", 0),
                                settings.ollama_context_budget, current_idx)
                current_idx = max((i for i, m in enumerate(conversation) if m.get("role") == "user"), default=0)
            result = provider.chat_stream(conversation, tools=tools_schema, on_delta=_stream_to_chunks)
            peak_prompt_tokens = max(peak_prompt_tokens, result.prompt_tokens or 0)

            if not result.tool_calls:
                # El modelo decidió que ya puede responder — puede ser la primera
                # ronda (sin tools) o la N-ésima después de encadenar varias.
                # Antes de aceptarlo, chequeamos si se saltó una tool que el
                # pedido claramente necesitaba (ver REQUIRED_TOOLS). Esto NO
                # vuelve a evaluar el contenido de la respuesta, solo si faltó
                # llamar algo puntual — y solo insiste MAX_FORCED_NUDGES veces
                # para no loopear si el modelo se niega.
                missing = _missing_required_tools(message, tools_called, has_visual=bool(vision_block))
                if missing and nudges_used < MAX_FORCED_NUDGES:
                    nudges_used += 1
                    emit({
                        "type": "progreso",
                        "mensaje": "Faltó un paso obligatorio antes de responder, insistiendo...",
                    })
                    if result.content:
                        # Se guarda como contexto (no como respuesta final) para que el
                        # modelo tenga continuidad de lo que ya había armado.
                        conversation.append({"role": "assistant", "content": result.content})
                    conversation.append({
                        "role": "user",
                        "content": (
                            "AVISO AUTOMÁTICO DEL SISTEMA (esto no lo escribió el usuario): "
                            + " ".join(rule["nudge"] for rule in missing)
                        ),
                    })
                    continue

                reply = result.content
                truncated = result.truncated
                closed_by_model = True
                break

            used_tools = True
            tool_rounds += 1
            tools_called.extend(c.name for c in result.tool_calls)
            conversation.append(result.as_message())

            emit({
                "type": "plan",
                "intento": iteration,
                "pasos": [{"tool": c.name, "arguments": c.arguments} for c in result.tool_calls],
            })

            pending, failures = self._execute_calls(result.tool_calls, conversation, iteration, emit, seen_calls)

            if pending:
                return self._await_confirmation(message, provider, tools_called, start, pending)

            if failures:
                emit({"type": "progreso", "mensaje": "La verificación encontró problemas, avisando al modelo..."})
                conversation.append({
                    "role": "user",
                    "content": (
                        "La verificación automática (basada en evidencia directa, no en lo que reportaste) "
                        "encontró problemas con el resultado anterior:\n" + "\n".join(failures) +
                        "\nCorrige el plan si corresponde, o segui con lo que sí tenés."
                    ),
                })

            if tool_rounds >= MAX_TOOL_ROUNDS:
                # Se acabaron las rondas de tools permitidas: no le preguntamos
                # de nuevo si quiere seguir llamando algo, vamos directo a la
                # pasada final salvavidas de abajo (mismo comportamiento que
                # antes, cuando esto era un for/else).
                break
            # Si no, el while sigue: tanto si hubo fallas (para que el modelo
            # corrija) como si todo salió bien (para que pueda encadenar otra
            # tool si lo necesita), se le vuelve a dar el turno con tools
            # todavía disponibles.

        if not closed_by_model:
            # Salvavidas: se acabaron las rondas de tools sin que el modelo
            # cerrara solo con texto (ni siquiera después del empujón) —
            # pasada final SIN tools, obligado a responder.
            emit({"type": "progreso", "mensaje": "Generando la respuesta final..."})
            final_result = provider.chat_stream(
                conversation, tools=tools_schema, allow_tools=False, on_delta=_stream_to_chunks
            )
            peak_prompt_tokens = max(peak_prompt_tokens, final_result.prompt_tokens or 0)
            reply = final_result.content
            truncated = final_result.truncated

        duration = time.time() - start
        save_message("assistant", reply)
        can_continue = False
        if truncated:
            can_continue = continuation.remember("code" if "```" in reply else "text", reply, message)
        else:
            pipeline._persist_code(reply)   # el código que se mostró queda como "el último" para editarlo después

        reason = (
            f"orquestador encadenó: {' -> '.join(tools_called)}"
            if tools_called else "orquestador respondió sin necesitar herramientas"
        )
        if nudges_used:
            reason += f" (con {nudges_used} empujón/es por tool obligatoria faltante)"
        log_turn_metadata(message, provider.name, reason, duration)

        final = {
            "reply": reply,
            "provider": provider.name,
            "reason": reason,
            "used_tools": used_tools,
            "truncated": truncated,
            "can_continue": can_continue,
        }
        if peak_prompt_tokens:
            # Útil para saber si el prompt se acerca (o pasa) del num_ctx configurado.
            final["prompt_tokens"] = peak_prompt_tokens
        return "completado", final

    def _execute_calls(self, tool_calls, conversation, iteration, emit, seen_calls):
        """
        Ejecuta cada llamada (salvo las riesgosas, que quedan pendientes de
        confirmación humana) y las verifica con evidencia directa.

        'seen_calls' es un dict {(tool, args_json): output} con lo ya
        ejecutado en ESTE turno (lo administra _run, vive mientras dura el
        turno completo, no entre turnos). Si el modelo repite una llamada
        IDÉNTICA — mismo nombre y mismos argumentos, byte a byte — más de
        una vez en el mismo turno (visto en la práctica: el mismo `recall`
        con la misma query dos veces seguidas), no se vuelve a ejecutar de
        verdad: se le devuelve el mismo resultado ya obtenido, marcado
        explícitamente como repetido, para no gastar una ronda entera (de
        las MAX_TOOL_ROUNDS disponibles) en algo que ya tiene resuelto.

        Nota: esto NO detecta llamadas "casi iguales" con argumentos
        distintos (ej. dos write_code con specs parecidos pero no idénticos)
        — eso requeriría comparar significado, no solo el JSON de argumentos,
        y es más frágil de lo que vale la pena acá. Solo cubre el caso
        determinístico: exactamente la misma tool con exactamente los
        mismos argumentos.

        Devuelve (pending_confirmations, verification_failures).
        """
        pending = []
        failures = []

        for call in tool_calls:
            cache_key = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))

            if cache_key in seen_calls:
                output = seen_calls[cache_key]
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": (
                        "[Llamada repetida: ya ejecutaste EXACTAMENTE esto en este mismo turno, "
                        "no se volvió a correr — este es el mismo resultado de antes, no cambió. "
                        "No la llames de nuevo igual; usá lo que ya tenés o seguí con otra cosa.]\n"
                        + str(output)
                    ),
                })
                emit({
                    "type": "verificacion", "intento": iteration, "tool": call.name,
                    "arguments": call.arguments,
                    "ok": True,
                    "detalle": "Llamada duplicada dentro del turno — se reusó el resultado anterior sin re-ejecutar.",
                    "preview": _preview(output),
                })
                continue

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
            seen_calls[cache_key] = output
            conversation.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": str(output),
            })

            ok, verdict = verify_tool_result(call.name, call.arguments, output)
            emit({
                "type": "verificacion", "intento": iteration, "tool": call.name,
                "arguments": call.arguments,
                "ok": ok, "detalle": verdict,
                "preview": _preview(output),
            })
            if not ok:
                failures.append(f"- {call.name}: {verdict}")

        return pending, failures

    def _await_confirmation(self, message, provider, tools_called, start, pending) -> tuple[str, dict]:
        reply = (
            "Esta acción necesita tu confirmación antes de ejecutarse. "
            "Usa POST /v1/confirm-action/{id} con {\"approved\": true} para aprobarla, "
            "o {\"approved\": false} para rechazarla."
        )
        save_message("assistant", reply)
        reason = (
            f"orquestador encadenó: {' -> '.join(tools_called)} (pausado por confirmación)"
            if tools_called else "pausado por confirmación"
        )
        log_turn_metadata(message, provider.name, reason, time.time() - start)

        return "esperando_confirmacion", {
            "reply": reply,
            "provider": provider.name,
            "reason": reason,
            "used_tools": False,
            "pending_confirmations": pending,
        }
