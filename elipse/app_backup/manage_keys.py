"""
CLI para administrar API keys de ELIPSE (Fase 5).

Uso (con el venv activado, desde la raíz del proyecto):
    python -m app.manage_keys create telefono
    python -m app.manage_keys list
    python -m app.manage_keys revoke 3

Con --json (para consumo automático, ej. el panel de control), va SIEMPRE
justo después de "app.manage_keys" y antes del subcomando:
    python -m app.manage_keys --json create telefono
    python -m app.manage_keys --json list
    python -m app.manage_keys --json revoke 3

Existe como script local (no como endpoint HTTP) a propósito: crear tu
primera llave necesita poder correr SIN estar ya autenticado.
"""

import argparse
import json
import sys

from app.core.db import init_db
from app.core.auth import create_key, list_keys, revoke_key


def main():
    parser = argparse.ArgumentParser(description="Administra API keys de ELIPSE.")
    parser.add_argument("--json", action="store_true", help="Salida en JSON para consumo automático.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Genera una nueva API key.")
    p_create.add_argument("name", help="Nombre del dispositivo/cliente, ej. 'telefono'")

    sub.add_parser("list", help="Lista las API keys (sin mostrar el valor real).")

    p_revoke = sub.add_parser("revoke", help="Revoca una API key por su id.")
    p_revoke.add_argument("key_id", type=int)

    args = parser.parse_args()
    init_db()  # por si es la primera vez que se corre y la tabla no existe aún

    if args.command == "create":
        raw_key = create_key(args.name)
        if args.json:
            print(json.dumps({"api_key": raw_key}))
        else:
            print("Llave creada. Guárdala ahora — no se puede volver a ver:\n")
            print(f"  {raw_key}\n")
            print(f"Úsala como header: Authorization: Bearer {raw_key}")

    elif args.command == "list":
        keys = list_keys()
        if args.json:
            print(json.dumps({"keys": keys}))
        elif not keys:
            print("No hay ninguna API key creada todavía.")
        else:
            for k in keys:
                estado = "REVOCADA" if k["revoked"] else "activa"
                print(f"[{k['id']}] {k['name']} — {estado} — creada {k['created_at']} — último uso: {k['last_used_at'] or 'nunca'}")

    elif args.command == "revoke":
        ok = revoke_key(args.key_id)
        if args.json:
            print(json.dumps({"ok": ok}))
        else:
            print("Revocada." if ok else "No existe una llave con ese id.")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()