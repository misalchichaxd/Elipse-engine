import threading
import time
import uuid
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

from app.config import settings

CHROMA_DIR = (Path(__file__).parent.parent.parent / settings.chroma_dir).resolve()
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

_client = None
_collection = None
_lock = threading.Lock()


def _get_collection():
    """
    Cliente y colección de Chroma como singleton perezoso: se inicializan la
    primera vez que hacen falta (no al importar el módulo), así el proceso no
    carga el modelo de embeddings si nunca se usa memoria semántica en esa corrida.
    """
    global _client, _collection
    if _collection is not None:
        return _collection

    with _lock:
        if _collection is None:
            _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
            embedding_fn = embedding_functions.DefaultEmbeddingFunction()
            _collection = _client.get_or_create_collection(
                name=settings.chroma_collection,
                embedding_function=embedding_fn,
                metadata={"hnsw:space": "cosine"},
            )
    return _collection


def add_memory(content: str, metadata: dict | None = None, memory_id: str | None = None) -> str:
    """
    Guarda un texto en la memoria semántica. Debe ser idealmente un resumen
    destilado, no texto crudo (ver plan_desarrollo.md, decisión de diseño #5).
    Devuelve el id con el que quedó guardado.
    """
    collection = _get_collection()
    memory_id = memory_id or uuid.uuid4().hex

    meta = {"created_at": time.time()}
    if metadata:
        meta.update({k: v for k, v in metadata.items() if v is not None})

    collection.add(ids=[memory_id], documents=[content], metadatas=[meta])
    return memory_id


def search_memory(query: str, n_results: int = 4, where: dict | None = None):
    """
    Busca por similitud semántica (significado), no por palabras exactas.
    Devuelve lista de dicts: {id, content, metadata, distance}.
    'distance' es distancia coseno: más BAJO = más parecido.
    """
    collection = _get_collection()
    total = collection.count()
    if total == 0:
        return []

    n_results = min(n_results, total)
    result = collection.query(query_texts=[query], n_results=n_results, where=where)

    ids = result.get("ids", [[]])[0]
    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    dists = result.get("distances", [[]])[0]

    return [
        {"id": ids[i], "content": docs[i], "metadata": metas[i], "distance": dists[i]}
        for i in range(len(ids))
    ]


def delete_memory(memory_id: str):
    _get_collection().delete(ids=[memory_id])


def count_memories() -> int:
    return _get_collection().count()


def enforce_retention_policy(max_items: int | None = None, prunable_types: list | None = None):
    """
    Política de retención: si hay más de 'max_items' recuerdos guardados en total,
    borra los MÁS VIEJOS (por 'created_at') hasta volver al límite — pero SOLO
    entre los recuerdos cuyo metadata['type'] esté en 'prunable_types'
    (por defecto settings.memory_prunable_types, ej. ["research"]).

    Todo lo que no tenga un 'type' podable (memoria manual, identidad, etc.)
    queda protegido y nunca se borra automáticamente, aunque sea lo más viejo.

    Si podar todo lo disponible en los tipos podables no alcanza para volver
    al límite (porque hay demasiados recuerdos protegidos), se borra lo que
    se pueda y se informa en 'limit_reached': el resto queda por encima del
    límite hasta que se libere espacio manualmente.

    Barato de correr después de cada guardado: solo trae ids+metadatas,
    nunca los vectores completos, así que es rápido incluso con miles de items.
    """
    max_items = max_items or settings.memory_max_items
    prunable_types = set(prunable_types if prunable_types is not None else settings.memory_prunable_types)

    collection = _get_collection()
    total = collection.count()
    if total <= max_items:
        return {"deleted": 0, "total_before": total, "total_after": total, "within_limit": True}

    everything = collection.get(include=["metadatas"])
    all_paired = list(zip(everything["ids"], everything["metadatas"]))

    prunable = sorted(
        (p for p in all_paired if p[1].get("type") in prunable_types),
        key=lambda p: p[1].get("created_at", 0),
    )

    overflow = total - max_items
    ids_to_delete = [p[0] for p in prunable[:overflow]]

    if ids_to_delete:
        collection.delete(ids=ids_to_delete)

    total_after = collection.count()
    return {
        "deleted": len(ids_to_delete),
        "total_before": total,
        "total_after": total_after,
        # False = seguimos por encima del límite: no había suficientes
        # recuerdos podables (research) para bajar más — el resto es protegido.
        "within_limit": total_after <= max_items,
    }


def reset_memory():
    """Borra TODA la memoria semántica. Uso manual/depuración, el agente nunca la llama."""
    global _client, _collection
    with _lock:
        if _client is not None:
            _client.delete_collection(settings.chroma_collection)
        _collection = None
    _get_collection()