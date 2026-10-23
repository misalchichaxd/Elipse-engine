"""
Continuación de respuestas cortadas + guardas de turno.

Problema que resuelve: cuando una respuesta se corta por límite de longitud (código a medias, texto a medias),
el cliente mandaba un prompt larguísimo ("Tu respuesta anterior se cortó... estos son los últimos 500
caracteres...") como si fuera un mensaje cualquiera, y el backend no sabía qué se había cortado: el planificador
lo interpretaba como un pedido nuevo. Ahora el SERVIDOR recuerda qué quedó pendiente (workspace/_pendiente.json) y
basta un mensaje corto («sigue», «continúa», «...»).

Cómo funciona:
  - core.py llama remember() cuando un turno terminó truncado (código del pipeline o texto del Agent Loop).
  - En el turno siguiente, handle_turn() mira el mensaje:
        «sigue»/«continúa»/«...» + hay pendiente  -> se continúa SOLO esa pieza (sin planificar, sin tools)
        solo puntuación («...», «?») sin pendiente -> respuesta corta, sin tools ni modelo (antes el orquestador
                                                       alucinaba investigaciones sin relación)
        cualquier otro mensaje                     -> el pendiente caduca (ya es otro tema) y sigue el flujo normal
  - Los documentos PDF/Word NO se continúan: se editan por partes (docedit.py).

Lo que devuelve (PipelineOutcome):
  - reply: SOLO la parte nueva (el cliente la pega al mensaje anterior). Si por alguna razón no se puede
    garantizar que calce, reply trae el texto completo y replace_previous=True (el cliente reemplaza).
  - history_note: lo que se guarda en el historial (una línea, no el código otra vez: el historial es corto y el
    código completo ya vive en workspace/code/_ultimo.json).
"""

import json
import re
import time
from pathlib import Path
from typing import Optional

from app.config import settings
from app.core import pipeline as P
from app.core.providers import strip_think_blocks

PENDING_TTL_SECONDS = 60 * 60          # un pendiente de hace más de una hora ya no se continúa
TAIL_CHARS = 1500                      # cuánto del final se le muestra al modelo al continuar texto
MAX_PARTIAL_CHARS = 120_000

_PUNCT_ONLY_RE = re.compile(r"^[\s.…?!¿¡,;:\-_*~'\"]*$")
_CONTINUE_RE = re.compile(
    r"^\s*(?:(?:por\s+favor|porfa|pls)\s+)?"
    r"(?:sigue|sigue\s+nom[aá]s|segu[ií]|segu[ií]\s+nom[aá]s|continu[aáe]\w*|contin[uú]a\w*|continuar|seguir|dale|dale\s+sigue|"
    r"completa(?:lo|la)?|termina(?:lo|la)?|acaba(?:lo|la)?|y\s+el\s+resto|el\s+resto|el\s+final)"
    r"(?:\s+(?:por\s+favor|porfa|pls|nom[aá]s|con\s+eso|ah[ií]|desde\s+ah[ií]))*\s*[.!¡?¿…]*\s*$", re.I)


# ---------------------------------------------------------------- estado en disco

def _pending_file() -> Path:
    root = (Path(__file__).parent.parent.parent / settings.workspace_dir).resolve()
    return root / "_pendiente.json"


