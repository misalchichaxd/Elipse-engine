"""
Navegación por páginas dentro de la ventana principal (en vez de una ventana nueva por cada cosa).

    Inicio  →  Ajustes  →  Voz / Skills / Proveedor      (con «← Volver» y migas de pan)

`Page` se comporta como un tk.Toplevel para el código que ya existía: acepta title(), geometry(),
resizable(), transient(), grab_set()… y destroy() = "cerrar y volver a la página anterior".
Por eso ProviderDialog, VoiceDialog, Settings y SkillsDialog solo cambian de clase base.

Siguen siendo ventanas aparte a propósito: los Logs (consola) y los avisos modales chicos.
"""

import re
import tkinter as tk

from . import theme
from .theme import C
from .widgets import Pill

HOME_SIZE = (640, 560)
SCREEN_MARGIN = 100          # lo mismo que usaban los diálogos: nunca más alto que la pantalla


class Page(tk.Frame):
    """Una "pantalla" que vive dentro de la ventana principal."""

    WIDTH = 780

    def __init__(self, app, **_):
        super().__init__(app.stage, bg=C["bg"])
        self._nav_app = app
        self._crumb = ""
        self._want = None            # (ancho, alto) del contenido que pidió la página
        self._presented = False
        self._closed = False
        # Se muestra al terminar el __init__ de la subclase: evita parpadeos de una página a medio armar.
        self.after_idle(self._present)

    def _present(self):
        if not self._closed and not self._presented:
            self._presented = True
            self._nav_app.nav.push(self)

    # ---- compatibilidad con tk.Toplevel (el código existente sigue igual) ----
    def title(self, text=None):
        if text is None:
            return self._crumb
        t = text.replace(" de ELIPSE", "")
        self._crumb = t.split("—")[-1].strip() if "—" in t else t.strip()
        if self._presented:
            self._nav_app.nav.refresh_bar()

    def geometry(self, spec=None):
        m = re.match(r"^(\d+)x(\d+)", spec or "")
        if not m:
            return
        self._want = (int(m.group(1)), int(m.group(2)))
        if self._presented:
            self._nav_app.nav.resize(self)

    def resizable(self, *a, **k): pass
    def minsize(self, *a, **k): pass
    def transient(self, *a, **k): pass
    def grab_set(self): pass
    def wait_visibility(self, *a, **k): pass

    def destroy(self):
        """Cerrar = volver a la página anterior."""
        if not self._closed:
            self._closed = True
            try:
                self._nav_app.nav.pop(self)
            except tk.TclError:
                pass
        super().destroy()


class Navigator:
    def __init__(self, app, home):
        self.app, self.home, self.stack = app, home, []
        self.bar = tk.Frame(app, bg=C["bg"])
        self.back_btn = Pill(self.bar, "←  Volver", self.back, kind="tonal", height=36)
        self.back_btn.pack(side="left")
        self.crumbs = tk.Frame(self.bar, bg=C["bg"])
        self.crumbs.pack(side="left", padx=(14, 0))
        app.bind("<Escape>", lambda e: self.back() if self.stack else None)

    # ---- API ----
    @property
    def current(self):
        return self.stack[-1] if self.stack else self.home

    def push(self, page):
        self.current.pack_forget()
        self.stack.append(page)
        self.bar.pack(side="top", fill="x", padx=22, pady=(14, 0), before=self.app.stage)
        page.pack(fill="both", expand=True)
        self.refresh_bar()
        self.resize(page)
        page.focus_set()

    def pop(self, page):
        if page not in self.stack:
            return
        was_top = self.stack[-1] is page
        self.stack.remove(page)
        page.pack_forget()
        if not was_top:
            return
        if getattr(self.app, "_closing", False):
            return
        cur = self.current
        cur.pack(fill="both", expand=True)
        if not self.stack:
            self.bar.pack_forget()
        self.refresh_bar()
        self.resize(cur)

    def back(self):
        if self.stack:
            self.stack[-1].destroy()

    def show(self, page):
        """Vuelve a `page` cerrando lo que esté encima."""
        while self.stack and self.stack[-1] is not page:
            self.stack[-1].destroy()

    def go_home(self):
        while self.stack:
            self.stack[-1].destroy()

    # ---- barra superior ----
    def refresh_bar(self):
        for w in self.crumbs.winfo_children():
            w.destroy()
        parts = [("ELIPSE", None)] + [(p._crumb or "…", p) for p in self.stack]
        for i, (text, page) in enumerate(parts):
            last = i == len(parts) - 1
            if i:
                tk.Label(self.crumbs, text="›", font=theme.FB, fg=C["dim"], bg=C["bg"]).pack(side="left", padx=6)
            l = tk.Label(self.crumbs, text=text, font=theme.FBB if last else theme.FB,
                         fg=C["primary"] if last else C["muted"], bg=C["bg"],
                         cursor="" if last else "hand2")
            l.pack(side="left")
            if not last:
                l.bind("<Button-1>", lambda e, p=page: self.show(p) if p else self.go_home())

    # ---- tamaño de la ventana ----
    def resize(self, page):
        """Ajusta la ventana al tamaño que pidió la página (más la barra superior)."""
        app = self.app
        try:
            if page is self.home:
                w, h = HOME_SIZE
                total = h
            else:
                if page is not self.current or not page._want:
                    return
                self.bar.update_idletasks()
                w, h = page._want
                total = h + self.bar.winfo_reqheight() + 14
            total = min(total, app.winfo_screenheight() - SCREEN_MARGIN)
            y = app.winfo_y()
            fit = max(0, min(y, app.winfo_screenheight() - 60 - total))   # que no se salga por abajo
            # Linux: winfo_x/y excluyen la decoración; reenviarlas siempre corre la ventana de a poco.
            app.geometry(f"{w}x{total}" + (f"+{app.winfo_x()}+{fit}" if fit != y else ""))
        except tk.TclError:
            pass
