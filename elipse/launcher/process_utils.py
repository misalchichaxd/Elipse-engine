"""
Todo lo que toca procesos del sistema operativo: encontrar el Python y el
Ollama correctos, crear el venv, prender/apagar servicios, matar puertos,
y correr `manage_keys.py`.
"""

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .env_store import load_prefs
from .paths import FLAGS, IS_WIN, LOGS, OLLAMA_PORT, PORT, ROOT, CORE_PACKAGES


def _venv_python_path(venv_dir):
    """Ruta al intérprete dentro de un directorio venv (Win/Linux/macOS)."""
    return Path(venv_dir) / ("Scripts/python.exe" if IS_WIN else "bin/python")


def find_python():
    """Prefiere el venv del proyecto (venv, .venv, env, …); si no hay, usa el Python actual."""
    for name in ("venv", ".venv", "env", "virtualenv"):
        p = _venv_python_path(ROOT / name)
        if p.exists():
            return str(p)
    # Preferencia guardada (venv externo u otra ruta)
    try:
        custom = load_prefs().get("python_path")
        if custom and Path(custom).exists():
            return custom
    except Exception:
        pass
    return sys.executable


def ensure_venv():
    """Crea ROOT/.venv si no hay ningún venv local. Devuelve la ruta al python del venv o None."""
    for name in ("venv", ".venv", "env", "virtualenv"):
        p = _venv_python_path(ROOT / name)
        if p.exists():
            return str(p)
    venv_dir = ROOT / ".venv"
    py = sys.executable
    r = subprocess.run(
        [py, "-m", "venv", str(venv_dir)],
        capture_output=True, text=True, timeout=120, creationflags=FLAGS,
    )
    if r.returncode != 0:
        raise RuntimeError(
            (r.stderr or r.stdout or f"código {r.returncode}").strip()[:400]
        )
    out = _venv_python_path(venv_dir)
    if not out.exists():
        raise RuntimeError(f"Se creó {venv_dir} pero no encontré {out}")
    return str(out)


def find_ollama():
    """Ubicación del binario de Ollama. Cubre instalación de sistema, por
    usuario (~/.local/bin) y snap, que es donde suele fallar la detección
    en distros que no agregan esas rutas al PATH de apps gráficas."""
    exe = shutil.which("ollama")
    if exe:
        return exe
    if IS_WIN:
        for p in (
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
            os.path.expandvars(r"%PROGRAMFILES%\Ollama\ollama.exe"),
        ):
            if p and os.path.exists(p):
                return p
        return None
    # Linux / macOS: rutas habituales, incluyendo snap e instalación por usuario
    for p in (
        "/usr/local/bin/ollama", "/usr/bin/ollama", "/snap/bin/ollama",
        os.path.expanduser("~/.local/bin/ollama"), os.path.expanduser("~/bin/ollama"),
    ):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return None


def check_missing_packages():
    """Comprueba con el Python del proyecto (venv preferido) qué paquetes del core faltan."""
    py = find_python()
    missing = []
    for mod in CORE_PACKAGES:
        try:
            r = subprocess.run(
                [py, "-c", f"import {mod}"],
                capture_output=True, text=True, timeout=8, creationflags=FLAGS,
            )
            if r.returncode != 0:
                missing.append(mod)
        except Exception:
            missing.append(mod)
    return missing


def ensure_project_layout():
    """Crea logs/, .env básico y comprueba que exista app/. Detecta paquetes faltantes."""
    from .paths import ENV_PATH, ENV_TEMPLATE  # import tardío: evita ciclos, uso puntual

    notes = []
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        notes.append(f"No pude crear logs/: {e}")

    if not ENV_PATH.exists():
        try:
            ENV_PATH.write_text(ENV_TEMPLATE, encoding="utf-8")
            notes.append("Se creó un .env básico (DEFAULT_PROVIDER=ollama).")
        except Exception as e:
            notes.append(f"No pude crear .env: {e}")

    if not (ROOT / "app").is_dir():
        notes.append("No está la carpeta app/ junto al panel. ELIPSE Core no podrá arrancar hasta que copies el proyecto completo.")

    missing_py = check_missing_packages()
    if missing_py:
        notes.append(
            "Faltan paquetes Python del core: "
            + ", ".join(missing_py)
            + ". El panel puede instalarlos por vos."
        )
    return notes, missing_py


