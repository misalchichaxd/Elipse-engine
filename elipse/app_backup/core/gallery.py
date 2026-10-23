"""
Galería visual de ELIPSE: aprende y reconoce PERSONAJES, mascotas, figuras u objetos concretos.

Idea: un modelo de 4B no sabe de memoria quién es "x personaje", y menos con su diseño exacto.
En vez de depender de lo que el modelo "recuerde", ELIPSE APRENDE con referencias:

  APRENDER   learn_character(name, path)            -> el usuario da una imagen y el nombre
             learn_character(name, from_web=True)   -> usa la INVESTIGACIÓN (research) para leer
                                                       cómo es y search_images para traer 1-2
                                                       referencias de internet
             Cada entrada se guarda en la MEMORIA SEMÁNTICA (Chroma, type="character", así no
             la poda la política de retención) con la descripción de su diseño, y sus imágenes de
             referencia (512 px) en workspace/personajes/.

  RECONOCER  identify_character(path)
             1) el modelo de visión describe el diseño del sujeto (prompt "personaje")
             2) esa descripción se busca en la memoria (similitud semántica, en CPU)
             3) los 1-2 candidatos se VERIFICAN: el modelo de visión mira la imagen nueva junto a
                las referencias del candidato y responde SI / NO / DUDA
             La verificación visual es la que decide; la búsqueda por texto solo propone candidatos.

  CORREGIR   si el usuario dice "no, es X" o "este es X", el modelo llama a learn_character y la
             galería mejora sola. forget_character borra una entrada equivocada.

Límites a propósito:
  - NO se aprenden ni se identifican PERSONAS REALES por su rostro (si el modelo de visión dice
    que el sujeto es una persona real, se rechaza). La galería es para personajes de ficción,
    mascotas, figuras u objetos.
  - Un modelo de 2B-4B puede equivocarse: el resultado se presenta como estimación, no certeza.
  - Las referencias de internet no están verificadas (pueden ser fan-art o de otro personaje).
"""

import logging
import re
import time
from io import BytesIO
from pathlib import Path
from typing import Optional

from app.config import settings
from app.core import memory, vision

log = logging.getLogger("elipse")

TYPE = "character"
_MAX_WEB_IMAGE_BYTES = 6 * 1024 * 1024
_TRUE = ("true", "1", "si", "sí", "yes", "verdadero")


# --------------------------------------------------------------------------- utilidades

def _root() -> Path:
    from app.core import tools  # import tardío: tools importa este módulo
    p = tools.WORKSPACE_ROOT / settings.gallery_dir
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(name: str) -> str:
    plain = vision._plain(name)
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:40]


def _col():
    return memory._get_collection()


def _entry_id(slug: str) -> str:
    return f"char_{slug}"


def _get_entry(slug: str) -> Optional[dict]:
    res = _col().get(ids=[_entry_id(slug)], include=["documents", "metadatas"])
    if res.get("ids"):
        return {"doc": res["documents"][0], "meta": dict(res["metadatas"][0])}
    return None


def count() -> int:
    try:
        return len(_col().get(where={"type": TYPE}, include=["metadatas"]).get("ids", []))
    except Exception as e:
        log.warning("galería: no pude contar: %s", e)
        return 0


def _split(value: str) -> list:
    return [x for x in (value or "").split(",") if x]


def _merge(old: str, new: str, cap: int = 900) -> str:
    old, new = (old or "").strip(), (new or "").strip()
    if not new:
        return old
    if not old:
        return new[:cap]
    if new[:80] in old:
        return old
    return f"{old} | {new}"[:cap]


def _save_ref(raw: bytes, slug: str) -> str:
    """Guarda una referencia reducida (512 px, JPEG) en workspace/personajes/. Devuelve el nombre."""
    img = vision.open_rgb(raw)
    img.thumbnail((512, 512))
    root = _root()
    nums = [int(m.group(1)) for p in root.glob(f"{slug}_*.jpg")
            if (m := re.search(r"_(\d+)\.jpg$", p.name))]
    fname = f"{slug}_{(max(nums) + 1) if nums else 1}.jpg"
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    (root / fname).write_bytes(buf.getvalue())
    return fname