def remember(kind: str, partial: str, request: str = "") -> bool:
    """Guarda lo que quedó a medias. kind: 'code' (reply con bloque ```), 'text' (respuesta de texto)."""
    try:
        partial = partial or ""
        if kind not in ("code", "text") or not partial.strip() or len(partial) > MAX_PARTIAL_CHARS:
            return False
        f = _pending_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps({"kind": kind, "partial": partial, "request": (request or "")[:800],
                                   "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
        return True
    except Exception:
        return False


def load_pending() -> Optional[dict]:
    try:
        data = json.loads(_pending_file().read_text(encoding="utf-8"))
        if time.time() - float(data.get("ts", 0)) > PENDING_TTL_SECONDS or not data.get("partial"):
            return None
        return data
    except Exception:
        return None


def clear_pending() -> None:
    try:
        _pending_file().unlink()
    except Exception:
        pass


# ---------------------------------------------------------------- clasificación del mensaje

def is_continue_request(message: str) -> bool:
    m = (message or "").strip()
    return bool(m) and len(m) <= 60 and (bool(_CONTINUE_RE.match(m)) or bool(re.fullmatch(r"[\s]*(?:\.{2,}|…+)[\s]*", m)))


def is_punctuation_only(message: str) -> bool:
    return bool(_PUNCT_ONLY_RE.match(message or ""))


# ---------------------------------------------------------------- continuar

def _outcome(reply: str, summary: str, ok: bool = True, truncated: bool = False, history_note: str = "",
             replace_previous: bool = False, tools=None):
    out = P.PipelineOutcome(reply=reply, tools_called=list(tools or []), ok=ok, truncated=truncated, summary=summary)
    out.history_note = history_note
    out.replace_previous = replace_previous
    out.from_continuation = True
    return out


def _new_part(partial: str, full: str) -> tuple[str, bool]:
    """(texto_a_pegar, reemplazar). Si `full` empieza con lo que el cliente ya mostró, solo se manda lo nuevo."""
    base = partial.rstrip()
    if full.startswith(base) and len(full) > len(base):
        return full[len(base):], False
    return full, True


def _continue_code(pending: dict, emit):
    partial = pending["partial"]
    emit({"type": "progreso", "mensaje": "Retomando el código donde se cortó..."})
    full, done = P.continue_code(partial)
    if not done or full.strip() == partial.strip():
        return _outcome("No pude retomar el código: el modelo no devolvió una continuación útil. "
                        "Pídeme el cambio de nuevo o dime qué parte falta.", "continuación de código fallida", ok=False)
    still = P.looks_truncated(full, False)
    new, replace = _new_part(partial, full)
    if still:
        remember("code", full, pending.get("request", ""))
        new_note = "El código sigue incompleto: puedes pedir «sigue» otra vez."
    else:
        P._persist_code(full)
        clear_pending()
        new_note = ""
    if not replace:
        # el cliente ya tiene `partial`; el texto nuevo se agrega y, si hace falta, se avisa al final
        new = new + (f"\n\n{new_note}" if new_note else "")
    lines = sum(len(b.splitlines()) for _, b in P._blocks(full)) or len(full.splitlines())
    return _outcome(new, f"continuación de código | {lines} líneas", ok=True, truncated=still, replace_previous=replace,
                    history_note=f"Listo: completé el código ({lines} líneas)." + (f" {new_note}" if new_note else ""))


def _continue_text(provider, pending: dict, emit):
    partial = pending["partial"]
    emit({"type": "progreso", "mensaje": "Retomando la respuesta donde se cortó..."})
    tail = partial[-TAIL_CHARS:]
    prompt = (f"Petición original del usuario:\n{pending.get('request', '')[:600]}\n\n"
              "Tu respuesta se cortó por el límite de longitud. Estas son sus últimas líneas:\n"
              f"---\n{tail}\n---\n"
              "Continuá EXACTAMENTE desde ahí: sin repetir lo anterior, sin saludar y sin avisar que continuás. "
              "Terminá lo que estaba diciendo.")
    res = provider.chat([{"role": "user", "content": prompt}], allow_tools=False)
    cont = strip_think_blocks(res.content or "").strip("\n")
    if not cont.strip():
        return _outcome("No pude retomar la respuesta: el modelo no devolvió nada nuevo.", "continuación de texto fallida", ok=False)
    full = P._merge_continuation(partial.rstrip(), cont)
    new, replace = _new_part(partial, full)
    if not replace and new[:1].isalnum() and partial.rstrip()[-1:].isalnum():
        new = " " + new                      # "…cuando Sega" + "lanzó…" no debe quedar "Segalanzó"
    still = bool(getattr(res, "truncated", False))
    if still:
        remember("text", full, pending.get("request", ""))
    else:
        clear_pending()
    return _outcome(new, f"continuación de texto | +{len(new)} caracteres", ok=True, truncated=still, replace_previous=replace,
                    history_note=f"(continuación de la respuesta anterior: {len(new)} caracteres más)")


# ---------------------------------------------------------------- punto de entrada para core.py

IDLE_REPLY = ("No tengo nada pendiente que continuar. Dime qué necesitas (por ejemplo: «hazme un informe de…», "
              "«agrégale una imagen al PDF» o «cámbiale el color a la página») y lo hago.")


def handle_turn(provider, message: str, emit) -> Optional["P.PipelineOutcome"]:
    """None -> el turno sigue su flujo normal. Un PipelineOutcome -> ya está resuelto (core.py lo guarda y devuelve)."""
    pending = load_pending()
    if is_continue_request(message):
        if pending:
            try:
                out = _continue_code(pending, emit) if pending["kind"] == "code" else _continue_text(provider, pending, emit)
            except Exception as e:
                out = _outcome(f"No pude retomar lo anterior ({e}).", "continuación fallida", ok=False)
            if out.reply:
                emit({"type": "chunk", "texto": out.reply}) if not out.replace_previous else None
            return out
        if is_punctuation_only(message):          # «...» sin nada pendiente: no despertar al orquestador
            emit({"type": "chunk", "texto": IDLE_REPLY})
            return _outcome(IDLE_REPLY, "mensaje vacío, sin pendiente", ok=True)
        return None                               # «sigue» sin pendiente puede referirse a la charla: que decida el modelo
    if is_punctuation_only(message):
        emit({"type": "chunk", "texto": IDLE_REPLY})
        return _outcome(IDLE_REPLY, "mensaje vacío", ok=True)
    clear_pending()                               # otro tema: lo anterior ya no se continúa
    return None