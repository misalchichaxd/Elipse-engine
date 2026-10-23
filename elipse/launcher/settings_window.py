"""
Ventana de Ajustes: pestaña "Modelo" (un rol por fila: Razonamiento general,
Código, Investigación — cada uno abre su propio ProviderDialog) y pestaña
"Conexión y llaves" (URL de red y API keys).
"""

import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .env_store import env_dict, load_prefs, save_prefs
from .process_utils import lan_ip, run_keys
from .paths import PORT
from .provider_dialog import ROLES, ProviderDialog, role_summary
from .nav import Page
from .skills_dialog import SkillsDialog, skills_summary
from .theme import C, FB, FBB, FDS, FMONO, FT
from .uiqueue import ui
from .voice_dialog import VoiceDialog, voice_summary
from .widgets import Pill, Surface, lbl


class Settings(Page):
    WIDTH = 780

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Ajustes de ELIPSE")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        top = tk.Frame(self, bg=C["bg"])
        top.pack(fill="x", padx=28, pady=(22, 10))
        tk.Label(top, text="Ajustes", font=FDS, fg=C["primary"], bg=C["bg"]).pack(side="left")
        tabs = tk.Frame(top, bg=C["bg"])
        tabs.pack(side="right")
        self.tab_btns = {}
        for k, t in (("model", "Modelo"), ("keys", "Conexión y llaves")):
            b = Pill(tabs, t, lambda k=k: self.show(k), height=36)
            b.pack(side="left", padx=(6, 0))
            self.tab_btns[k] = b
        self.body = tk.Frame(self, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)
        self.tabs = {"model": self._build_model(), "keys": self._build_keys()}
        self.show("model")
        self._load_keys()
        self.after_idle(self._autosize)
        self.after(150, self._autosize)

    def _autosize(self):
        """Alto según el contenido (y nunca más alto que la pantalla)."""
        try:
            self.update_idletasks()
            h = min(self.winfo_reqheight(), self.winfo_screenheight() - 100)
            self.geometry(f"{self.WIDTH}x{h}")
        except tk.TclError:
            pass

    def show(self, k):
        self.tabs[k].tkraise()
        for name, b in self.tab_btns.items():
            b.set(kind="filled" if name == k else "tonal")

    def _copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self.app.toast("Copiado al portapapeles")

    # ---- pestaña Modelo: una fila por rol, cada una abre su ProviderDialog ----
    def _build_model(self):
        f = tk.Frame(self.body, bg=C["bg"])
        f.grid(row=0, column=0, sticky="nsew")
        tk.Label(f, text="¿Qué usa ELIPSE para pensar?", font=FT, fg=C["text"], bg=C["bg"]).pack(anchor="w", pady=(4, 4))
        lbl(f, "Un proveedor por default, y opcionalmente uno distinto para código, investigación o visión (imágenes).",
            wrap=700).pack(anchor="w", pady=(0, 14))

        self.role_rows_box = tk.Frame(f, bg=C["bg"])
        self.role_rows_box.pack(fill="x")
        self._render_role_rows()
        return f

    def _render_role_rows(self):
        for w in self.role_rows_box.winfo_children():
            w.destroy()
        for prefix, (label, _desc) in ROLES.items():
            self._build_role_row(self.role_rows_box, prefix, label)
        self._build_voice_row(self.role_rows_box)
        self._build_skills_row(self.role_rows_box)
    def _build_role_row(self, parent, prefix, label):
        card = Surface(parent, pad=16)
        card.pack(fill="x", pady=(0, 10))
        row = tk.Frame(card.inner, bg=C["card"])
        row.pack(fill="x")
        left = tk.Frame(row, bg=C["card"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=label, font=FBB, fg=C["text"], bg=C["card"]).pack(anchor="w")
        lbl(left, role_summary(prefix), wrap=520).pack(anchor="w", pady=(2, 0))
        has_override = bool(prefix) and self._has_override(prefix)
        Pill(
            row, "Configurar" if has_override or not prefix else "+ Agregar",
            lambda p=prefix, ov=has_override: self._open_role(p, ov), kind="tonal" if prefix else "filled", height=38,
        ).pack(side="right")

    def _build_voice_row(self, parent):
        card = Surface(parent, pad=16)
        card.pack(fill="x", pady=(0, 10))
        row = tk.Frame(card.inner, bg=C["card"])
        row.pack(fill="x")
        left = tk.Frame(row, bg=C["card"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="Voz", font=FBB, fg=C["text"], bg=C["card"]).pack(anchor="w")
        lbl(left, voice_summary(), wrap=520).pack(anchor="w", pady=(2, 0))
        Pill(row, "Configurar", lambda: VoiceDialog(self.app, on_saved=self._render_role_rows), kind="tonal", height=38).pack(side="right")

    def _build_skills_row(self, parent):
        card = Surface(parent, pad=16)
        card.pack(fill="x", pady=(0, 10))
        row = tk.Frame(card.inner, bg=C["card"])
        row.pack(fill="x")
        left = tk.Frame(row, bg=C["card"])
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="Skills", font=FBB, fg=C["text"], bg=C["card"]).pack(anchor="w")
        lbl(left, skills_summary(), wrap=520).pack(anchor="w", pady=(2, 0))
        Pill(row, "Administrar", lambda: self.app._single("skills", lambda: SkillsDialog(self.app, on_changed=self._refresh_rows)),
             kind="tonal", height=38).pack(side="right")

    def _refresh_rows(self):
        if self.winfo_exists():   # el diálogo de skills puede seguir abierto con Ajustes ya cerrado
            self._render_role_rows()

    def _has_override(self, prefix: str) -> bool:
        if prefix == "VISION_":  # la visión no hereda: siempre se "configura" (por default ya está activa)
            return True
        return bool(env_dict().get(f"{prefix}PROVIDER", "").strip())

    def _open_role(self, prefix, has_override=True):
        ProviderDialog(self.app, prefix, on_saved=self._render_role_rows, start_unchecked=not has_override)
    # ---- pestaña Conexión y llaves ----
    def _build_keys(self):
        f = tk.Frame(self.body, bg=C["bg"])
        f.grid(row=0, column=0, sticky="nsew")
        tk.Label(f, text="Conexión", font=FT, fg=C["text"], bg=C["bg"]).pack(anchor="w", pady=(4, 8))
        s = Surface(f)
        s.pack(fill="x")
        i = s.inner
        lbl(i, "URL de la API para clientes (web, teléfono)").pack(anchor="w")
        r = tk.Frame(i, bg=C["card"])
        r.pack(fill="x", pady=(4, 8))
        ip, lan = lan_ip(), bool(load_prefs().get("lan"))
        opts = [f"http://127.0.0.1:{PORT}"] + ([f"http://{ip}:{PORT}"] if ip else [])
        self.url = tk.StringVar(value=opts[-1] if lan else opts[0])
        ttk.Combobox(r, textvariable=self.url, values=opts).pack(side="left", fill="x", expand=True)
        Pill(r, "Copiar", lambda: self._copy(self.url.get()), height=36).pack(side="left", padx=(8, 0))
        self.lan = tk.BooleanVar(value=lan)
        tk.Checkbutton(i, text="Permitir otros dispositivos de mi red", variable=self.lan, command=self._lan_changed,
                       bg=C["card"], fg=C["text"], selectcolor=C["card_hi"], activebackground=C["card"],
                       activeforeground=C["text"], font=FB).pack(anchor="w")
        lbl(i, "Apagado, ELIPSE solo escucha en esta PC. Encendido, cualquiera de tu wifi con una llave válida puede usarlo.",
            wrap=600).pack(anchor="w")

        tk.Label(f, text="Llaves de ELIPSE", font=FT, fg=C["text"], bg=C["bg"]).pack(anchor="w", pady=(16, 6))
        bar = tk.Frame(f, bg=C["bg"])
        bar.pack(anchor="w", pady=(0, 8))
        for t, cmd, k in (("Actualizar", self._load_keys, "tonal"), ("Nueva llave", self._new_key, "filled"),
                          ("Revocar", self._revoke, "danger")):
            Pill(bar, t, cmd, kind=k, height=36).pack(side="left", padx=(0, 8))
        cols = (("id", "ID", 44), ("nombre", "Nombre", 130), ("estado", "Estado", 90), ("creada", "Creada", 160), ("uso", "Último uso", 160))
        self.tree = ttk.Treeview(f, columns=[c[0] for c in cols], show="headings", height=6)
        for c, t, w in cols:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x")
        self.kstatus = lbl(f, "Una llave ya creada no se puede volver a ver, solo se guarda su hash. Para rotarla, crea otra y revoca la vieja.", wrap=640)
        self.kstatus.pack(anchor="w", pady=(8, 0))
        return f

    def _lan_changed(self):
        p = load_prefs()
        p["lan"] = self.lan.get()
        save_prefs(p)
        self.app.toast("Reinicia ELIPSE Core para aplicar el cambio de red" if self.app.svc["elipse"].state == "on" else "Preferencia guardada")

    def _load_keys(self):
        def run():
            try:
                ui(self._populate, run_keys(["list"]).get("keys", []), None)
            except Exception as e:
                ui(self._populate, [], str(e))
        threading.Thread(target=run, daemon=True).start()

    def _populate(self, keys, err):
        if not self.winfo_exists():
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for k in keys:
            self.tree.insert("", "end", values=(k.get("id"), k.get("name"), "revocada" if k.get("revoked") else "activa",
                                                k.get("created_at") or "", k.get("last_used_at") or "nunca"))
        if err:
            self.kstatus.config(text=f"No se pudieron leer las llaves: {err}", fg=C["err"])
        else:
            self.kstatus.config(text="Una llave ya creada no se puede volver a ver, solo se guarda su hash. Para rotarla, crea otra y revoca la vieja.",
                                fg=C["muted"])

    def _new_key(self):
        name = simpledialog.askstring("Nueva llave", "Nombre del dispositivo o cliente (por ejemplo, telefono):", parent=self)
        if not name:
            return

        def run():
            try:
                ui(self._show_key, run_keys(["create", name]).get("api_key", ""))
            except Exception as e:
                ui(messagebox.showerror, "No se pudo crear la llave", str(e))
            ui(self._load_keys)
        threading.Thread(target=run, daemon=True).start()

    def _show_key(self, raw):
        w = tk.Toplevel(self)
        w.title("Nueva llave")
        w.configure(bg=C["bg"])
        w.resizable(False, False)
        tk.Label(w, text="Guárdala ahora, no se vuelve a mostrar", font=FT, fg=C["primary"], bg=C["bg"]).pack(padx=24, pady=(20, 10))
        e = tk.Entry(w, width=54, font=FMONO, bg=C["card_hi"], fg=C["text"], relief="flat", readonlybackground=C["card_hi"])
        e.insert(0, raw)
        e.config(state="readonly")
        e.pack(padx=24, ipady=8)
        Pill(w, "Copiar", lambda: self._copy(raw), kind="filled").pack(pady=18)

    def _revoke(self):
        sel = self.tree.selection()
        if not sel:
            self.app.toast("Selecciona una llave de la lista primero")
            return
        kid, name = self.tree.item(sel[0], "values")[:2]
        if not messagebox.askyesno("Revocar llave", f"¿Revocar la llave «{name}» (id {kid})? No se puede deshacer.", parent=self):
            return

        def run():
            try:
                if not run_keys(["revoke", str(kid)]).get("ok"):
                    ui(self.app.toast, "No existe una llave con ese id")
            except Exception as e:
                ui(messagebox.showerror, "No se pudo revocar", str(e))
            ui(self._load_keys)
        threading.Thread(target=run, daemon=True).start()