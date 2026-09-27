import asyncio
import queue as queue_module
import re
import subprocess
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import settings
from app.core.auth import create_key, list_keys, require_api_key, require_api_key_ws, revoke_key
from app.core.db import get_connection, init_db
from app.core.seed_personality import seed
from app.core.safety import list_pending_actions, resolve_pending_action
from app.core.tools import MAX_FILE_CHARS, get_tools_schema, save_uploaded_attachment, tools_schema_to_mcp
from app.core.tasks import create_task, describe_providers, get_task, run_agent_task, run_research_task
from app.core import mcp_client, memory


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Conectar a los servidores MCP puede tardar (arrancan procesos): se hace en un
    # hilo para no bloquear el event loop de FastAPI. Si no hay mcp_servers.json, no hace nada.
    await asyncio.to_thread(mcp_client.start_from_settings)
    yield
    await asyncio.to_thread(mcp_client.manager.stop)


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# El cliente web (Fase 5) se abre como archivo local (file://) o desde otro
# dispositivo en tu red, no desde este mismo dominio — sin CORS el navegador
# bloquea la respuesta aunque el request esté bien armado y autenticado.
# No usamos cookies/credenciales, todo va por el header Authorization, así
# que abrir a "*" no compromete nada adicional (la API key sigue siendo el
# único mecanismo real de acceso).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()
seed()


# ---- Adjuntos de texto/código en el chat ----
# Whitelist de extensiones que hoy sabemos interpretar de forma directa (texto plano,
# el modelo las lee igual que cualquier mensaje). Si el cliente manda algo fuera de
# esta lista, se rechaza acá con un mensaje claro en vez de mandarlo igual al modelo
# y que falle de forma confusa más adelante.
ALLOWED_ATTACHMENT_EXTENSIONS = {
    "py", "java", "js", "jsx", "ts", "tsx", "html", "htm", "css", "scss",
    "json", "md", "markdown", "txt", "sh", "bash", "sql", "c", "h", "cpp",
    "hpp", "go", "rs", "rb", "php", "yml", "yaml", "xml", "csv", "ini",
    "toml", "kt", "swift", "cs", "vue", "svelte", "env", "log", "dockerfile",
}


class Attachment(BaseModel):
    filename: str
    content: str


class ChatRequest(BaseModel):
    message: str
    attachments: list[Attachment] = []


class MemoryRequest(BaseModel):
    content: str


class ConfirmActionRequest(BaseModel):
    approved: bool


class InstallPackageRequest(BaseModel):
    package: str


class ResearchRequest(BaseModel):
    topic: str


class SemanticMemoryRequest(BaseModel):
    content: str
    topic: str | None = None


class CreateKeyRequest(BaseModel):
    name: str


@app.get("/v1/status")
def status():
    providers = describe_providers()
    return {
        "status": "ok",
        "name": settings.app_name,
        "model": providers["general"],  # se mantiene la clave "model" por compatibilidad con clientes existentes
        "providers": providers,
    }


def _extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else filename.lower()


def _build_message_with_attachments(message: str, attachments: list[Attachment]) -> str:
    """
    Valida cada adjunto contra ALLOWED_ATTACHMENT_EXTENSIONS (server-side, nunca confiar
    solo en la validación del cliente) y lo guarda directo en el workspace en vez de pegar
    su contenido completo en el mensaje: el modelo lo lee con read_file solo si hace falta,
    y lo edita con write_file, en vez de tener que reescribir el archivo entero en cada
    respuesta (carísimo en tokens con archivos grandes, y la causa de las respuestas
    cortadas a la mitad). Si algún adjunto no está soportado, corta acá con un 415 claro.
    """
    if not attachments:
        return message

    notes = []
    for att in attachments:
        ext = _extension_of(att.filename)
        if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail=f"Formato no compatible: '.{ext}' ('{att.filename}') todavía no está habilitado para adjuntar.",
            )
        saved_name = save_uploaded_attachment(att.filename, att.content)
        notes.append(
            f"Archivo adjunto: '{saved_name}' — guardado en el workspace. Usá read_file('{saved_name}') "
            f"si necesitás ver su contenido actual, y write_file('{saved_name}', ...) para guardar tus "
            f"cambios ahí, sin reescribirlo completo dentro de tu respuesta de chat."
        )

    header = "\n\n".join(notes)
    return f"{header}\n\n{message}".strip()

@app.post("/v1/chat", dependencies=[Depends(require_api_key)])
def chat(request: ChatRequest):
    final_message = _build_message_with_attachments(request.message, request.attachments)
    task_id = create_task()
    threading.Thread(target=run_agent_task, args=(task_id, final_message), daemon=True).start()

    return {
        "task_id": task_id,
        "status": "en_progreso",
        "ws_url": f"/v1/ws/{task_id}",
        "poll_url": f"/v1/task/{task_id}",
    }


@app.get("/v1/task/{task_id}", dependencies=[Depends(require_api_key)])
def task_status(task_id: str):
    task = get_task(task_id)
    if not task:
        return {"status": "error", "detail": "No existe una tarea con ese id."}
    return {"status": task["status"], "result": task["result"]}


