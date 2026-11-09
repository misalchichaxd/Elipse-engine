"""
Actualiza app/readme.md con el estado real de los perfiles (Fases 0–4) y lo que falta.
Uso (raíz del proyecto, con readme_perfiles.md en la misma carpeta):   python patch_readme.py
Hace 4 inserciones; si un ancla no aparece EXACTAMENTE una vez, no escribe nada.
"""
import sys
from pathlib import Path

ROOT = Path(".")
README = ROOT / "app" / "readme.md"
SECCION = Path(__file__).with_name("readme_perfiles.md")
if not README.exists() or not SECCION.exists():
    sys.exit("✗ Falta app/readme.md o readme_perfiles.md (deben estar: el primero en app/, el segundo junto a este script).")

raw = README.read_bytes().decode("utf-8")
crlf = "\r\n" in raw
lines = raw.replace("\r\n", "\n").split("\n")
if any(l.startswith("## Perfiles de anfitrión (multi-tenant)") for l in lines):
    sys.exit("✗ El readme ya tiene la sección de perfiles. Edítala a mano o bórrala antes de volver a correr esto.")


def find(prefix):
    idx = [i for i, l in enumerate(lines) if l.startswith(prefix)]
    if len(idx) != 1:
        sys.exit(f"✗ El ancla «{prefix[:60]}» aparece {len(idx)} veces (debe ser 1). Nada escrito.")
    return idx[0]


seccion = SECCION.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n").split("\n") + [""]

# 1) fecha de actualización
i = find("> _Última actualización de este documento:")
lines[i] = ("> _Última actualización de este documento: 2026-10-29 (se agregó el apartado **Perfiles de anfitrión**: "
            "Fases 0–4 hechas —aislamiento por perfil y callback HMAC— y la lista de lo que falta)._")

# 2) aviso junto a "Fase actual"
i = find("> **Fase actual del plan:**")
lines[i + 1:i + 1] = ["", "> **Perfiles (multi-anfitrión, p. ej. RBE):** Fases 0–4 hechas y con 74 tests; falta la Fase 5 (SDK + migrar RBE). "
                      "Todo está en el apartado **«Perfiles de anfitrión»** (más abajo): qué hay, cómo se hizo y qué sigue."]

# 3) árbol de archivos
i = find("    ├── manage_keys.py")
lines[i + 1:i + 1] = ["    ├── manage_profiles.py       # CLI para crear/editar/activar/borrar perfiles y su callback (ver «Perfiles de anfitrión»)"]
i = find("        ├── tasks.py")
lines[i:i] = ["        ├── profiles.py            # Perfiles: spec ∩ caps, Capabilities, gestión y callback config (ver «Perfiles de anfitrión»)",
              "        ├── profiles_api.py        # Router /v1/profiles",
              "        ├── callback.py            # Callback HMAC al anfitrión: firma, verificación y despacho de tools del anfitrión"]

# 4) la sección grande, antes de "Instalación de dependencias"
i = find("## Instalación de dependencias")
lines[i:i] = seccion

# 5) puntero en pendientes
i = find("## Notas / cosas pendientes conocidas")
lines[i + 1:i + 1] = ["", "- **Perfiles:** la lista completa y ordenada de lo que falta (Fase 5, E8, E6, E16, tools por abrir…) está en «Perfiles de anfitrión → Lo que falta»."]

out = "\n".join(lines)
README.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
print("✓ app/readme.md actualizado" + (" (CRLF conservado)" if crlf else ""))