def _delete_ref_files(names: list) -> None:
    for n in names:
        try:
            (_root() / n).unlink(missing_ok=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- aprender

def _web_knowledge(name: str) -> str:
    """Usa la tool de INVESTIGACIÓN existente (busca -> resume -> guarda en memoria como 'research')."""
    try:
        from app.core.research import run_research_pipeline
        res = run_research_pipeline(f"{name} aspecto físico y rasgos distintivos (cómo se ve)")
        if res.get("status") == "ok":
            return (res.get("summary") or "")[:500]
        log.info("galería: la investigación de '%s' no dio resultados: %s", name, res.get("detail"))
    except Exception as e:
        log.warning("galería: falló la investigación de '%s': %s", name, e)
    return ""


def _web_images(name: str, n: int) -> list:
    """Descarga hasta n imágenes de referencia de internet (misma validación SSRF que read_url)."""
    out = []
    try:
        from ddgs import DDGS
        from app.core import tools
        results = list(DDGS().images(f"{name} personaje", max_results=n * 4, safesearch="moderate"))
        for r in results:
            url = (r.get("image") or "").strip()
            if not tools._usable_image(url):
                continue
            try:
                resp = tools._safe_get(url)
                data = resp.content
                if resp.status_code != 200 or len(data) > _MAX_WEB_IMAGE_BYTES or not vision.sniff_mime(data):
                    continue
            except Exception:
                continue
            out.append(data)
            if len(out) >= n:
                break
    except Exception as e:
        log.warning("galería: no pude traer imágenes de '%s': %s", name, e)
    return out


def _appearance(raw: bytes) -> tuple[str, str]:
    """(sujeto, diseño) con el prompt 'personaje' del modelo de visión. Lanza VisionError."""
    return vision.split_subject(vision.describe_bytes(raw, kind="personaje", ocr_text=None))


def learn(name: str, path: str = "", notes: str = "", from_web=False) -> str:
    """Función de la TOOL learn_character."""
    name = " ".join((name or "").split())
    slug = _slug(name)
    if not (2 <= len(name) <= 60) or not slug:
        return "Error: dame un nombre de entre 2 y 60 caracteres."
    from_web = str(from_web).strip().lower() in _TRUE
    path, notes = (path or "").strip(), (notes or "").strip()
    if not (path or notes or from_web):
        return "Error: necesito al menos una imagen (path), unas notas o from_web=true para aprender algo."

    # 1) lo que NO necesita el modelo de visión primero (investigación y descargas), para
    #    cambiar de modelo la menor cantidad de veces posible en una GPU de 4 GB.
    web_text = _web_knowledge(name) if from_web else ""
    web_raws = _web_images(name, max(int(settings.gallery_web_refs), 1)) if from_web else []

    # 2) todas las pasadas del modelo de visión juntas
    user_raw = b""
    appearances = []   # (origen, diseño, bytes)
    try:
        if path:
            user_raw = vision._read_workspace_image(path)
            subject, design = _appearance(user_raw)
            if vision.is_real_person(subject):
                return ("No guardo esta imagen: parece una persona real y no aprendo ni identifico "
                        "personas por su rostro. La galería es para personajes, mascotas, figuras u objetos.")
            appearances.append(("usuario", design, user_raw))
        for raw in web_raws:
            subject, design = _appearance(raw)
            if not vision.is_real_person(subject):
                appearances.append(("web", design, raw))
    except vision.VisionError as e:
        return f"Error: no pude mirar la imagen con el modelo de visión: {e}"

    # 3) guardar
    entry = _get_entry(slug)
    meta = entry["meta"] if entry else {"type": TYPE, "slug": slug, "name": name, "created_at": time.time(),
                                        "refs": "", "ref_sources": "", "appearance": "", "notes": "", "web": ""}
    refs, sources = _split(meta.get("refs")), _split(meta.get("ref_sources"))
    added = {"usuario": 0, "web": 0}
    for origin, design, raw in appearances:
        fname = _save_ref(raw, slug)
        refs.append(fname)
        sources.append(origin)
        added[origin] += 1
        meta["appearance"] = _merge(meta.get("appearance", ""), design)

    # tope de referencias: se descartan primero las de internet y luego las más viejas
    max_refs = max(int(settings.gallery_max_refs), 1)
    while len(refs) > max_refs:
        idx = next((i for i, s in enumerate(sources) if s == "web"), 0)
        _delete_ref_files([refs.pop(idx)])
        sources.pop(idx)

    meta["refs"], meta["ref_sources"] = ",".join(refs), ",".join(sources)
    meta["notes"] = _merge(meta.get("notes", ""), notes, cap=400)
    meta["web"] = web_text or meta.get("web", "")
    meta["updated_at"] = time.time()
    doc = (f"{name}. Apariencia: {meta['appearance']}. {meta['notes']}. {meta['web']}").strip()[:1800]
    _col().upsert(ids=[_entry_id(slug)], documents=[doc], metadatas=[meta])

    parts = []
    if added["usuario"]:
        parts.append(f"{added['usuario']} referencia tuya")
    if added["web"]:
        parts.append(f"{added['web']} de internet (sin verificar)")
    if web_text:
        parts.append("datos investigados en internet")
    if notes:
        parts.append("tus notas")
    saved = ", ".join(parts) if parts else "sin material nuevo"
    return (f"Aprendí a «{name}» ({saved}; en total {len(refs)} referencia(s) guardadas). "
            f"Diseño registrado: {meta['appearance'][:300] or '(sin descripción visual todavía)'}")


# --------------------------------------------------------------------------- reconocer

def _verify(raw: bytes, name: str, refs: list) -> str:
    """El modelo de visión compara la imagen nueva con las referencias. 'si' | 'no' | 'duda' | 'error'."""
    images = [vision.prepare_image(raw, max_side=512)]
    for r in refs:
        p = _root() / r
        if p.is_file():
            images.append(vision.prepare_image(p.read_bytes(), max_side=512))
    if len(images) < 2:
        return "duda"
    k = len(images) - 1
    refs_txt = "La imagen 2 es" if k == 1 else f"Las imágenes 2 a {k + 1} son"
    prompt = (
        f"Tienes {k + 1} imágenes. La imagen 1 es la que hay que identificar. {refs_txt} referencia(s) de «{name}».\n"
        "¿La imagen 1 muestra al mismo personaje o sujeto que la(s) referencia(s)? Compara el DISEÑO: "
        "color y peinado del cabello o pelaje, ojos, ropa, accesorios y rasgos distintivos. Ignora diferencias de "
        "pose, ángulo, expresión, fondo o calidad. Sé estricto: si el diseño es distinto, responde NO.\n"
        "Responde en la primera línea SOLO una palabra: SI, NO o DUDA. En la segunda línea, la razón en máximo 20 palabras."
    )
    try:
        text = vision.ask(prompt, images)
    except vision.VisionError as e:
        log.warning("galería: falló la verificación de '%s': %s", name, e)
        return "error"
    words = re.findall(r"[a-z]+", vision._plain(text))
    first = words[0] if words else ""
    log.info("galería: verificación de '%s' -> %s | %s", name, first, text.replace("\n", " ")[:120])
    return {"si": "si", "no": "no", "duda": "duda"}.get(first, "duda")


def _identify(raw: bytes, description: str) -> dict:
    subject, appearance = vision.split_subject(description)
    if vision.is_real_person(subject):
        return {"status": "real"}
    total = count()
    if total == 0:
        return {"status": "vacia", "appearance": appearance}
    hits = memory.search_memory(appearance or description,
                                n_results=max(int(settings.gallery_verify_max), 1), where={"type": TYPE})
    checked = []
    for h in hits:
        log.info("galería: candidato '%s' a distancia %.3f", h["metadata"].get("name"), h["distance"])
        if h["distance"] > float(settings.gallery_match_distance):
            continue
        meta = h["metadata"]
        refs = _split(meta.get("refs"))[: max(int(settings.gallery_verify_refs), 1)]
        verdict = _verify(raw, meta.get("name", "?"), refs)
        checked.append((meta.get("name", "?"), verdict))
        if verdict == "si":
            return {"status": "match", "name": meta.get("name"), "refs": len(refs), "appearance": appearance}
    return {"status": "no_match", "checked": checked, "total": total, "appearance": appearance}


def _format(res: dict) -> str:
    st = res["status"]
    if st == "real":
        return "(Es la fotografía de una persona real: no la identifico por su rostro.)"
    if st == "vacia":
        return ("(Mi galería de personajes está vacía. Si el usuario dice quién es, usa learn_character "
                "con el nombre y el archivo; para investigarlo en internet, learn_character con from_web=true.)")
    if st == "match":
        return (f"Coincidencia en mi galería: «{res['name']}» (el modelo de visión la verificó contra "
                f"{res['refs']} referencia(s); es una estimación, no una certeza).")
    dud = [f"{n} ({v})" for n, v in res.get("checked", [])]
    extra = f" Descartados tras comparar: {', '.join(dud)}." if dud else ""
    return (f"No coincide con ninguno de los {res['total']} personajes de mi galería.{extra} "
            "Si el usuario dice quién es, usa learn_character; para investigarlo en internet, "
            "learn_character con from_web=true.")


def match_text(raw: bytes, description: str) -> str:
    """Para vision.analyze (tipo 'personaje'): texto de coincidencia, o '' si la galería está vacía."""
    if count() == 0:
        return ""
    return _format(_identify(raw, description))


def identify(path: str, region: str = "") -> str:
    """Función de la TOOL identify_character."""
    try:
        raw = vision._read_workspace_image(path)
        if region and region.strip():
            raw, _ = vision.crop_region(raw, region)
        description = vision.describe_bytes(raw, kind="personaje", ocr_text=None)
    except vision.VisionError as e:
        return f"Error: {e}"
    res = _identify(raw, description)
    subject, design = vision.split_subject(description)
    return "\n".join([
        f"[Análisis de '{path}'" + (f", zona '{region}'" if region else "") + ":",
        f"Sujeto: {subject or 'no indicado'}. Diseño: {design}",
        _format(res) + "]",
    ])


# --------------------------------------------------------------------------- olvidar

def forget(name: str) -> str:
    """Función de la TOOL forget_character (pasa por confirmación humana: ver safety.py)."""
    slug = _slug(name or "")
    entry = _get_entry(slug) if slug else None
    if not entry:
        return f"Error: no tengo ningún personaje llamado '{name}' en la galería."
    _delete_ref_files(_split(entry["meta"].get("refs")))
    _col().delete(ids=[_entry_id(slug)])
    return f"Olvidé a «{entry['meta'].get('name', name)}» y borré sus referencias."
