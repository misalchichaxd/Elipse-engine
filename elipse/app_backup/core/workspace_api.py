"""
API del Workspace: listar, descargar y borrar los archivos que ELIPSE genera o recibe
(documentos PDF/Word, código, imágenes, adjuntos...). Todo pasa por _resolve_safe_path,
así que nada fuera de la carpeta workspace es alcanzable (../, rutas absolutas, symlinks).

Se monta en main.py con:  app.include_router(workspace_router)
"""

import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.auth import require_api_key
from app.core.tools import WORKSPACE_ROOT, _resolve_safe_path

router = APIRouter(prefix="/v1/workspace", dependencies=[Depends(require_api_key)])

# Carpetas que no se muestran: fuentes internas de write_document (mode='append') y ruido técnico.
SKIP_DIRS = {"_src", "__pycache__", "node_modules", ".git", ".venv", "venv"}
MAX_FILES = 3000
DOC_EXTS = (".pdf", ".docx", ".md")


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _target_file(path: str) -> Path:
    try:
        target = _resolve_safe_path(path)
    except ValueError:
        raise HTTPException(400, "Ruta no permitida.")
    if not target.is_file():
        raise HTTPException(404, "Archivo no encontrado.")
    return target


@router.get("/files")
def list_workspace_files():
    """Todos los archivos del workspace (recursivo), con tamaño y fecha de modificación."""
    files: list[dict] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(WORKSPACE_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            rel = full.relative_to(WORKSPACE_ROOT).as_posix()
            files.append({
                "path": rel,
                "name": name,
                "dir": rel.rsplit("/", 1)[0] if "/" in rel else "",
                "ext": _ext(name),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
            if len(files) >= MAX_FILES:
                truncated = True
                break
        if truncated:
            break
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return {"files": files, "total": len(files), "truncated": truncated}


@router.get("/download")
def download_workspace_file(path: str):
    target = _target_file(path)
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, filename=target.name)


@router.delete("/file")
def delete_workspace_file(path: str):
    target = _target_file(path)
    docs_dir = (WORKSPACE_ROOT / "documents").resolve()
    try:
        target.unlink()
    except OSError as e:
        raise HTTPException(500, f"No se pudo borrar: {e}")

    # Si era un documento generado y ya no queda ningún formato, se borra también su fuente
    # Markdown interna (documents/_src) para que no quede huérfana.
    if target.parent == docs_dir and target.suffix.lower() in DOC_EXTS:
        if not any((docs_dir / (target.stem + e)).exists() for e in DOC_EXTS):
            (docs_dir / "_src" / (target.stem + ".md")).unlink(missing_ok=True)

    return {"status": "borrado", "path": path}