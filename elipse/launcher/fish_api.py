"""
Consulta a Fish Audio desde el launcher: dado un ID (o el enlace de la voz) averigua qué voz es.
GET https://api.fish.audio/model/{id}  ->  título, tipo, estado, idiomas, visibilidad, autor.
"""

import json
import re
import urllib.error
import urllib.request

FISH_BASE = "https://api.fish.audio"
_ID = re.compile(r"[0-9a-fA-F]{32}")


def extract_fish_id(text: str) -> str:
    """Acepta el ID pelado o un enlace (https://fish.audio/m/<id>/...) y devuelve el ID de 32 caracteres hex."""
    text = (text or "").strip()
    m = _ID.search(text)
    if m:
        return m.group(0).lower()
    return text if re.fullmatch(r"[\w\-]{6,64}", text) else ""


def fetch_fish_voice(api_key: str, voice_id: str, base_url: str = FISH_BASE, timeout: int = 10) -> dict:
    """Datos de la voz. Lanza Exception con un mensaje en español si algo falla."""
    req = urllib.request.Request(
        f"{base_url}/model/{voice_id}",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "ELIPSE-launcher/1.0"} if api_key
        else {"User-Agent": "ELIPSE-launcher/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise Exception("Fish rechazó la API key (o la voz es privada de otra cuenta). Revisa la clave.") from None
        if e.code in (400, 404, 422):
            raise Exception("No existe una voz con ese ID en Fish Audio.") from None
        raise Exception(f"Fish respondió con el error {e.code}.") from None
    except urllib.error.URLError as e:
        raise Exception(f"No pude conectar con Fish Audio ({e.reason}).") from None
    except (ValueError, TimeoutError, OSError) as e:
        raise Exception(f"Respuesta inesperada de Fish Audio ({type(e).__name__}).") from None

    return {
        "id": data.get("_id") or voice_id,
        "title": (data.get("title") or "").strip(),
        "type": data.get("type") or "",            # "tts" sirve; "svc" es conversión de voz
        "state": data.get("state") or "",          # created | training | trained | failed
        "languages": data.get("languages") or [],
        "visibility": data.get("visibility") or "",
        "author": ((data.get("author") or {}).get("nickname") or "").strip(),
        "train_mode": data.get("train_mode") or "",
        "tags": data.get("tags") or [],
    }


_STATE = {"created": "creada", "training": "entrenando", "trained": "entrenada", "failed": "falló"}
_VIS = {"public": "pública", "unlist": "no listada", "private": "privada"}


def describe_voice(info: dict) -> tuple[str, str]:
    """(texto, nivel) con nivel 'ok' | 'warn'. Resume la voz y avisa si no sirve para TTS."""
    bits = [f"«{info['title'] or 'sin título'}»"]
    if info["author"]:
        bits.append(f"de {info['author']}")
    if info["languages"]:
        bits.append("idiomas: " + ", ".join(info["languages"][:5]))
    if info["visibility"]:
        bits.append(_VIS.get(info["visibility"], info["visibility"]))
    if info["state"]:
        bits.append(_STATE.get(info["state"], info["state"]))
    text = "✓ " + " · ".join(bits)
    if info["type"] == "svc":
        return text + "\n⚠ Es un modelo de conversión de voz (svc), no de texto a voz: probablemente no hable.", "warn"
    if info["state"] in ("training", "failed", "created"):
        return text + "\n⚠ La voz todavía no está lista (estado: " + _STATE.get(info["state"], info["state"]) + ").", "warn"
    return text, "ok"
