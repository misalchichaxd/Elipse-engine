"""
Ventana principal del panel: tarjetas de Ollama / ELIPSE Core, barra de
acciones, avisos de preparación del entorno (venv, paquetes faltantes,
instalación de Ollama), y el loop de detección de estado en background.
"""

import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

from . import theme
from .env_store import load_prefs, save_prefs
from .log_window import LogWindow
from .network import probe
from .paths import CLIENT_FILE, CORE_PACKAGES, IS_WIN, LOGS, OLLAMA_PORT, PORT, ROOT
from .process_utils import (
    check_missing_packages,
    ensure_project_layout,
    ensure_venv,
    find_ollama,
    find_python,
    kill_ollama,
    kill_port,
    launch_elipse,
    launch_ollama,
)
from .paths import OLLAMA_INSTALL_SH, OLLAMA_INSTALLER_WIN
from .services import BUSY, Card, Service
from .settings_window import Settings
from .setup_dialog import SetupDialog
from .theme import C, FB, FD
from .nav import Navigator, Page
from .uiqueue import UIQ, ui
from .widgets import Pill, lbl, style_ttk


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        theme.init_fonts()
        self.title("ELIPSE")
        self.geometry("640x560")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        style_ttk(self)
        self._closing, self._toast_id, self._win, self._errs = False, None, {}, 0
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.report_callback_exception = self._report_callback_exception

        # Navegación: inicio y páginas (Ajustes, Voz, Skills…) comparten esta misma ventana
        self.stage = tk.Frame(self, bg=C["bg"])
        self.stage.pack(fill="both", expand=True)
        self.home = tk.Frame(self.stage, bg=C["bg"])
        self.home.pack(fill="both", expand=True)
        self.nav = Navigator(self, self.home)

        # Carpetas, .env y avisos de dependencias (no bloquea la UI)
        self._setup_notes, self._missing_py = ensure_project_layout()

        self.services = [
            Service(self, "ollama", "Ollama", OLLAMA_PORT, "", launch_ollama, kill_ollama, installed=lambda: bool(find_ollama())),
            Service(self, "elipse", "ELIPSE Core", PORT, "/v1/status", launch_elipse, lambda: kill_port(PORT)),
        ]
        self.svc = {s.key: s for s in self.services}

        hd = tk.Canvas(self.home, width=640, height=112, bg=C["bg"], highlightthickness=0)
        hd.pack()
        hd.create_polygon(theme.cookie(62, 58, 32, amp=0.12), fill=C["primary_c"], outline="")
        hd.create_polygon([c for x, y in theme.BOLT for c in (62 + x * 1.1, 58 + y * 1.1)], fill=C["warm"], outline="")
        hd.create_text(112, 48, anchor="w", text="ELIPSE", font=FD, fill=C["primary"])
        hd.create_text(114, 84, anchor="w", text="Panel de control", font=FB, fill=C["muted"])
        hd.create_window(620, 58, window=Pill(hd, "Ajustes", self.open_settings, height=42), anchor="e")

        for s in self.services:
            s.card = Card(self.home, s)
            s.card.pack(pady=7)
            s.card.refresh()

        bar = tk.Frame(self.home, bg=C["bg"])
        bar.pack(fill="x", padx=20, pady=(12, 0))
        self.all_btn = Pill(bar, "Iniciar todo", self.toggle_all, kind="filled", width=170, height=48)
        self.all_btn.pack(side="right")
        Pill(bar, "Abrir consola", self.open_console, kind="tonal", height=48).pack(side="right", padx=10)
        lbl(self.home, "Al cerrar el panel, lo que iniciaste aquí sigue corriendo. Usa Detener para apagarlo.",
            fg=C["dim"], wrap=600).pack(anchor="w", padx=24, pady=(16, 0))

        self.toast_lbl = tk.Label(self, bg="#322F35", fg=C["text"], font=FB, padx=20, pady=11)
        self._pump()
        self._tick()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if self._setup_notes or self._missing_py:
            self.after(600, lambda: self._show_setup_notes(self._setup_notes, self._missing_py))

    # -- errores: nunca mueren en silencio --
    def _report_callback_exception(self, exc, val, tb):
        msg = "".join(traceback.format_exception(exc, val, tb))
        traceback.print_exception(exc, val, tb)
        try:
            LOGS.mkdir(exist_ok=True)
            with open(LOGS / "panel_error.log", "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{msg}")
        except Exception:
            pass
        self._errs += 1
        if self._errs <= 3:  # evita una lluvia de ventanas si el error se repite
            try:
                messagebox.showerror("Error en el panel",
                                     f"{val}\n\nDetalle guardado en logs/panel_error.log\n\n{msg[-900:]}")
            except Exception:
                pass

    # -- motores internos --
    def _pump(self):
        try:
            while True:
                fn, args = UIQ.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    self._report_callback_exception(*sys.exc_info())
        except Exception:
            pass
        self.after(60, self._pump)

    def _tick(self):
        for s in self.services:
            s.card.spin()
        self.after(40, self._tick)

    def _poll_loop(self):
        while not self._closing:
            for s in self.services:
                ui(s.observe, probe(s.port, s.path))
            time.sleep(2.5)

    def on_change(self, svc):
        svc.card.refresh()
        busy = any(s.state in BUSY for s in self.services)
        every = all(s.state == "on" for s in self.services)
        self.all_btn.set("Detener todo" if every else "Iniciar todo", "danger" if every else "filled", not busy)

    # -- acciones --
    def toggle_all(self):
        if any(s.state in BUSY for s in self.services):
            return
        if all(s.state == "on" for s in self.services):
            for s in self.services:
                s.stop()
        else:
            for s in self.services:
                if s.state == "off" and s.installed():
                    s.start()

    def open_settings(self):
        self._single("settings", lambda: Settings(self))

    def open_logs(self, svc):
        self._single("log_" + svc.key, lambda: LogWindow(self, svc))

    def _single(self, key, make):
        w = self._win.get(key)
        if w and w.winfo_exists():
            if isinstance(w, Page):
                self.nav.show(w)        # ya está abierta: volver a ella
            else:
                w.lift()                # ventanas aparte (logs)
        else:
            self._win[key] = make()

    def open_console(self):
        p = ROOT / CLIENT_FILE
        if p.exists():
            webbrowser.open(p.as_uri())
        else:
            self.toast(f"No encontré {CLIENT_FILE} junto al panel")

    def _show_setup_notes(self, notes, missing_py=None):
        """Avisa de .env creado, app/ faltante o paquetes Python sin instalar.
        Si faltan paquetes del core, ofrece: crear un venv acá e instalarlos,
        localizar a mano el Python de un venv que ya tengas, o posponerlo."""
        missing_py = missing_py or []
        other = [n for n in (notes or []) if "paquetes Python" not in n]
        if missing_py:
            pkgs = ", ".join(missing_py)
            py = find_python()
            py_norm = py.replace("\\", "/")
            using_venv = (
                any(f"/{n}/" in py_norm for n in ("venv", ".venv", "env", "virtualenv"))
                or py == load_prefs().get("python_path")
            )
            extra = "\n\n".join(other) if other else ""
            if using_venv:
                where = f"Usará el venv del proyecto:\n{py}"
            else:
                where = (
                    "No hay venv en esta carpeta. «Instalar aquí» crea «.venv» acá "
                    "e instala ahí; «Localizar Python…» te deja elegir el intérprete "
                    "de un venv que ya tengas armado en otro lado."
                )
            msg = f"Faltan paquetes Python del core:\n{pkgs}\n\n{where}" + (f"\n\n{extra}" if extra else "")

            def skip():
                if other:
                    messagebox.showwarning("Preparación del entorno", "\n\n".join(other), parent=self)

            SetupDialog(
                self, msg,
                on_install=lambda: self.install_python_packages(missing_py),
                on_locate=self.locate_python,
                on_skip=skip if other else None,
            )
            return
        if not notes:
            return
        msg = "\n\n".join(notes)
        serious = any("app/" in n or "No pude" in n for n in notes)
        if serious:
            messagebox.showwarning("Preparación del entorno", msg, parent=self)
        else:
            self.toast(notes[0][:120])

    def locate_python(self):
        """Deja al usuario señalar a mano el intérprete Python de un venv ya
        existente (por si `find_python()` no lo encuentra solo: nombre no
        estándar, venv compartido fuera del proyecto, etc.). Se guarda en
        panel_prefs.json y `find_python()` lo prioriza en adelante."""
        filetypes = [("python.exe", "python.exe"), ("Todos los archivos", "*.*")] if IS_WIN \
            else [("Ejecutables Python", "python*"), ("Todos los archivos", "*")]
        hint = (
            "Elegí el archivo python.exe dentro de la carpeta Scripts\\ de tu venv"
            if IS_WIN else
            "Elegí el archivo «python» dentro de la carpeta bin/ de tu venv"
        )
        self.toast(hint)
        path = filedialog.askopenfilename(
            parent=self,
            title="Localizar intérprete Python de un venv",
            initialdir=str(ROOT),
            filetypes=filetypes,
        )
        if not path:
            return
        p = Path(path)
        if not p.exists():
            messagebox.showerror("No encontrado", f"No existe:\n{path}", parent=self)
            return

        prefs = load_prefs()
        prefs["python_path"] = str(p)
        save_prefs(prefs)
        self.toast(f"Usando Python: {p}")

        def run():
            still = check_missing_packages()
            ui(self._after_locate, str(p), still)
        threading.Thread(target=run, daemon=True).start()

    def _after_locate(self, py_path, still_missing):
        if still_missing:
            if messagebox.askyesno(
                "Todavía faltan paquetes",
                f"Con ese Python ({py_path}) siguen faltando:\n{', '.join(still_missing)}\n\n"
                "¿Instalarlos ahí ahora?",
                parent=self,
            ):
                self.install_python_packages(still_missing)
        else:
            self._missing_py = []
            messagebox.showinfo(
                "Listo",
                f"Ese entorno ya tiene todo lo necesario:\n{py_path}\n\nYa podés iniciar ELIPSE Core.",
                parent=self,
            )

    def install_python_packages(self, packages=None):
        """Crea .venv si hace falta e instala requirements.txt (o los paquetes del core)."""
        from .paths import FLAGS

        packages = packages or list(CORE_PACKAGES)
        req = ROOT / "requirements.txt"
        self.toast("Preparando entorno e instalando paquetes…")

        def run():
            try:
                LOGS.mkdir(exist_ok=True)
                log_path = LOGS / "pip_install.log"
                # 1) Asegurar venv local (o respetar uno localizado a mano con
                # `locate_python`, aunque su carpeta no se llame venv/.venv/env)
                py = find_python()
                py_norm = py.replace("\\", "/")
                has_local = (
                    any(f"/{n}/" in py_norm for n in ("venv", ".venv", "env", "virtualenv"))
                    or py == load_prefs().get("python_path")
                )
                if not has_local:
                    with open(log_path, "ab") as logf:
                        logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} creando .venv =====\n".encode())
                        logf.flush()
                    ui(self.toast, "Creando entorno virtual .venv…")
                    py = ensure_venv()
                    with open(log_path, "ab") as logf:
                        logf.write(f"venv listo: {py}\n".encode())
                # 2) Instalar
                if req.exists():
                    cmd = [py, "-m", "pip", "install", "-r", str(req)]
                    what = "requirements.txt"
                else:
                    cmd = [py, "-m", "pip", "install"] + list(packages)
                    what = ", ".join(packages)
                ui(self.toast, f"Instalando {what}… (puede tardar)")
                with open(log_path, "ab") as logf:
                    logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} pip install =====\n".encode())
                    logf.write((" ".join(cmd) + "\n").encode())
                    logf.flush()
                    r = subprocess.run(
                        cmd, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                        text=False, timeout=600, creationflags=FLAGS,
                    )
                if r.returncode == 0:
                    still = check_missing_packages()
                    if still:
                        ui(messagebox.showwarning, "Instalación incompleta",
                           f"pip terminó, pero aún faltan: {', '.join(still)}\n\n"
                           f"Revisá logs/pip_install.log")
                    else:
                        ui(self.toast, "Listo: .venv + paquetes instalados. Ya podés iniciar ELIPSE Core.")
                        self._missing_py = []
                else:
                    ui(messagebox.showerror, "Error al instalar paquetes",
                       f"pip falló (código {r.returncode}) al instalar {what}.\n\n"
                       f"Detalle en logs/pip_install.log\n\n"
                       f"Probá a mano en la raíz del proyecto:\n"
                       f"  {py} -m pip install -r requirements.txt")
            except subprocess.TimeoutExpired:
                ui(messagebox.showerror, "Instalación agotó el tiempo",
                   "La instalación de paquetes tardó demasiado (>10 min).\n"
                   "Revisá logs/pip_install.log o instalá a mano.")
            except Exception as e:
                ui(messagebox.showerror, "No se pudo preparar el entorno",
                   f"{e}\n\nPodés crear el venv a mano:\n"
                   f"  python -m venv .venv\n"
                   f"  .venv/bin/python -m pip install -r requirements.txt\n"
                   f"(en Windows: .venv\\Scripts\\python.exe …)")
        threading.Thread(target=run, daemon=True).start()

    def install_ollama(self):
        """Descarga/instala Ollama en Windows o Linux (script oficial)."""
        if IS_WIN:
            self.toast("Descargando el instalador de Ollama…")

            def run_win():
                try:
                    import os
                    path = os.path.join(tempfile.gettempdir(), "OllamaSetup.exe")
                    urllib.request.urlretrieve(OLLAMA_INSTALLER_WIN, path)
                    subprocess.Popen([path])
                    ui(self.toast, "Instalador abierto. Sigue los pasos en esa ventana.")
                except Exception as e:
                    ui(messagebox.showerror, "No se pudo descargar Ollama",
                       f"{e}\n\nDescárgalo desde https://ollama.com/download")
            threading.Thread(target=run_win, daemon=True).start()
            return

        # Linux / otros: script oficial (requiere curl o wget)
        if not messagebox.askyesno(
            "Instalar Ollama",
            "Se va a ejecutar el instalador oficial de Ollama:\n\n"
            "  curl -fsSL https://ollama.com/install.sh | sh\n\n"
            "Puede pedir contraseña de administrador. ¿Continuar?",
            parent=self,
        ):
            webbrowser.open("https://ollama.com/download")
            return

        self.toast("Instalando Ollama… (puede pedir sudo)")

        def run_linux():
            import shutil
            try:
                if shutil.which("curl"):
                    cmd = f"curl -fsSL {OLLAMA_INSTALL_SH} | sh"
                elif shutil.which("wget"):
                    cmd = f"wget -qO- {OLLAMA_INSTALL_SH} | sh"
                else:
                    ui(messagebox.showerror, "Falta curl o wget",
                       "Instalá curl o wget, o descargá Ollama desde https://ollama.com/download")
                    return
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=600)
                if r.returncode == 0 and find_ollama():
                    ui(self.toast, "Ollama instalado. Ya podés iniciarlo desde el panel.")
                    ui(self.svc["ollama"].card.refresh)
                else:
                    tail = ((r.stderr or r.stdout or "")[-500:]) or f"código {r.returncode}"
                    ui(messagebox.showerror, "Instalación de Ollama",
                       f"No se completó bien.\n\n{tail}\n\nProba desde https://ollama.com/download")
            except Exception as e:
                ui(messagebox.showerror, "No se pudo instalar Ollama",
                   f"{e}\n\nDescárgalo desde https://ollama.com/download")
        threading.Thread(target=run_linux, daemon=True).start()

    def toast(self, msg):
        self.toast_lbl.config(text=msg)
        self.toast_lbl.place(relx=.5, rely=1.0, y=-22, anchor="s")
        self.toast_lbl.lift()
        if self._toast_id:
            self.after_cancel(self._toast_id)
        self._toast_id = self.after(3600, self.toast_lbl.place_forget)

    def close(self):
        self._closing = True
        self.destroy()
