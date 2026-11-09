"""
Ventana de logs en vivo (tail -f casero) para un servicio.

Si el servicio corre FUERA del panel (p. ej. Ollama como servicio de systemd o desde la bandeja de Windows),
su salida no pasa por logs/<servicio>.log. En ese caso la ventana lee los logs del sistema:
  - Linux:   journalctl -u ollama   (y ~/.ollama/logs/server.log como respaldo)
  - Windows: %LOCALAPPDATA%\\Ollama\\server.log
  - macOS:   ~/.ollama/logs/server.log
Un botón permite alternar entre «Panel» (lo que lanzó el panel) y «Sistema» (el servicio externo).
"""

import os
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path

from . import theme
from .paths import IS_LINUX, IS_WIN, LOGS
from .theme import C, rrect
from .uiqueue import ui
from .widgets import Pill

TAIL_BYTES = 60000
BIND_ERRORS = ("only one usage of each socket address", "address already in use")


def _tail_file(path: Path, nbytes=TAIL_BYTES):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "replace")


def external_log(key: str):
    """Logs de un servicio que NO lanzó el panel -> (origen, texto). Nunca lanza."""
    if key != "ollama":
        return "sin fuente externa", ""
    try:
        if IS_WIN:
            cands = [Path(os.path.expandvars(r"%LOCALAPPDATA%\Ollama\server.log"))]
        else:
            cands = [Path.home() / ".ollama" / "logs" / "server.log"]
        if IS_LINUX and shutil.which("journalctl"):
            for extra in ([], ["--user"]):
                r = subprocess.run(["journalctl", *extra, "-u", "ollama", "-n", "400", "--no-pager", "-o", "short-iso"],
                                   capture_output=True, text=True, timeout=4)
                out = (r.stdout or "").strip()
                if r.returncode == 0 and out and "-- No entries --" not in out:
                    return "journalctl -u ollama", out
        for p in cands:
            if p.exists():
                return str(p), _tail_file(p)
        return "sin fuente externa", ""
    except Exception as e:
        return "sin fuente externa", f"No pude leer los logs del sistema: {e}"


