"""
Capa de proveedores de modelos.

El Core (core.py) NO habla con Ollama, Claude ni GPT directamente: habla con
un `Provider`. Todo lo específico de cada uno (cliente, formato de mensajes,
formato de herramientas, bugs raros) vive AQUÍ y no se filtra al resto.
Así se cumple el principio #1 del plan: el Core nunca depende de un proveedor.

Proveedores incluidos:
  - OllamaProvider            -> modelos locales (privado, offline, gratis)
  - AnthropicProvider         -> Claude vía API de Anthropic
  - OpenAICompatibleProvider  -> OpenAI y todo lo que hable su mismo protocolo
                                 (OpenRouter, Mistral, Groq, LM Studio, vLLM...)
                                 cambiando solo base_url / api_key / model.
  - GeminiProvider            -> Gemini vía API de Google AI (generateContent)

Para agregar otro: una clase con `name: str` y
    chat(messages, tools=None, allow_tools=True) -> ChatResult
y registrarla en `build_provider`.

FORMATO NEUTRAL de conversación (el que usa el Core):
    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str,
        "tool_calls": [{"id": str, "function": {"name": str, "arguments": dict}}]}   # tool_calls opcional
    {"role": "tool", "content": str, "tool_call_id": str, "name": str}
Cada proveedor traduce de/hacia su API. `tools` llega en formato JSON-Schema
estilo function-calling (el de TOOLS_SCHEMA).

`allow_tools=False` significa "responde con texto, sin pedir más herramientas":
lo usa el Core en la pasada final, cuando la conversación ya contiene
resultados de herramientas.
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

import httpx
from ollama import Client

from app.config import settings

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

# Reintentos ante límites de velocidad (429) o saturación temporal del proveedor.
_RETRY_STATUS = {429, 502, 503, 529}
_MAX_RETRIES = 4              # reintentos extra tras el primer intento
_BASE_BACKOFF_SECONDS = 2.0   # 2s, 4s, 8s, 16s (si el proveedor no manda Retry-After)
_MAX_WAIT_SECONDS = 20.0      # tope de espera por reintento


class ProviderError(Exception):
    """Fallo al hablar con un proveedor (red, API key inválida, límite, etc.)."""


class ProviderConfigError(ProviderError):
    """El proveedor está mal configurado (falta API key, modelo, nombre desconocido...)."""


def strip_think_blocks(text: str) -> str:
    """
    Quita bloques <think>...</think> que qwen3 a veces genera aunque se le pida
    think=False (bug conocido de Ollama). Si el bloque quedó abierto (se cortó
    la generación a medio razonamiento), descarta desde ahí: no hay nada útil.
    """
    text = _THINK_BLOCK_RE.sub("", text or "")
    if "<think>" in text:
        text = text.split("<think>")[0]
    return text.strip()


def strip_tool_call_blocks(text: str) -> str:
    """
    Quita bloques <tool_call>...</tool_call> que Qwen (y derivados) a veces
    generan como TEXTO en vez de usar el campo nativo de tool-calling de
    Ollama — sobre todo visto en la pasada final con allow_tools=False:
    el modelo "quiere" seguir pidiendo una herramienta pero no puede, y en
    vez de responder en texto puro deja el JSON crudo envuelto en estos
    tags como si fuera su respuesta. Sin este filtro eso le llega directo
    al usuario en el chat. Si el bloque quedó abierto (se cortó a la
    mitad), descarta desde ahí: no hay nada útil después de eso.
    """
    text = _TOOL_CALL_BLOCK_RE.sub("", text or "")
    if "<tool_call>" in text:
        text = text.split("<tool_call>")[0]
    return text.strip()


def _new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


@dataclass
class ToolCall:
    name: str
    arguments: dict
    # Los proveedores que dan id propio (Claude, OpenAI) lo sobreescriben; con los
    # que no (Ollama) se genera uno, porque el formato neutral siempre lo lleva.
    id: str = field(default_factory=_new_call_id)
    # Solo lo usa Gemini 3 (thoughtSignature): hay que devolverle exactamente la
    # misma firma que vino en la respuesta cuando le mandamos el resultado de la
    # tool, si no tira 400. Los demás proveedores la ignoran.
    signature: Optional[str] = None


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: Optional[int] = None  # tokens de entrada, si el proveedor lo reporta
    truncated: bool = False  # True si el proveedor cortó por tope de tokens de salida, no porque el modelo terminó solo

    def as_message(self) -> dict:
        """El mensaje del asistente en formato neutral, listo para re-inyectar."""
        message: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": c.id,
                    "function": {"name": c.name, "arguments": c.arguments},
                    **({"signature": c.signature} if c.signature else {}),
                }
                for c in self.tool_calls
            ]
        return message


class Provider(Protocol):
    name: str

    def chat(
        self, messages: list[dict], tools: Optional[list[dict]] = None, allow_tools: bool = True
    ) -> ChatResult:
        ...

    def chat_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        allow_tools: bool = True,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> ChatResult:
        ...


def _field(obj, key, default=None):
    """Lee obj[key] tanto de dicts como de los objetos de respuesta de ollama."""
    try:
        value = obj[key]
    except (KeyError, TypeError, AttributeError):
        return default
    return default if value is None else value

def _parse_fallback_tool_call(content: str, tools: Optional[list]) -> Optional[ToolCall]:
    """
    Modelos locales chicos a veces no usan el campo nativo de tool_calls de
    Ollama: en cambio, devuelven la llamada como texto en 'content', de dos
    formas vistas en la práctica:
      1. El content ENTERO es un JSON plano {"name":..., "arguments":...}
         (a veces envuelto en ```json ... ```).
      2. Qwen y derivados a veces usan su propio formato de texto
         <tool_call>{"name":..., "arguments":...}</tool_call>, a veces con
         más texto antes o después (ej. el modelo escribe algo y DESPUÉS
         decide pedir una tool con este formato).
    Ambas se detectan acá y se convierten en un ToolCall real para que el
    Agent Loop la ejecute de verdad, en vez de descartar el intento o
    mostrarle el JSON/tags crudos al usuario como si fuera la respuesta
    final (ver strip_tool_call_blocks, que limpia lo que esto no logra
    reconocer o lo que llega en la pasada final con allow_tools=False).
    """
    if not content or not tools:
        return None
    text = content.strip()

    match = _TOOL_CALL_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()
    elif text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()

    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    name, arguments = data.get("name"), data.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None
    valid_names = {t["function"]["name"] for t in tools if isinstance(t, dict) and "function" in t}
    if name not in valid_names:
        return None
    return ToolCall(name=name, arguments=arguments)
# =============================================================================
# Ollama (local)
# =============================================================================

def _to_ollama_messages(messages: list[dict]) -> list[dict]:
    """
    Ollama no usa ids de llamada: los quita para mandar exactamente el mismo
    formato que ya funcionaba antes de existir los ids en el formato neutral.
    """
    out = []
    for message in messages:
        message = dict(message)
        if message.get("images"):  # Ollama quiere una lista de strings base64
            message["images"] = [i["data"] if isinstance(i, dict) else i for i in message["images"]]
        if message.get("role") == "tool":
            message.pop("tool_call_id", None)
            message.pop("name", None)
        elif message.get("tool_calls"):
            message["tool_calls"] = [{"function": c["function"]} for c in message["tool_calls"]]
        out.append(message)
    return out


class OllamaProvider:
    def __init__(
        self,
        model: Optional[str] = None,
        num_ctx: Optional[int] = None,
        num_predict: Optional[int] = None,
        timeout: Optional[int] = None,
    ):
        model = model or settings.ollama_model
        self.name = f"ollama:{model}"
        self._model = model
        self._num_ctx = num_ctx or settings.ollama_num_ctx
        self._num_predict = num_predict or settings.ollama_num_predict
        # Timeout explícito: si Ollama se cuelga, falla con una excepción clara
        # en vez de dejar la tarea para siempre en "en_progreso".
        self._client = Client(timeout=timeout or settings.ollama_timeout_seconds)

    def with_limits(self, num_predict: int) -> "OllamaProvider":
        """Copia liviana (mismo cliente y modelo) con otro tope de tokens de salida,
        p. ej. para el planificador, que solo devuelve un JSON corto."""
        clone = object.__new__(OllamaProvider)
        clone.__dict__.update(self.__dict__)
        clone._num_predict = num_predict
        return clone

    def _log_usage(self, prompt_tokens, eval_tokens) -> None:
        """Deja en el log cuánto del num_ctx se usó. Si el prompt llega a ~85% del contexto,
        Ollama empieza a recortar en silencio lo más viejo (system prompt / tools)."""
        if not prompt_tokens:
            return
        used = prompt_tokens + (eval_tokens or 0)
        pct = 100.0 * used / max(self._num_ctx, 1)
        level = logging.WARNING if used >= 0.85 * self._num_ctx else logging.INFO
        logging.getLogger("elipse").log(
            level, "ollama ctx: prompt=%s salida=%s de %s (%.0f%%)",
            prompt_tokens, eval_tokens or 0, self._num_ctx, pct)

    def chat(self, messages, tools=None, allow_tools=True) -> ChatResult:
        kwargs = {}
        if tools and allow_tools:
            kwargs["tools"] = tools

        response = self._client.chat(
            model=self._model,
            messages=_to_ollama_messages(messages),
            think=False,
            options={"num_ctx": self._num_ctx, "num_predict": self._num_predict},
            **kwargs,
        )

        message = response["message"]
        tool_calls = [
            ToolCall(
                name=call["function"]["name"],
                arguments=dict(call["function"]["arguments"] or {}),
            )
            for call in (_field(message, "tool_calls") or [])
        ]
        content = strip_think_blocks(_field(message, "content", ""))

        if not tool_calls and tools and allow_tools:
            fallback = _parse_fallback_tool_call(content, tools)
            if fallback:
                tool_calls = [fallback]
                content = ""

        if not tool_calls:
            # Limpieza final: si el modelo dejó un <tool_call>...</tool_call> como
            # texto y no se pudo (o no se debía — allow_tools=False, la pasada
            # salvavidas) convertir en una llamada real, que al menos no le
            # llegue crudo al usuario en la respuesta.
            content = strip_tool_call_blocks(content)

        self._log_usage(_field(response, "prompt_eval_count"), _field(response, "eval_count"))
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=_field(response, "prompt_eval_count"),
            truncated=_field(response, "done_reason") == "length",
        )

    def chat_stream(self, messages, tools=None, allow_tools=True, on_delta=None) -> ChatResult:
        kwargs = {}
        if tools and allow_tools:
            kwargs["tools"] = tools

        stream = self._client.chat(
            model=self._model,
            messages=_to_ollama_messages(messages),
            think=False,
            options={"num_ctx": self._num_ctx, "num_predict": self._num_predict},
            stream=True,
            **kwargs,
        )

        text_parts = []
        tool_calls = []
        prompt_tokens = None
        eval_tokens = None
        done_reason = None

        # Filtro de streaming para el bug conocido de <think> (qwen3 con think=False):
        # no mandamos nada por on_delta hasta saber si el bloque arranca con <think>;
        # si arranca, lo tragamos entero y recién emitimos lo que venga después.
        buf = {"text": "", "in_think": False, "decided": False}

        def handle_piece(piece: str):
            buf["text"] += piece
            if not buf["decided"]:
                stripped = buf["text"].lstrip()
                if stripped and not "<think>".startswith(stripped[:7]):
                    buf["decided"] = True
                elif stripped.startswith("<think>"):
                    buf["decided"] = True
                    buf["in_think"] = True
                    buf["text"] = stripped[len("<think>"):]
                else:
                    return  # todavía no sabemos si arranca con <think>, esperar más texto
            if buf["in_think"]:
                if "</think>" not in buf["text"]:
                    return
                buf["text"] = buf["text"].split("</think>", 1)[1]
                buf["in_think"] = False
            if buf["text"]:
                if on_delta:
                    on_delta(buf["text"])
                buf["text"] = ""

        for part in stream:
            message = part["message"]
            piece = _field(message, "content", "")
            if piece:
                text_parts.append(piece)
                handle_piece(piece)
            calls = _field(message, "tool_calls")
            if calls:
                tool_calls = [
                    ToolCall(name=c["function"]["name"], arguments=dict(c["function"]["arguments"] or {}))
                    for c in calls
                ]
            if _field(part, "done"):
                prompt_tokens = _field(part, "prompt_eval_count")
                eval_tokens = _field(part, "eval_count")
                done_reason = _field(part, "done_reason")

        content = strip_think_blocks("".join(text_parts))
        if not tool_calls and tools and allow_tools:
            fallback = _parse_fallback_tool_call(content, tools)
            if fallback:
                tool_calls = [fallback]
                content = ""
                # Nota: si un modelo local chico cae en este fallback (JSON de tool
                # call como texto plano en vez de tool_calls nativo), ese JSON ya se
                # habrá mostrado en pantalla vía on_delta antes de detectarlo acá.
                # Caso borde de modelos sin tool-calling nativo confiable.

        if not tool_calls:
            # Misma limpieza final que en chat(): si quedó un <tool_call> como
            # texto sin poder convertirse en llamada real (típicamente en la
            # pasada salvavidas con allow_tools=False), no dejarlo crudo en
            # el texto guardado/devuelto. Nota: al igual que con el fallback
            # de arriba, en streaming puede haberse mostrado ya en pantalla
            # antes de limpiarlo acá — ver core.py para el porqué de esta
            # limitación conocida en modelos sin tool-calling confiable.
            content = strip_tool_call_blocks(content)

        self._log_usage(prompt_tokens, eval_tokens)
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            truncated=done_reason == "length",
        )


# =============================================================================
# Base para proveedores HTTP (nube)
# =============================================================================

def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else body
        if isinstance(error, dict):
            return str(error.get("message") or error)[:500]
        return str(error)[:500]
    except ValueError:
        return response.text[:500]


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    """Segundos a esperar antes de reintentar: respeta Retry-After si viene, si no, backoff exponencial."""
    raw = response.headers.get("retry-after")
    try:
        wait = float(raw) if raw else None
    except ValueError:
        wait = None
    if wait is None:
        wait = _BASE_BACKOFF_SECONDS * (2 ** attempt)
    return min(max(wait, 1.0), _MAX_WAIT_SECONDS)


class _HTTPProvider:
    label = "proveedor"

    def __init__(self, timeout: Optional[int] = None, http_client: Optional[httpx.Client] = None):
        self._http = http_client or httpx.Client(timeout=timeout or settings.cloud_timeout_seconds)

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        response = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = self._http.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                raise ProviderError(f"No se pudo contactar a {self.label}: {e}") from e

            if response.status_code not in _RETRY_STATUS or attempt == _MAX_RETRIES:
                break
            time.sleep(_retry_wait(response, attempt))

        if response.status_code >= 400:
            hint = ""
            if response.status_code == 429:
                hint = (
                    f" (límite de velocidad; se reintentó {_MAX_RETRIES} veces sin éxito. "
                    "Revisa la sección Limits de tu cuenta: puede ser el tope mensual de tokens.)"
                )
            raise ProviderError(
                f"{self.label} respondió {response.status_code}: {_error_detail(response)}{hint}"
            )
        try:
            return response.json()
        except ValueError as e:
            raise ProviderError(f"{self.label} devolvió una respuesta que no es JSON válido.") from e


# =============================================================================
# Anthropic (Claude)
# =============================================================================

def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """
    Formato neutral -> Messages API de Anthropic. Devuelve (system, messages).

    Diferencias que hay que resolver:
      - 'system' es un parámetro aparte, no un mensaje.
      - Las llamadas a herramientas son bloques 'tool_use' dentro del mensaje del
        asistente, y los resultados son bloques 'tool_result' dentro de un mensaje
        de USUARIO que referencia el id. Varios resultados seguidos van juntos.
      - No se aceptan bloques de texto vacíos.
      - Mensajes consecutivos del mismo rol se fusionan (ej. resultados de
        herramientas + el mensaje de reintento del Core).
      - La conversación debe empezar con un mensaje de usuario (el historial
        recortado a N mensajes puede empezar con uno del asistente: se descarta).
    """
    system_parts: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list[dict]):
        blocks = [b for b in blocks if not (b["type"] == "text" and not b["text"].strip())]
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message["role"]
        content = message.get("content") or ""

        if role == "system":
            if content.strip():
                system_parts.append(content)

        elif role == "user":
            blocks = [
                {"type": "image", "source": {"type": "base64", "media_type": i["mime"], "data": i["data"]}}
                for i in message.get("images") or []
            ]
            push("user", blocks + [{"type": "text", "text": content}])

        elif role == "assistant":
            blocks = [{"type": "text", "text": content}]
            for call in message.get("tool_calls") or []:
                blocks.append({
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["function"]["name"],
                    "input": call["function"]["arguments"],
                })
            push("assistant", blocks)

        elif role == "tool":
            if not message.get("tool_call_id"):
                raise ProviderError("Mensaje de herramienta sin 'tool_call_id': no se puede enviar a Claude.")
            push("user", [{
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "content": content,
            }])

    while out and out[0]["role"] == "assistant":
        out.pop(0)

    return "\n\n".join(system_parts), out


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}},
        }
        for t in tools
    ]


class AnthropicProvider(_HTTPProvider):
    label = "Anthropic"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        base_url: str = "https://api.anthropic.com",
        timeout: Optional[int] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        if not api_key:
            raise ProviderConfigError("Falta ANTHROPIC_API_KEY en el .env para usar el proveedor 'anthropic'.")
        super().__init__(timeout, http_client)
        self.name = f"anthropic:{model}"
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._url = base_url.rstrip("/") + "/v1/messages"

    def chat(self, messages, tools=None, allow_tools=True) -> ChatResult:
        system, converted = _to_anthropic_messages(messages)
        if not converted:
            raise ProviderError("No hay ningún mensaje de usuario que enviar a Claude.")

        body: dict = {"model": self._model, "max_tokens": self._max_tokens, "messages": converted}
        if system:
            body["system"] = system
        if tools:
            # Con tool_use/tool_result en el historial hay que seguir declarando las
            # herramientas; para pedir solo texto se usa tool_choice "none".
            body["tools"] = _to_anthropic_tools(tools)
            if not allow_tools:
                body["tool_choice"] = {"type": "none"}

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }
        data = self._post(self._url, headers, body)

        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [
            ToolCall(name=b["name"], arguments=b.get("input") or {}, id=b["id"])
            for b in blocks
            if b.get("type") == "tool_use"
        ]

        usage = data.get("usage") or {}
        prompt_tokens = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        ) or None

        return ChatResult(
            content=text.strip(),
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            truncated=data.get("stop_reason") == "max_tokens",
        )

    def chat_stream(self, messages, tools=None, allow_tools=True, on_delta=None) -> ChatResult:
        system, converted = _to_anthropic_messages(messages)
        if not converted:
            raise ProviderError("No hay ningún mensaje de usuario que enviar a Claude.")

        body: dict = {"model": self._model, "max_tokens": self._max_tokens, "messages": converted, "stream": True}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = _to_anthropic_tools(tools)
            if not allow_tools:
                body["tool_choice"] = {"type": "none"}

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }

        text_parts: list[str] = []
        blocks: dict[int, dict] = {}
        stop_reason = None
        usage: dict = {}

        # Nota: a diferencia de _post, este camino streaming no reintenta en
        # 429/5xx (complicaría manejar un stream a medio abrir). Si hace falta,
        # se puede sumar un reintento antes de abrir la conexión.
        with self._http.stream("POST", self._url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                response.read()
                raise ProviderError(_error_detail(response))
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                event = json.loads(payload)
                etype = event.get("type")

                if etype == "message_start":
                    usage = event["message"].get("usage", {})
                elif etype == "content_block_start":
                    blocks[event["index"]] = dict(event["content_block"], json="")
                elif etype == "content_block_delta":
                    delta = event["delta"]
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta["text"])
                        if on_delta:
                            on_delta(delta["text"])
                    elif delta.get("type") == "input_json_delta":
                        idx = event["index"]
                        blocks[idx]["json"] += delta.get("partial_json", "")
                elif etype == "message_delta":
                    stop_reason = event["delta"].get("stop_reason")
                    if event.get("usage", {}).get("output_tokens") is not None:
                        usage["output_tokens"] = event["usage"]["output_tokens"]
                elif etype == "error":
                    raise ProviderError(str(event.get("error")))

        calls = []
        for b in blocks.values():
            if b.get("type") == "tool_use":
                try:
                    args = json.loads(b.get("json") or "{}")
                except ValueError:
                    args = {}
                calls.append(ToolCall(name=b["name"], arguments=args, id=b["id"]))

        prompt_tokens = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        ) or None

        return ChatResult(
            content="".join(text_parts).strip(),
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            truncated=stop_reason == "max_tokens",
        )


# =============================================================================
# OpenAI y compatibles (OpenRouter, Mistral, Groq, LM Studio, vLLM, ...)
# =============================================================================

def _to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for message in messages:
        role = message["role"]
        content = message.get("content") or ""

        if role == "assistant" and message.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["function"]["name"],
                            "arguments": json.dumps(c["function"]["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in message["tool_calls"]
                ],
            })
        elif role == "tool":
            if not message.get("tool_call_id"):
                raise ProviderError("Mensaje de herramienta sin 'tool_call_id': no se puede enviar.")
            out.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": content})
        elif role == "user" and message.get("images"):
            parts = [{"type": "text", "text": content}] + [
                {"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}}
                for i in message["images"]
            ]
            out.append({"role": "user", "content": parts})
        else:
            out.append({"role": role, "content": content})
    return out


def _parse_openai_arguments(raw) -> dict:
    """Los argumentos llegan como string JSON; si vienen rotos se marcan para que la
    verificación del Core lo detecte y el modelo pueda corregirlo en el reintento."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_argumentos_invalidos": raw}
    except ValueError:
        return {"_argumentos_invalidos": raw}