@app.websocket("/v1/ws/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str):
    await require_api_key_ws(websocket)  # valida ?token=... antes de aceptar la conexión
    await websocket.accept()

    task = get_task(task_id)
    if not task:
        await websocket.send_json({"type": "error", "mensaje": "No existe una tarea con ese id."})
        await websocket.close()
        return

    try:
        while True:
            try:
                event = await asyncio.to_thread(task["queue"].get, True, 30)
            except queue_module.Empty:
                await websocket.send_json({"type": "ping"})
                continue

            await websocket.send_json(event)

            if event["type"] == "final":
                break
    except WebSocketDisconnect:
        pass

    await websocket.close()


@app.post("/v1/memory", dependencies=[Depends(require_api_key)])
def add_memory(request: MemoryRequest):
    conn = get_connection()
    conn.execute("INSERT INTO facts (content) VALUES (?)", (request.content,))
    conn.commit()
    conn.close()
    return {"status": "saved", "content": request.content}


@app.get("/v1/memory", dependencies=[Depends(require_api_key)])
def list_memory():
    conn = get_connection()
    facts = conn.execute("SELECT id, content, created_at FROM facts ORDER BY id DESC").fetchall()
    conn.close()
    return {"facts": [dict(f) for f in facts]}


@app.get("/v1/router-log", dependencies=[Depends(require_api_key)])
def router_log():
    conn = get_connection()
    logs = conn.execute(
        "SELECT id, message, provider_chosen, reason, duration_seconds, created_at FROM router_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return {"logs": [dict(l) for l in logs]}


@app.get("/v1/pending-actions", dependencies=[Depends(require_api_key)])
def pending_actions():
    return {"actions": list_pending_actions()}


@app.post("/v1/confirm-action/{action_id}", dependencies=[Depends(require_api_key)])
def confirm_action(action_id: str, request: ConfirmActionRequest):
    return resolve_pending_action(action_id, request.approved)


@app.get("/v1/tools", dependencies=[Depends(require_api_key)])
def list_tools(format: str = "ollama"):
    # Catálogo completo: herramientas propias + las de los servidores MCP conectados.
    schema = get_tools_schema()
    if format == "mcp":
        return {"format": "mcp", "tools": tools_schema_to_mcp(schema)}
    return {"format": "ollama", "tools": schema}


@app.get("/v1/mcp/status", dependencies=[Depends(require_api_key)])
def mcp_status():
    """Estado de cada servidor MCP configurado (conectado, herramientas, error). Sin secretos."""
    return mcp_client.manager.status()


# ---- Fase 4: memoria semántica y research pipeline ----

@app.post("/v1/research", dependencies=[Depends(require_api_key)])
def research(request: ResearchRequest):
    """
    Dispara el pipeline de investigación (buscar -> resumir -> guardar en memoria
    semántica) como una tarea en background, igual que /v1/chat. Conéctate al
    ws_url para ver el progreso en vivo, o haz polling con poll_url.
    """
    task_id = create_task()
    threading.Thread(target=run_research_task, args=(task_id, request.topic), daemon=True).start()

    return {
        "task_id": task_id,
        "status": "en_progreso",
        "ws_url": f"/v1/ws/{task_id}",
        "poll_url": f"/v1/task/{task_id}",
    }


@app.get("/v1/research/log", dependencies=[Depends(require_api_key)])
def research_log():
    conn = get_connection()
    logs = conn.execute(
        "SELECT id, topic, summary, memory_id, sources, created_at FROM research_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return {"logs": [dict(l) for l in logs]}


@app.post("/v1/memory/semantic", dependencies=[Depends(require_api_key)])
def add_semantic_memory(request: SemanticMemoryRequest):
    """Guarda manualmente algo en memoria semántica, sin pasar por el agente."""
    metadata = {"type": "manual"}
    if request.topic:
        metadata["topic"] = request.topic
    memory_id = memory.add_memory(request.content, metadata=metadata)
    memory.enforce_retention_policy()
    return {"status": "saved", "memory_id": memory_id}


@app.get("/v1/memory/semantic/search", dependencies=[Depends(require_api_key)])
def search_semantic_memory(q: str, n: int = 4):
    return {"query": q, "results": memory.search_memory(q, n_results=n)}


@app.get("/v1/memory/semantic/stats", dependencies=[Depends(require_api_key)])
def semantic_memory_stats():
    return {"total": memory.count_memories(), "max_items": settings.memory_max_items}


@app.post("/v1/memory/semantic/cleanup", dependencies=[Depends(require_api_key)])
def cleanup_semantic_memory():
    """Fuerza la política de retención manualmente (normalmente corre sola tras cada guardado)."""
    return memory.enforce_retention_policy()


# ---- Fase 5: autenticación multi-dispositivo ----
# La PRIMERA llave se crea por CLI (python -m app.manage_keys create <nombre>),
# porque crear una llave nueva aquí requiere ya estar autenticado con una.
# Estos endpoints son para gestionar llaves adicionales una vez que ya tienes una.

@app.post("/v1/auth/keys", dependencies=[Depends(require_api_key)])
def create_api_key(request: CreateKeyRequest):
    raw_key = create_key(request.name)
    return {
        "status": "creada",
        "api_key": raw_key,
        "aviso": "Guarda esta llave ahora. No se puede volver a consultar después.",
    }


@app.get("/v1/auth/keys", dependencies=[Depends(require_api_key)])
def list_api_keys():
    return {"keys": list_keys()}


@app.post("/v1/auth/keys/{key_id}/revoke", dependencies=[Depends(require_api_key)])
def revoke_api_key(key_id: int):
    ok = revoke_key(key_id)
    if not ok:
        return {"status": "error", "detail": "No existe una llave con ese id."}
    return {"status": "revocada", "key_id": key_id}


# ---- resto igual que antes ----

PACKAGE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")


@app.post("/v1/install-package", dependencies=[Depends(require_api_key)])
def install_package(request: InstallPackageRequest):
    package = request.package.strip()

    if not package or not PACKAGE_NAME_PATTERN.match(package):
        return {"status": "error", "detail": "Nombre de paquete inválido."}

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "detail": "La instalación tardó demasiado y se canceló."}

    output = (result.stdout or "") + (result.stderr or "")

    if result.returncode != 0:
        return {"status": "error", "package": package, "output": output[-3000:]}

    return {"status": "installed", "package": package, "output": output[-3000:]}