class LogWindow(tk.Toplevel):
    def __init__(self, app, svc):
        super().__init__(app)
        self.svc = svc
        self.path, self._sz = LOGS / f"{svc.key}.log", -1
        self._mode = "auto"          # auto | panel | system
        self._busy = False
        self._last_ext = None
        self.title(f"Logs de {svc.name}")
        self.geometry("860x500")
        self.configure(bg=C["bg"])

        top = tk.Frame(self, bg=C["bg"])
        top.pack(fill="x", padx=22, pady=(16, 8))
        tk.Label(top, text=svc.name, font=theme.FT, fg=C["primary"], bg=C["bg"]).pack(side="left")
        self.src = tk.Label(top, text="", font=theme.FS, fg=C["dim"], bg=C["bg"])
        self.src.pack(side="left", padx=14)

        self.btn_bottom = Pill(top, "↓ Final", self._go_bottom, kind="ghost", width=90, height=36, bg=C["bg"])
        self.btn_bottom.pack(side="right")
        if svc.key == "ollama":
            self.btn_src = Pill(top, "Fuente: auto", self._cycle_source, kind="ghost", width=130, height=36, bg=C["bg"])
            self.btn_src.pack(side="right", padx=(0, 8))

        self.body = tk.Frame(self, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)

        self.txt = tk.Text(
            self.body, bg=C["card"], fg=C["text"], font=theme.FMONO, relief="flat",
            bd=0, padx=16, pady=12, wrap="none", highlightthickness=0, state="disabled",
        )
        sy = tk.Scrollbar(self.body, orient="vertical", command=self.txt.yview)
        sx = tk.Scrollbar(self.body, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=lambda a, b: (sy.set(a, b), self._check_scroll()),
                           xscrollcommand=sx.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")

        self.float_btn = tk.Canvas(self.body, width=40, height=40, bg=C["card"], highlightthickness=0, cursor="hand2")
        rrect(self.float_btn, 2, 2, 38, 38, 12, fill=C["primary_c"], outline="")
        self.float_btn.create_text(20, 20, text="↓", fill=C["on_primary_c"], font=("Segoe UI", 14, "bold"))
        self.float_btn.bind("<Button-1>", lambda e: self._go_bottom())

        self._refresh()

    # ---------------------------------------------------------------- scroll
    def _go_bottom(self):
        try:
            self.txt.see("end")
            self.txt.yview_moveto(1.0)
            self._hide_float()
        except tk.TclError:
            pass

    def _at_bottom(self):
        try:
            return self.txt.yview()[1] >= 0.98
        except tk.TclError:
            return True

    def _check_scroll(self):
        if self._at_bottom():
            self._hide_float()
        else:
            self._show_float()

    def _show_float(self):
        try:
            self.float_btn.place(in_=self.txt, relx=1.0, rely=1.0, x=-16, y=-16, anchor="se")
            self.float_btn.lift()
        except tk.TclError:
            pass

    def _hide_float(self):
        try:
            self.float_btn.place_forget()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- fuente
    def _cycle_source(self):
        self._mode = {"auto": "panel", "panel": "system", "system": "auto"}[self._mode]
        self.btn_src.set({"auto": "Fuente: auto", "panel": "Fuente: panel", "system": "Fuente: sistema"}[self._mode])
        self._sz, self._last_ext = -1, None      # fuerza redibujar ya
        self._refresh(reschedule=False)

    def _panel_text(self):
        try:
            sz = self.path.stat().st_size
        except OSError:
            return None, ""
        return sz, _tail_file(self.path)

    def _wants_system(self, panel_text):
        """auto: el servicio está corriendo pero no lo lanzó el panel, o el último arranque del panel chocó con el puerto."""
        if self.svc.key != "ollama":
            return False
        if self._mode == "system":
            return True
        if self._mode == "panel":
            return False
        last_start = panel_text.rsplit("iniciando", 1)[-1].lower()
        return bool(self.svc.external) or any(e in last_start for e in BIND_ERRORS)

    # ---------------------------------------------------------------- refresco
    def _refresh(self, reschedule=True):
        if not self.winfo_exists():
            return
        sz, panel_text = self._panel_text()
        if self._wants_system(panel_text):
            if not self._busy:
                self._busy = True
                threading.Thread(target=self._load_external, daemon=True).start()
            delay = 2500
        else:
            if sz != self._sz:
                self._sz = sz
                if sz is None:
                    self._show("Sin logs todavía.\n\nSi el servicio se inició fuera del panel, su salida no se captura aquí.",
                               "panel (vacío)")
                else:
                    self._show(panel_text, "lanzado por el panel")
            delay = 800
        if reschedule:
            self.after(delay, self._refresh)

    def _load_external(self):
        try:
            origin, text = external_log(self.svc.key)
        finally:
            self._busy = False
        ui(self._apply_external, origin, text)

    def _apply_external(self, origin, text):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if not text.strip():
            text = ("Ollama está corriendo fuera del panel y no encontré sus logs.\n\n"
                    "• Linux: journalctl -u ollama -f\n"
                    "• Windows: %LOCALAPPDATA%\\Ollama\\server.log\n\n"
                    "Si quieres que el panel capture todo, usa «Detener» (apaga también el servicio externo) y vuelve a «Iniciar».")
        if text == self._last_ext:
            return
        self._last_ext = text
        self._show(text, f"servicio externo · {origin}")

    def _show(self, text, origin):
        bottom = self._at_bottom()
        self.src.config(text=origin)
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("end", text)
        self.txt.config(state="disabled")
        if bottom:
            self.txt.see("end")
            self._hide_float()
        else:
            self._show_float()