def kill_port(port):
    if IS_WIN:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=FLAGS).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                subprocess.run(["taskkill", "/F", "/T", "/PID", line.split()[-1]], capture_output=True, creationflags=FLAGS)
        return
    # Linux/macOS: lsof → fuser → ss
    killed = False
    if shutil.which("lsof"):
        r = subprocess.run(f"lsof -ti:{port}", shell=True, capture_output=True, text=True)
        pids = (r.stdout or "").strip().split()
        for pid in pids:
            subprocess.run(["kill", "-9", pid], capture_output=True)
            killed = True
    if not killed and shutil.which("fuser"):
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        killed = True
    if not killed and shutil.which("ss"):
        r = subprocess.run(["ss", "-lptn", f"sport = :{port}"], capture_output=True, text=True)
        import re as _re
        for m in _re.finditer(r"pid=(\d+)", r.stdout or ""):
            subprocess.run(["kill", "-9", m.group(1)], capture_output=True)


def kill_ollama():
    """Detiene Ollama en Win/Linux/macOS. En Ubuntu suele ser un servicio systemd (user o system)."""
    if IS_WIN:
        # Primero la app de bandeja; si no, vuelve a levantar el servidor
        for exe in ("ollama app.exe", "ollama.exe"):
            subprocess.run(["taskkill", "/F", "/T", "/IM", exe], capture_output=True, creationflags=FLAGS)
        return

    # 1) systemd: instalaciones oficiales de Ollama en Linux
    if shutil.which("systemctl"):
        # servicio de usuario (sin root)
        subprocess.run(
            ["systemctl", "--user", "stop", "ollama"],
            capture_output=True, timeout=15,
        )
        # servicio de sistema (puede fallar sin permisos; no pasa nada)
        subprocess.run(
            ["systemctl", "stop", "ollama"],
            capture_output=True, timeout=15,
        )
        # por si el unit tiene otro nombre
        for unit in ("ollama.service", "ollama"):
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                capture_output=True, timeout=10,
            )

    # 2) procesos sueltos (serve / runner)
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-f", "ollama"], capture_output=True)
        import time
        time.sleep(0.4)
        subprocess.run(["pkill", "-9", "-f", "ollama"], capture_output=True)

    # 3) por si sigue escuchando el puerto
    kill_port(OLLAMA_PORT)


def launch_ollama(logf):
    return subprocess.Popen([find_ollama() or "ollama", "serve"], stdout=logf, stderr=subprocess.STDOUT, creationflags=FLAGS)


def launch_elipse(logf):
    if not (ROOT / "app").is_dir():
        raise RuntimeError("no hay carpeta app/ junto al panel; copia este archivo a la raíz del proyecto")
    host = "0.0.0.0" if load_prefs().get("lan") else "127.0.0.1"
    return subprocess.Popen([find_python(), "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(PORT)],
                            cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, creationflags=FLAGS)


def run_keys(args):
    """python -m app.manage_keys --json <args>. (revoke devuelve código 1 si no existe: es válido)."""
    r = subprocess.run([find_python(), "-m", "app.manage_keys", "--json"] + args, cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=30, creationflags=FLAGS)
    if r.returncode not in (0, 1):
        raise RuntimeError((r.stderr.strip().splitlines() or [f"código de salida {r.returncode}"])[-1])
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        tail = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["Sin salida del comando."])[-1]
        raise RuntimeError(tail)


def run_skills(args, timeout=30):
    """python -m app.core.skills --json <args>. Devuelve el dict JSON (puede traer status "error": es válido)."""
    r = subprocess.run([find_python(), "-m", "app.core.skills", "--json"] + args, cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=FLAGS)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        tail = (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["Sin salida del comando."])[-1]
        raise RuntimeError(tail)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None
