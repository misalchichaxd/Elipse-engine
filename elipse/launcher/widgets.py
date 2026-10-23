"""
Widgets M3 reutilizables: botón píldora, superficie con esquinas
redondeadas, helpers de etiqueta/campo, y estilo ttk coherente con
el tema oscuro.

Nota de compatibilidad: los defaults de fuente se resuelven al vuelo
(``font=None`` + lectura de ``theme.FBTN``/``theme.FS`` dentro del cuerpo)
en vez de bindearse como valor por defecto del parámetro. Si se
bindearan como default, quedarían congelados con la fuente de
fallback de antes de llamar a ``theme.init_fonts()`` y en Linux se
vería siempre "Segoe UI" en vez de Ubuntu/Noto/DejaVu.
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from . import theme
from .theme import C, mix, rrect, tween


class Pill(tk.Canvas):
    """Botón en forma de píldora con transición de color."""

    @property
    def KINDS(self):
        return {"filled": (C["primary"], C["on_primary"]), "tonal": (C["sec_c"], C["on_sec_c"]),
                "danger": (C["err"], C["on_err"]), "busy": (C["card_hi"], C["dim"]),
                "ghost": (C["card_hi"], C["muted"])}

    def __init__(self, parent, text, cmd=None, kind="tonal", width=0, height=40, padx=22, font=None, bg=None):
        font = font or theme.FBTN
        w = max(width, tkfont.Font(font=font).measure(text) + 2 * padx)
        super().__init__(parent, width=w, height=height, bg=bg or parent.cget("bg"), highlightthickness=0, cursor="hand2")
        self.cmd, self.kind, self.enabled = cmd, kind, True
        self.fill, self.fg = self.KINDS[kind]
        self.shape = rrect(self, 1, 1, w - 1, height - 1, (height - 2) / 2, fill=self.fill, outline="")
        self.label = self.create_text(w / 2, height / 2, text=text, fill=self.fg, font=font)
        self.bind("<Button-1>", lambda e: self.enabled and self.cmd and self.cmd())
        self.bind("<Enter>", lambda e: self.enabled and self.itemconfig(self.shape, fill=mix(self.fill, "#ffffff", .12)))
        self.bind("<Leave>", lambda e: self.itemconfig(self.shape, fill=self.fill))

    def _apply(self, cols):
        self.fill, self.fg = cols
        self.itemconfig(self.shape, fill=self.fill)
        self.itemconfig(self.label, fill=self.fg)

    def set(self, text=None, kind=None, enabled=True):
        if text is not None:
            self.itemconfig(self.label, text=text)
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        if kind and kind != self.kind:
            self.kind = kind
            tween(self, (self.fill, self.fg), self.KINDS[kind], self._apply)


class Surface(tk.Frame):
    """Contenedor de esquinas redondeadas; el contenido va en .inner.
    Sin `height`, se ajusta solo al contenido (así no se corta con otras fuentes o escalas de pantalla)."""

    def __init__(self, parent, height=None, fill=None, r=26, pad=20):
        pbg = parent.cget("bg")
        super().__init__(parent, bg=pbg)
        self.fill, self.r, self.pad, self.min_h = fill or C["card"], r, pad, height or 0
        self.cv = tk.Canvas(self, bg=pbg, highlightthickness=0, height=max(self.min_h, 2 * pad))
        self.cv.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=self.fill)
        self.win = self.cv.create_window(pad, pad, window=self.inner, anchor="nw")
        self.cv.bind("<Configure>", self._cfg)
        self.after_idle(self.fit)

    def fit(self):
        try:
            self.update_idletasks()
            self.cv.configure(height=max(self.min_h, self.inner.winfo_reqheight() + 2 * self.pad))
        except tk.TclError:
            pass

    def _cfg(self, e):
        self.cv.delete("bg")
        rrect(self.cv, 1, 1, e.width - 1, e.height - 1, self.r, fill=self.fill, outline="", tags="bg")
        self.cv.tag_lower("bg")
        self.cv.itemconfig(self.win, width=e.width - 2 * self.pad, height=e.height - 2 * self.pad)


def lbl(parent, text, fg=None, font=None, wrap=0):
    font = font or theme.FS
    return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=fg or C["muted"], font=font,
                    anchor="w", justify="left", wraplength=wrap)


def field(parent, var, show=None):
    return tk.Entry(parent, textvariable=var, show=show or "", bg=C["card_hi"], fg=C["text"], insertbackground=C["text"],
                    relief="flat", font=theme.FB, highlightthickness=2, highlightbackground=C["outline"], highlightcolor=C["primary"])


def style_ttk(root):
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure("TCombobox", fieldbackground=C["card_hi"], background=C["card_hi"], foreground=C["text"],
                arrowcolor=C["text"], bordercolor=C["outline"], lightcolor=C["outline"], darkcolor=C["outline"], padding=7)
    s.map("TCombobox", fieldbackground=[("readonly", C["card_hi"])], foreground=[("readonly", C["text"])],
          selectbackground=[("readonly", C["card_hi"])], selectforeground=[("readonly", C["text"])],
          bordercolor=[("focus", C["primary"])])
    s.configure("Treeview", background=C["card"], fieldbackground=C["card"], foreground=C["text"],
                rowheight=30, borderwidth=0, font=theme.FB)
    s.configure("Treeview.Heading", background=C["card_hi"], foreground=C["muted"], relief="flat", font=theme.FBB)
    s.map("Treeview", background=[("selected", C["primary_c"])], foreground=[("selected", C["on_primary_c"])])
    s.map("Treeview.Heading", background=[("active", C["card_hi"])])
    for k, v in (("background", C["card_hi"]), ("foreground", C["text"]),
                 ("selectBackground", C["primary_c"]), ("selectForeground", C["on_primary_c"]), ("font", theme.FB)):
        root.option_add(f"*TCombobox*Listbox.{k}", v)


def safe_grab(win):
    """grab_set seguro: en Linux (X11/Wayland) falla con 'window not viewable'
    si la ventana aun no es visible. Espera a que se mapee y, si aun asi falla,
    sigue sin modal en vez de romper el panel."""
    import tkinter as tk
    try:
        win.wait_visibility()
    except tk.TclError:
        pass
    try:
        win.grab_set()
    except tk.TclError:
        win.after(50, lambda: _retry_grab(win))


def _retry_grab(win):
    import tkinter as tk
    try:
        if win.winfo_exists():
            win.grab_set()
    except tk.TclError:
        pass
