"""
CLI para administrar los PERFILES de ELIPSE (Fase 2). Mismo patrón que manage_keys.

Uso (con el venv activado, desde la raíz del proyecto):
    python -m app.manage_profiles list
    python -m app.manage_profiles show rbe
    python -m app.manage_profiles create rbe --spec rbe.json        # nace habilitado
    python -m app.manage_profiles update rbe --spec rbe.json
    python -m app.manage_profiles disable rbe                       # sus llaves dan 403
    python -m app.manage_profiles enable rbe
    python -m app.manage_profiles set-callback rbe --url http://127.0.0.1:8001/elipse/tools --generate
    python -m app.manage_profiles clear-callback rbe
    python -m app.manage_profiles delete rbe [--keep-keys]          # por defecto revoca sus llaves
    python -m app.manage_profiles caps-template                     # techo sugerido para llaves

Con --json (para el launcher), va SIEMPRE justo después de "app.manage_profiles":
    python -m app.manage_profiles --json list
Salida JSON: {"ok": true, ...} o {"error": "..."} (código de salida 1).

El callback y su secreto solo se fijan AQUÍ (o con una llave admin por la API), nunca con la llave
del anfitrión (R12). El secreto generado se muestra UNA sola vez.
"""

import argparse
import json
import sys

from app.core import profiles
from app.core.db import init_db


def _emit(args, payload: dict, text: str = ""):
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif text:
        print(text)


def _fail(args, message: str):
    if args.json:
        print(json.dumps({"error": message}, ensure_ascii=False))
    else:
        print(f"Error: {message}")
    sys.exit(1)


def _read_spec(args) -> dict:
    try:
        with open(args.spec, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        _fail(args, f"No pude leer --spec ({e}).")


def main():
    parser = argparse.ArgumentParser(description="Administra los perfiles de ELIPSE.")
    parser.add_argument("--json", action="store_true", help="Salida en JSON para consumo automático.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Lista los perfiles.")
    sub.add_parser("caps-template", help="Techo (caps) sugerido para llaves de anfitrión.")
    for cmd in ("show", "enable", "disable", "clear-callback"):
        sub.add_parser(cmd).add_argument("name")
    for cmd in ("create", "update"):
        p = sub.add_parser(cmd)
        p.add_argument("name")
        p.add_argument("--spec", required=True, help="Archivo JSON con el spec del perfil.")
    p = sub.add_parser("set-callback")
    p.add_argument("name")
    p.add_argument("--url", required=True)
    p.add_argument("--secret", help="Secreto propio (≥16 caracteres).")
    p.add_argument("--generate", action="store_true", help="Genera un secreto nuevo (se muestra una sola vez).")
    p = sub.add_parser("delete")
    p.add_argument("name")
    p.add_argument("--keep-keys", action="store_true", help="No revocar las llaves del perfil.")

    args = parser.parse_args()
    init_db()
    c = args.command

    try:
        if c == "list":
            items = profiles.list_profiles()
            _emit(args, {"ok": True, "profiles": items, "builtin_tools": sorted(profiles.PROFILE_SAFE_BUILTINS)})
            if not args.json:
                for p in items:
                    estado = "activo" if p["enabled"] else "DESACTIVADO"
                    sync = "" if p["synced"] else " (sin sincronizar)"
                    cb = "callback ok" if p["callback_url"] and p["has_secret"] else "sin callback"
                    print(f"{p['name']} — {estado}{sync} — {p['keys']} llave(s) — {cb} — {p['brief'] or '(sin brief)'}")
                if not items:
                    print("No hay perfiles todavía.")
        elif c == "caps-template":
            _emit(args, {"ok": True, "caps": profiles.default_caps()}, json.dumps(profiles.default_caps(), indent=2))
        elif c == "show":
            info = profiles.get_profile_info(args.name)
            if info is None:
                _fail(args, f"No existe el perfil '{args.name}'.")
            _emit(args, {"ok": True, "profile": info}, json.dumps(info, indent=2, ensure_ascii=False))
        elif c in ("create", "update"):
            res = profiles.sync_profile(args.name, _read_spec(args), None,
                                        create_only=(c == "create"), update_only=(c == "update"))
            _emit(args, {"ok": True, **res}, f"Perfil '{args.name}' guardado." +
                  (f" Descartado: {', '.join(res['rejected'])}" if res["rejected"] else ""))
        elif c in ("enable", "disable"):
            if not profiles.set_enabled(args.name, c == "enable"):
                _fail(args, f"No existe el perfil '{args.name}'.")
            _emit(args, {"ok": True, "enabled": c == "enable"}, "Hecho.")
        elif c == "set-callback":
            res = profiles.set_callback(args.name, args.url, args.secret, args.generate)
            _emit(args, {"ok": True, **res},
                  f"Callback: {res['callback_url']}" + (f"\nSecreto (guárdalo, no se vuelve a ver): {res['secret']}" if "secret" in res else ""))
        elif c == "clear-callback":
            if not profiles.clear_callback(args.name):
                _fail(args, f"No existe el perfil '{args.name}'.")
            _emit(args, {"ok": True}, "Callback borrado.")
        elif c == "delete":
            res = profiles.delete_profile(args.name, revoke_keys=not args.keep_keys)
            if not res["deleted"] and not res["keys_revoked"]:
                _fail(args, f"No existe el perfil '{args.name}'.")
            _emit(args, {"ok": True, **res}, f"Perfil borrado. Llaves revocadas: {res['keys_revoked']}.")
    except (ValueError, LookupError) as e:
        _fail(args, str(e).strip("'\""))


if __name__ == "__main__":
    main()