"""
Diálogo de "Preparación del entorno" (faltan paquetes del core).

A diferencia de un simple Sí/No, da tres caminos:
- Instalar aquí: crea (o reusa) el venv del proyecto y corre pip.
- Localizar Python…: abre el gestor de archivos para señalar el
  intérprete de un venv que ya tenés armado en otro lado (por ejemplo
  uno compartido entre varios proyectos, o uno con nombre no estándar
  que `find_python()` no adivina solo).
- Ahora no: cierra el aviso sin tocar nada.
"""

import tkinter as tk

from .theme import C, FT
from .widgets import Pill, lbl, safe_grab


class SetupDialog(tk.Toplevel):
    WIDTH = 460

    def __init__(self, app, message, on_install, on_locate, on_skip=None):
        super().__init__(app)
        self.title("Preparación del entorno")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(app)

        tk.Label(self, text="Preparación del entorno", font=FT, fg=C["primary"], bg=C["bg"]).pack(
            anchor="w", padx=26, pady=(22, 6))
        lbl(self, message, fg=C["muted"], wrap=self.WIDTH - 52).pack(anchor="w", padx=26, pady=(0, 18))

        bar1 = tk.Frame(self, bg=C["bg"])
        bar1.pack(fill="x", padx=26)
        Pill(bar1, "Instalar aquí", lambda: self._run(on_install), kind="filled", height=42).pack(side="left")
        Pill(bar1, "Localizar Python…", lambda: self._run(on_locate), kind="tonal", height=42).pack(side="left", padx=(8, 0))

        bar2 = tk.Frame(self, bg=C["bg"])
        bar2.pack(fill="x", padx=26, pady=(10, 22))
        Pill(bar2, "Ahora no", lambda: self._run(on_skip), kind="ghost", height=38).pack(side="right")

        self.after_idle(self._autosize)
        safe_grab(self)

    def _autosize(self):
        try:
            self.update_idletasks()
            self.geometry(f"{self.WIDTH}x{self.winfo_reqheight()}")
        except tk.TclError:
            pass

    def _run(self, fn):
        self.destroy()
        if fn:
            fn()