class OpenAICompatibleProvider(_HTTPProvider):
    label = "OpenAI"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: Optional[int] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        if not model:
            raise ProviderConfigError("Falta OPENAI_MODEL en el .env para usar el proveedor 'openai'.")
        if not api_key and "api.openai.com" in base_url:
            raise ProviderConfigError("Falta OPENAI_API_KEY en el .env para usar el proveedor 'openai'.")
        super().__init__(timeout, http_client)
        self.name = f"openai:{model}"
        self._api_key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/chat/completions"

    def chat(self, messages, tools=None, allow_tools=True) -> ChatResult:
        body: dict = {"model": self._model, "messages": _to_openai_messages(messages)}
        if tools:
            body["tools"] = tools  # TOOLS_SCHEMA ya está en el formato de OpenAI
            if not allow_tools:
                body["tool_choice"] = "none"

        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"

        data = self._post(self._url, headers, body)

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.label} devolvió una respuesta sin 'choices'.")
        message = choices[0].get("message") or {}

        calls = [
            ToolCall(
                name=c["function"]["name"],
                arguments=_parse_openai_arguments(c["function"].get("arguments")),
                id=c.get("id") or _new_call_id(),
            )
            for c in (message.get("tool_calls") or [])
        ]

        return ChatResult(
            content=strip_think_blocks(message.get("content") or ""),
            tool_calls=calls,
            prompt_tokens=(data.get("usage") or {}).get("prompt_tokens"),
            truncated=choices[0].get("finish_reason") == "length",
        )

    def chat_stream(self, messages, tools=None, allow_tools=True, on_delta=None) -> ChatResult:
        body: dict = {"model": self._model, "messages": _to_openai_messages(messages), "stream": True}
        if tools:
            body["tools"] = tools
            if not allow_tools:
                body["tool_choice"] = "none"

        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"

        text_parts: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = None
        prompt_tokens = None

        with self._http.stream("POST", self._url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                response.read()
                raise ProviderError(_error_detail(response))
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                chunk = json.loads(payload)
                usage = chunk.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens")
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                    if on_delta:
                        on_delta(delta["content"])
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    acc = tool_calls_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

        calls = [
            ToolCall(
                name=acc["name"],
                arguments=_parse_openai_arguments(acc["arguments"]),
                id=acc["id"] or _new_call_id(),
            )
            for acc in tool_calls_acc.values()
        ]

        return ChatResult(
            content=strip_think_blocks("".join(text_parts)),
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            truncated=finish_reason == "length",
        )


# =============================================================================
# Gemini (Google AI)
# =============================================================================

def _to_gemini_messages(messages: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """
    Formato neutral -> generateContent de Gemini.
    Devuelve (system_instruction | None, contents).

    Diferencias:
      - 'system' va en systemInstruction, no en contents.
      - el rol del asistente se llama 'model'.
      - tool_calls son partes functionCall; tool results son functionResponse
        dentro de un mensaje de rol 'user'.
      - mensajes consecutivos del mismo rol se fusionan (Gemini lo exige).
    """
    system_parts: list[str] = []
    contents: list[dict] = []

    def push(role: str, parts: list[dict]):
        parts = [p for p in parts if p]
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": list(parts)})

    for message in messages:
        role = message["role"]
        content = message.get("content") or ""

        if role == "system":
            if content.strip():
                system_parts.append(content)

        elif role == "user":
            image_parts = [{"inlineData": {"mimeType": i["mime"], "data": i["data"]}} for i in message.get("images") or []]
            push("user", image_parts + [{"text": content}])

        elif role == "assistant":
            parts: list[dict] = []
            if content and content.strip():
                parts.append({"text": content})
            for call in message.get("tool_calls") or []:
                part: dict = {
                    "functionCall": {
                        "name": call["function"]["name"],
                        "args": call["function"]["arguments"] or {},
                    }
                }
                # Algunos modelos de Gemini devuelven id; lo reinyectamos si existe.
                if call.get("id"):
                    part["functionCall"]["id"] = call["id"]
                # Gemini 3: si esta tool call vino con thoughtSignature, hay que
                # devolvérsela tal cual al reenviar el historial, o tira 400.
                if call.get("signature"):
                    part["thoughtSignature"] = call["signature"]
                parts.append(part)
            push("model", parts)

        elif role == "tool":
            name = message.get("name") or "tool"
            # Gemini espera un objeto JSON en response, no un string suelto.
            response_payload: dict
            try:
                parsed = json.loads(content) if content else {}
                response_payload = parsed if isinstance(parsed, dict) else {"result": content}
            except (ValueError, TypeError):
                response_payload = {"result": content}
            fn_resp: dict = {"name": name, "response": response_payload}
            if message.get("tool_call_id"):
                fn_resp["id"] = message["tool_call_id"]
            push("user", [{"functionResponse": fn_resp}])

    # Gemini exige que contents empiece por user.
    while contents and contents[0]["role"] == "model":
        contents.pop(0)

    system_instruction = (
        {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    )
    return system_instruction, contents


def _to_gemini_tools(tools: list[dict]) -> list[dict]:
    declarations = []
    for t in tools:
        fn = t.get("function") or {}
        parameters = fn.get("parameters") or {"type": "object", "properties": {}}
        # Gemini no siempre tolera additionalProperties; lo quitamos si está.
        if isinstance(parameters, dict):
            parameters = dict(parameters)
            parameters.pop("additionalProperties", None)
        declarations.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": parameters,
        })
    return [{"functionDeclarations": declarations}] if declarations else []


class GeminiProvider(_HTTPProvider):
    label = "Gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 8192,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: Optional[int] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        if not api_key:
            raise ProviderConfigError("Falta GEMINI_API_KEY en el .env para usar el proveedor 'gemini'.")
        if not model:
            raise ProviderConfigError("Falta GEMINI_MODEL en el .env para usar el proveedor 'gemini'.")
        super().__init__(timeout, http_client)
        self.name = f"gemini:{model}"
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._url = (
            base_url.rstrip("/")
            + f"/models/{model}:generateContent"
        )

    def chat(self, messages, tools=None, allow_tools=True) -> ChatResult:
        system_instruction, contents = _to_gemini_messages(messages)
        if not contents:
            raise ProviderError("No hay ningún mensaje de usuario que enviar a Gemini.")

        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self._max_tokens},
        }
        if system_instruction:
            body["systemInstruction"] = system_instruction
        if tools:
            body["tools"] = _to_gemini_tools(tools)
            body["toolConfig"] = {
                "functionCallingConfig": {
                    "mode": "AUTO" if allow_tools else "NONE",
                }
            }

        headers = {
            "content-type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        data = self._post(self._url, headers, body)

        candidates = data.get("candidates") or []
        if not candidates:
            # A veces Gemini bloquea por safety y no manda candidates.
            feedback = data.get("promptFeedback") or {}
            raise ProviderError(
                f"Gemini no devolvió candidatos. promptFeedback={feedback!r}"
            )

        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        for part in parts:
            if "text" in part and part["text"]:
                text_chunks.append(part["text"])
            fc = part.get("functionCall")
            if fc:
                calls.append(ToolCall(
                    name=fc.get("name") or "",
                    arguments=dict(fc.get("args") or {}),
                    id=fc.get("id") or _new_call_id(),
                    signature=part.get("thoughtSignature"),
                ))

        usage = data.get("usageMetadata") or {}
        prompt_tokens = usage.get("promptTokenCount")

        return ChatResult(
            content=strip_think_blocks("".join(text_chunks).strip()),
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            truncated=candidates[0].get("finishReason") == "MAX_TOKENS",
        )

    def chat_stream(self, messages, tools=None, allow_tools=True, on_delta=None) -> ChatResult:
        system_instruction, contents = _to_gemini_messages(messages)
        if not contents:
            raise ProviderError("No hay ningún mensaje de usuario que enviar a Gemini.")

        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self._max_tokens},
        }
        if system_instruction:
            body["systemInstruction"] = system_instruction
        if tools:
            body["tools"] = _to_gemini_tools(tools)
            body["toolConfig"] = {
                "functionCallingConfig": {"mode": "AUTO" if allow_tools else "NONE"},
            }

        headers = {"content-type": "application/json", "x-goog-api-key": self._api_key}
        # Mismo endpoint que chat(), pero streamGenerateContent + alt=sse en vez
        # de generateContent: Gemini devuelve la respuesta en varios chunks SSE
        # en lugar de uno solo.
        stream_url = self._url.replace(":generateContent", ":streamGenerateContent") + "?alt=sse"

        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        prompt_tokens = None
        finish_reason = None
        last_feedback = None
        got_candidates = False

        with self._http.stream("POST", stream_url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                response.read()
                raise ProviderError(_error_detail(response))
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                chunk = json.loads(payload)

                candidates = chunk.get("candidates") or []
                if candidates:
                    got_candidates = True
                    cand = candidates[0]
                    if cand.get("finishReason"):
                        finish_reason = cand["finishReason"]
                    for part in (cand.get("content") or {}).get("parts") or []:
                        if part.get("text"):
                            text_chunks.append(part["text"])
                            if on_delta:
                                on_delta(part["text"])
                        fc = part.get("functionCall")
                        if fc:
                            calls.append(ToolCall(
                                name=fc.get("name") or "",
                                arguments=dict(fc.get("args") or {}),
                                id=fc.get("id") or _new_call_id(),
                                signature=part.get("thoughtSignature"),
                            ))
                else:
                    last_feedback = chunk.get("promptFeedback") or last_feedback

                usage = chunk.get("usageMetadata")
                if usage and usage.get("promptTokenCount") is not None:
                    prompt_tokens = usage["promptTokenCount"]

        if not got_candidates:
            # Igual que en chat(): a veces Gemini bloquea por safety y no manda candidates.
            raise ProviderError(f"Gemini no devolvió candidatos. promptFeedback={last_feedback!r}")

        return ChatResult(
            content=strip_think_blocks("".join(text_chunks).strip()),
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            truncated=finish_reason == "MAX_TOKENS",
        )


# =============================================================================
# Fábrica: nombre (de la configuración) -> proveedor
# =============================================================================

def _pick(role: str, role_field: str, general_field: str):
    """
    Valor efectivo de un campo para un rol: el override del rol si no está
    vacío, si no el valor general. 'general' nunca tiene override propio,
    así que siempre devuelve directamente el campo general.
    """
    if role != "general":
        role_value = getattr(settings, role_field, "")
        if role_value:
            return role_value
    return getattr(settings, general_field)


def build_role_provider(role: str) -> Optional["Provider"]:
    """
    Arma el proveedor de un ROL ("general", "code", "research", "vision"), no de un
    nombre de proveedor suelto. Esto es lo que usan tasks.py y research.py
    para saber con qué piensa cada tipo de tarea.

    - "general" siempre construye algo: usa DEFAULT_PROVIDER tal cual.
    - "code" / "research" heredan TODO de "general" si "{ROL}_PROVIDER" está
      vacío en la config -> devuelve None (el llamador debe usar el proveedor
      general en ese caso, sin crear una instancia duplicada).
    - Si el rol sí define un proveedor propio, cada campo suyo que esté vacío
      hereda su equivalente general (ver `_pick`).
    """
    if role == "general":
        provider_name = settings.default_provider
    else:
        provider_name = getattr(settings, f"{role}_provider", "").strip()
        if not provider_name:
            return None

    key = provider_name.strip().lower()

    if key == "ollama":
        model = _pick(role, f"{role}_ollama_model", "ollama_model")
        if role == "vision":  # contexto y salida propios: la imagen ocupa cientos de tokens
            return OllamaProvider(model=model, num_ctx=settings.vision_num_ctx,
                                  num_predict=settings.vision_num_predict)
        return OllamaProvider(model=model)

    if key == "anthropic":
        return AnthropicProvider(
            api_key=_pick(role, f"{role}_anthropic_api_key", "anthropic_api_key"),
            model=_pick(role, f"{role}_anthropic_model", "anthropic_model"),
            max_tokens=settings.anthropic_max_tokens,
            base_url=settings.anthropic_base_url,
        )

    if key == "openai":
        return OpenAICompatibleProvider(
            api_key=_pick(role, f"{role}_openai_api_key", "openai_api_key"),
            model=_pick(role, f"{role}_openai_model", "openai_model"),
            base_url=_pick(role, f"{role}_openai_base_url", "openai_base_url"),
        )

    if key == "gemini":
        return GeminiProvider(
            api_key=_pick(role, f"{role}_gemini_api_key", "gemini_api_key"),
            model=_pick(role, f"{role}_gemini_model", "gemini_model"),
            max_tokens=settings.gemini_max_tokens,
            base_url=settings.gemini_base_url,
        )

    raise ProviderConfigError(
        f"Proveedor desconocido para el rol '{role}': '{provider_name}'. "
        "Opciones válidas: ollama, anthropic, openai, gemini."
    )


def build_provider(name: str) -> Provider:
    key = (name or "").strip().lower()

    if key == "ollama":
        return OllamaProvider()

    if key == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            base_url=settings.anthropic_base_url,
        )

    if key == "openai":
        return OpenAICompatibleProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
        )

    if key == "gemini":
        return GeminiProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            max_tokens=settings.gemini_max_tokens,
            base_url=settings.gemini_base_url,
        )

    raise ProviderConfigError(
        f"Proveedor desconocido: '{name}'. Opciones válidas: ollama, anthropic, openai, gemini."
    )