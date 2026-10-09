"""
Caché por imagen para ELIPSE: guarda el OCR, la descripción del modelo de visión y la zona
mirada, con clave = sha1 de los bytes de la imagen.

Para qué sirve (con una GPU de 4 GB):
  - Cambiar del modelo principal al de visión (y volver) es lo MÁS lento de todo el flujo.
    Si el usuario pregunta otra vez por la misma imagen ("¿y qué decía abajo?"), el OCR y la
    descripción salen de acá al instante y no se despierta ningún modelo.
  - El OCR corre en CPU y en una captura grande tarda unos segundos: se paga una sola vez.

La clave incluye el motor de OCR / el modelo de visión / la pregunta, así que si cambias de
modelo o preguntas algo distinto no te devuelve una respuesta vieja.

La tabla se crea sola la primera vez (no hace falta tocar db.py). Un fallo del caché nunca
rompe nada: simplemente se comporta como si no hubiera caché.
"""

import hashlib
import json
import logging
import threading
import time
from typing import Optional

from app.config import settings
from app.core.db import get_connection

log = logging.getLogger("elipse")

_lock = threading.Lock()
_ready = False
_puts = 0


def _ensure_table() -> None:
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        conn = get_connection()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS image_cache ("
                " image_id TEXT NOT NULL,"
                " key TEXT NOT NULL,"
                " value TEXT NOT NULL,"
                " created_at REAL NOT NULL,"
                " PRIMARY KEY (image_id, key))"
            )
            conn.commit()
        finally:
            conn.close()
        _ready = True


def digest(data: bytes) -> str:
    """Identificador estable de una imagen: sha1 de sus bytes."""
    return hashlib.sha1(data).hexdigest()


def key_of(*parts: str) -> str:
    """Clave corta y estable a partir de varias piezas (modelo, tipo, pregunta...)."""
    raw = "|".join((p or "").strip().lower() for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def get(image_id: str, key: str) -> Optional[dict]:
    try:
        _ensure_table()
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM image_cache WHERE image_id = ? AND key = ?", (image_id, key)
            ).fetchone()
        finally:
            conn.close()
        return json.loads(row["value"]) if row else None
    except Exception as e:  # el caché nunca debe romper un turno
        log.warning("imagecache.get falló: %s", e)
        return None


def put(image_id: str, key: str, value: dict) -> None:
    global _puts
    try:
        _ensure_table()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO image_cache (image_id, key, value, created_at) VALUES (?, ?, ?, ?)",
                (image_id, key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        _puts += 1
        if _puts % 25 == 0:
            _prune()
    except Exception as e:
        log.warning("imagecache.put falló: %s", e)


def _prune() -> None:
    """Mantiene la tabla acotada: borra las filas más viejas si se pasa del tope."""
    limit = max(int(settings.vision_cache_max), 20) * 6  # ~6 claves por imagen
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM image_cache").fetchone()["n"]
        if total > limit:
            conn.execute(
                "DELETE FROM image_cache WHERE rowid IN "
                "(SELECT rowid FROM image_cache ORDER BY created_at ASC LIMIT ?)",
                (total - limit,),
            )
            conn.commit()
    finally:
        conn.close()
