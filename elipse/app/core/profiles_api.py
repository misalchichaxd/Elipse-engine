"""
API de perfiles (/v1/profiles/*). Se monta en main.py con app.include_router(profiles_router).

Quién puede qué (E10):
  - Llave ADMIN (perfil 'elipse', sin caps): todo.
  - Llave de ANFITRIÓN (p. ej. 'rbe'): solo GET/PUT de SU perfil (spec recortado por el techo de su
    llave; el engine informa `rejected`). Nunca toca callback_url / secreto (R12).
Un perfil ajeno responde 404 (no 403) para no revelar qué perfiles existen.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import profiles
from app.core.auth import require_admin_key, require_principal
from app.core.profiles import Principal

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


class SpecBody(BaseModel):
    spec: dict


class CreateBody(BaseModel):
    name: str
    spec: dict
    enabled: bool = True


class EnabledBody(BaseModel):
    enabled: bool


class CallbackBody(BaseModel):
    url: str
    secret: str | None = None
    generate_secret: bool = False


def _bad(e: Exception):
    code = 404 if isinstance(e, LookupError) else 400
    raise HTTPException(status_code=code, detail=str(e).strip("'\""))


def _own_or_admin(name: str, principal: Principal):
    if not (principal.is_admin or principal.profile.name == name):
        raise HTTPException(status_code=404, detail="No existe ese perfil.")


@router.get("", dependencies=[Depends(require_admin_key)])
def list_all():
    return {"profiles": profiles.list_profiles()}


@router.get("/caps-template", dependencies=[Depends(require_admin_key)])
def caps_template():
    return {"caps": profiles.default_caps()}


@router.post("", dependencies=[Depends(require_admin_key)])
def create(body: CreateBody):
    try:
        res = profiles.sync_profile(body.name, body.spec, None, create_only=True, enabled=body.enabled)
    except (ValueError, LookupError) as e:
        _bad(e)
    return res


@router.get("/{name}")
def read(name: str, principal: Principal = Depends(require_principal)):
    _own_or_admin(name, principal)
    info = profiles.get_profile_info(name)
    if info is None:
        raise HTTPException(status_code=404, detail="Ese perfil aún no está sincronizado.")
    if not principal.is_admin:   # el anfitrión ve su spec EFECTIVO; nunca la URL ni el secreto
        eff, rejected = profiles.effective_spec(info["spec"], principal.profile.caps)
        return {"name": name, "enabled": info["enabled"], "spec": eff, "rejected": rejected,
                "has_callback": bool(info["callback_url"] and info["has_secret"])}
    return info


@router.put("/{name}")
def put(name: str, body: SpecBody, principal: Principal = Depends(require_principal)):
    _own_or_admin(name, principal)
    caps = None if principal.is_admin else principal.profile.caps
    try:
        return profiles.sync_profile(name, body.spec, caps)
    except (ValueError, LookupError) as e:
        _bad(e)


@router.post("/{name}/enabled", dependencies=[Depends(require_admin_key)])
def enabled(name: str, body: EnabledBody):
    if not profiles.set_enabled(name, body.enabled):
        raise HTTPException(status_code=404, detail="No existe ese perfil.")
    return {"profile": name, "enabled": body.enabled}


@router.delete("/{name}", dependencies=[Depends(require_admin_key)])
def delete(name: str, keep_keys: bool = False):
    res = profiles.delete_profile(name, revoke_keys=not keep_keys)
    if not res["deleted"] and not res["keys_revoked"]:
        raise HTTPException(status_code=404, detail="No existe ese perfil.")
    return res


@router.put("/{name}/callback", dependencies=[Depends(require_admin_key)])
def set_callback(name: str, body: CallbackBody):
    try:
        return profiles.set_callback(name, body.url, body.secret, body.generate_secret)
    except (ValueError, LookupError) as e:
        _bad(e)


@router.delete("/{name}/callback", dependencies=[Depends(require_admin_key)])
def clear_callback(name: str):
    if not profiles.clear_callback(name):
        raise HTTPException(status_code=404, detail="No existe ese perfil.")
    return {"profile": name, "callback_url": None}