"""
Diálogo "Perfiles": crear y gestionar los perfiles de ELIPSE (p. ej. 'rbe') y sus llaves.

Toda la lógica vive en app/core/profiles.py; este panel solo la invoca con
`python -m app.manage_profiles --json ...` (y `manage_keys` para las llaves), igual que Skills.
Así el panel hace exactamente lo que hará el servidor y los cambios aplican sin reiniciar.

Seguridad (R12): el callback y su secreto solo se fijan aquí o con una llave admin; el secreto
generado se muestra UNA vez. Una llave de perfil solo puede lo que su techo (caps) permita.
"""

import copy
import json
import os
import sqlite3
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import theme
from .nav import Page
from .paths import ROOT
from .process_utils import run_keys, run_profiles
from .theme import C
from .uiqueue import ui
from .widgets import Pill, Surface, field, lbl, safe_grab

# Mientras la Fase 3 no abra más, estas son las únicas tools propias que un perfil puede usar.
SAFE_TOOLS = (("get_current_datetime", "Fecha y hora"), ("calculate", "Calculadora"))
STATE_FG = {"activo": C["good"], "desactivado": C["dim"], "sin sincronizar": C["warm"]}


def profiles_summary() -> str:
    """Texto corto para la fila de Ajustes. Lee la base directo (solo lectura), sin lanzar Python."""
    db = ROOT / "elipse.db"
    if not db.exists():
        return "Sin perfiles · crea uno para conectar otro sistema (p. ej. RBE)"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            n, off = con.execute("SELECT COUNT(*), COALESCE(SUM(enabled = 0), 0) FROM profiles").fetchone()
        finally:
            con.close()
    except Exception:
        return "Sin perfiles · crea uno para conectar otro sistema (p. ej. RBE)"
    if not n:
        return "Sin perfiles · crea uno para conectar otro sistema (p. ej. RBE)"
    return f"{n} perfil(es)" + (f" · {off} desactivado(s)" if off else "")


def _text(parent, height, wrap="word"):
    t = tk.Text(parent, height=height, wrap=wrap, bg=C["card_hi"], fg=C["text"], insertbackground=C["text"],
                relief="flat", font=theme.FB, highlightthickness=2, highlightbackground=C["outline"],
                highlightcolor=C["primary"], padx=8, pady=6)
    return t


def _check(parent, text, var):
    return tk.Checkbutton(parent, text=text, variable=var, bg=parent.cget("bg"), fg=C["text"],
                          selectcolor=C["card_hi"], activebackground=parent.cget("bg"),
                          activeforeground=C["text"], font=theme.FB, anchor="w")


def _bg(fn, ok, fail=None):
    """Corre fn() en un hilo y entrega el resultado (o el error) a la UI."""
    def run():
        try:
            res = fn()
        except Exception as e:           # noqa: BLE001 — se muestra tal cual al usuario
            ui(fail or (lambda err: messagebox.showerror("ELIPSE", str(err))), e)
            return
        ui(ok, res)
    threading.Thread(target=run, daemon=True).start()


def _secret_window(parent, title, headline, value):
    """Muestra algo que no se vuelve a ver (llave o secreto) con botón de copiar."""
    w = tk.Toplevel(parent)
    w.title(title)
    w.configure(bg=C["bg"])
    w.resizable(False, False)
    tk.Label(w, text=headline, font=theme.FT, fg=C["primary"], bg=C["bg"]).pack(padx=24, pady=(20, 10))
    e = tk.Entry(w, width=64, font=theme.FMONO, bg=C["card_hi"], fg=C["text"], relief="flat",
                 readonlybackground=C["card_hi"])
    e.insert(0, value)
    e.config(state="readonly")
    e.pack(padx=24, ipady=8)

    def copy():
        w.clipboard_clear()
        w.clipboard_append(value)
        w.update()
    Pill(w, "Copiar", copy, kind="filled").pack(pady=18)


class ProfilesDialog(Page):
    WIDTH, HEIGHT = 780, 740

    def __init__(self, app, on_changed=None):
        super().__init__(app)
        self.app, self.on_changed = app, on_changed
        self.items: dict = {}
        self.keys: list = []
        self.title("Perfiles de ELIPSE")
        self.configure(bg=C["bg"])
        h = min(self.HEIGHT, max(520, self.winfo_screenheight() - 170))
        self.geometry(f"{self.WIDTH}x{h}")
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ construcción
    def _build(self):
        head = tk.Frame(self, bg=C["bg"])
        head.pack(fill="x", padx=26, pady=(20, 4))
        tk.Label(head, text="Perfiles", font=theme.FDS, fg=C["primary"], bg=C["bg"]).pack(side="left")
        lbl(self, "Un perfil es «ELIPSE trabajando para otro sistema» (p. ej. RBE): con su propio rol, sus herramientas, "
                  "su memoria y sus llaves. Tu ELIPSE de siempre no cambia, y ningún perfil ve lo tuyo.",
            wrap=self.WIDTH - 60).pack(anchor="w", padx=26, pady=(0, 10))

        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=26)
        Pill(bar, "Nuevo perfil", self._new, kind="filled", height=38).pack(side="left")
        Pill(bar, "Recargar", self.refresh, kind="ghost", height=38).pack(side="right")

        box = tk.Frame(self, bg=C["bg"])
        box.pack(fill="x", padx=26, pady=(12, 0))
        cols = (("nombre", "Perfil", 110), ("estado", "Estado", 110), ("tools", "Tools", 60),
                ("llaves", "Llaves", 60), ("callback", "Callback", 90), ("brief", "Descripción", 300))
        self.tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=5, selectmode="browse")
        for c, t, w in cols:
            self.tree.heading(c, text=t, anchor="w")
            self.tree.column(c, width=w, anchor="w", stretch=(c == "brief"))
        self.tree.pack(fill="x")
        for st, fg in STATE_FG.items():
            self.tree.tag_configure(st, foreground=fg)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        act = tk.Frame(self, bg=C["bg"])
        act.pack(fill="x", padx=26, pady=(10, 0))
        self.btns = {}
        for key, text, cmd, kind in (("edit", "Editar", self._edit, "tonal"),
                                     ("toggle", "Desactivar", self._toggle, "tonal"),
                                     ("callback", "Callback…", self._callback, "tonal"),
                                     ("delete", "Eliminar", self._delete, "danger")):
            self.btns[key] = Pill(act, text, cmd, kind=kind, height=36)
            self.btns[key].pack(side="left", padx=(0, 8))
        self.status = lbl(self, "", wrap=self.WIDTH - 60)
        self.status.pack(anchor="w", padx=26, pady=(6, 0))

        tk.Label(self, text="Llaves de este perfil", font=theme.FBB, fg=C["text"], bg=C["bg"]).pack(
            anchor="w", padx=26, pady=(12, 4))
        kb = tk.Frame(self, bg=C["bg"])
        kb.pack(fill="x", padx=26)
        self.new_key_btn = Pill(kb, "Llave nueva…", self._new_key, kind="filled", height=36)
        self.new_key_btn.pack(side="left")
        self.revoke_btn = Pill(kb, "Revocar", self._revoke_key, kind="danger", height=36)
        self.revoke_btn.pack(side="left", padx=(8, 0))
        kbox = tk.Frame(self, bg=C["bg"])
        kbox.pack(fill="x", padx=26, pady=(8, 0))
        kcols = (("id", "ID", 44), ("nombre", "Nombre", 160), ("estado", "Estado", 90), ("uso", "Último uso", 200))
        self.ktree = ttk.Treeview(kbox, columns=[c[0] for c in kcols], show="headings", height=4, selectmode="browse")
        for c, t, w in kcols:
            self.ktree.heading(c, text=t, anchor="w")
            self.ktree.column(c, width=w, anchor="w")
        self.ktree.pack(fill="x")
        lbl(self, "Una llave ya creada no se puede volver a ver. Una llave de perfil nace con un techo de permisos "
                  "(caps): solo puede lo que ahí se marque.", wrap=self.WIDTH - 60).pack(anchor="w", padx=26, pady=(8, 14))
        self._on_select()

    # ------------------------------------------------------------------ datos
    def refresh(self):
        def load():
            return run_profiles(["list"]), run_keys(["list"]).get("keys", [])
        _bg(load, lambda r: self._populate(*r), lambda e: self._say(f"No se pudo leer: {e}", err=True))

    def _populate(self, prof_res, keys):
        if not self.winfo_exists():
            return
        if "error" in prof_res:
            self._say(prof_res["error"], err=True)
            return
        self.items = {p["name"]: p for p in prof_res.get("profiles", [])}
        self.keys = keys
        keep = self.selected()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for p in self.items.values():
            state = "sin sincronizar" if not p["synced"] else ("activo" if p["enabled"] else "desactivado")
            n_tools = len(p["builtin_tools"]) + len(p["host_tools"])
            cb = "sí" if p["callback_url"] and p["has_secret"] else "—"
            self.tree.insert("", "end", iid=p["name"], tags=(state,),
                             values=(p["name"], state, n_tools, p["keys"], cb, p["brief"] or ""))
        if keep in self.items:
            self.tree.selection_set(keep)
        self._on_select()
        self._say("")
        if self.on_changed:
            self.on_changed()

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _say(self, text, err=False):
        if self.winfo_exists():
            self.status.config(text=text, fg=C["err"] if err else C["muted"])

    def _on_select(self):
        name = self.selected()
        p = self.items.get(name) if name else None
        for k in ("edit", "toggle", "callback", "delete"):
            self.btns[k].set(enabled=bool(p))
        self.btns["toggle"].set(text="Activar" if p and not p["enabled"] else "Desactivar", enabled=bool(p and p["synced"]))
        self.btns["edit"].set(enabled=bool(p and p["synced"]))
        self.btns["callback"].set(enabled=bool(p and p["synced"]))
        self.new_key_btn.set(enabled=bool(p))
        for iid in self.ktree.get_children():
            self.ktree.delete(iid)
        if p:
            for k in self.keys:
                if k.get("profile") == name:
                    self.ktree.insert("", "end", iid=str(k["id"]), values=(
                        k["id"], k["name"], "revocada" if k["revoked"] else "activa", k.get("last_used_at") or "nunca"))
            if not p["synced"]:
                self._say("Esta llave existe pero el sistema aún no sincronizó su perfil. Puedes crearlo tú con «Nuevo perfil» "
                          "usando el mismo nombre.")
            elif p["callback_url"] and p["has_secret"]:
                self._say(f"Callback: {p['callback_url']}")
            else:
                self._say("Sin callback: las herramientas del anfitrión no podrán ejecutarse todavía.")

    # ------------------------------------------------------------------ acciones
    def _after(self, msg=None):
        def done(res):
            if isinstance(res, dict) and res.get("error"):
                messagebox.showerror("ELIPSE", res["error"])
            elif msg:
                self.app.toast(msg)
            self.refresh()
        return done

    def _new(self):
        ProfileEditor(self.app, None, {}, on_saved=self.refresh)

    def _edit(self):
        p = self.items.get(self.selected())
        if p:
            ProfileEditor(self.app, p["name"], p["spec"], on_saved=self.refresh)

    def _toggle(self):
        p = self.items.get(self.selected())
        if p:
            cmd = "disable" if p["enabled"] else "enable"
            _bg(lambda: run_profiles([cmd, p["name"]]), self._after("Perfil actualizado"))

    def _delete(self):
        p = self.items.get(self.selected())
        if not p:
            return
        if not messagebox.askyesno("Eliminar perfil", f"¿Eliminar el perfil «{p['name']}» y revocar sus {p['keys']} llave(s)?\n"
                                   "Su historial no se borra. No se puede deshacer.", parent=self):
            return
        _bg(lambda: run_profiles(["delete", p["name"]]), self._after("Perfil eliminado"))

    def _callback(self):
        p = self.items.get(self.selected())
        if p:
            CallbackWindow(self, p, on_done=self.refresh)

    def _new_key(self):
        p = self.items.get(self.selected())
        if p:
            KeyWindow(self, p, on_done=self.refresh)

    def _revoke_key(self):
        sel = self.ktree.selection()
        if not sel:
            self.app.toast("Selecciona una llave de la lista primero")
            return
        if not messagebox.askyesno("Revocar llave", f"¿Revocar la llave {sel[0]}? No se puede deshacer.", parent=self):
            return
        _bg(lambda: run_keys(["revoke", sel[0]]), self._after("Llave revocada"))


class ProfileEditor(Page):
    """Crear o editar un perfil. Al editar conserva lo que este formulario no toca (tools del anfitrión, voz, límites…)."""
    WIDTH, HEIGHT = 780, 700

    def __init__(self, app, name, spec, on_saved=None):
        super().__init__(app)
        self.app, self.name, self.base, self.on_saved = app, name, copy.deepcopy(spec or {}), on_saved
        self.title("Editar perfil" if name else "Nuevo perfil")
        self.configure(bg=C["bg"])
        h = min(self.HEIGHT, max(520, self.winfo_screenheight() - 170))
        self.geometry(f"{self.WIDTH}x{h}")
        self._build()

    def _build(self):
        tk.Label(self, text="Editar perfil" if self.name else "Nuevo perfil", font=theme.FDS, fg=C["primary"],
                 bg=C["bg"]).pack(anchor="w", padx=26, pady=(20, 10))
        s = Surface(self, pad=18)
        s.pack(fill="x", padx=26)
        i = s.inner
        lbl(i, "Nombre (minúsculas, números, _ o -; ej. rbe)").pack(anchor="w")
        self.v_name = tk.StringVar(value=self.name or "")
        e = field(i, self.v_name)
        e.pack(fill="x", pady=(4, 10), ipady=5)
        if self.name:
            e.config(state="readonly", readonlybackground=C["card_hi"])
        lbl(i, "Descripción corta (la usa el ELIPSE normal para saber qué hace este perfil; máx. 300)").pack(anchor="w")
        self.v_brief = tk.StringVar(value=self.base.get("brief", ""))
        field(i, self.v_brief).pack(fill="x", pady=(4, 10), ipady=5)
        lbl(i, "Rol: quién es ELIPSE en este sistema y qué puede hacer").pack(anchor="w")
        self.t_role = _text(i, 5)
        self.t_role.insert("1.0", self.base.get("role", ""))
        self.t_role.pack(fill="x", pady=(4, 10))
        lbl(i, "Reglas (una por línea)").pack(anchor="w")
        self.t_rules = _text(i, 4)
        self.t_rules.insert("1.0", "\n".join(self.base.get("rules", [])))
        self.t_rules.pack(fill="x", pady=(4, 10))
        lbl(i, "Herramientas propias de ELIPSE que puede usar (las del sistema anfitrión las declara su SDK)").pack(anchor="w")
        have = set((self.base.get("tools") or {}).get("builtin", []))
        self.v_tools = {}
        for tool, label in SAFE_TOOLS:
            self.v_tools[tool] = tk.BooleanVar(value=tool in have if self.name else True)
            _check(i, label, self.v_tools[tool]).pack(anchor="w")
        lbl(i, "Memoria, archivos, web y código se abrirán por perfil en una fase siguiente; por ahora un perfil no los usa.",
            wrap=self.WIDTH - 110).pack(anchor="w", pady=(6, 0))
        s.fit()

        row = tk.Frame(self, bg=C["bg"])
        row.pack(fill="x", padx=26, pady=14)
        Pill(row, "Guardar", self._save, kind="filled").pack(side="left")
        Pill(row, "Cancelar", self.destroy, kind="ghost").pack(side="left", padx=(8, 0))
        self.err = lbl(row, "", fg=C["err"], wrap=420)
        self.err.pack(side="left", padx=14)

    def _save(self):
        name = (self.name or self.v_name.get()).strip()
        spec = copy.deepcopy(self.base)
        spec["brief"] = self.v_brief.get().strip()
        spec["role"] = self.t_role.get("1.0", "end").strip()
        rules = [r.strip() for r in self.t_rules.get("1.0", "end").splitlines() if r.strip()]
        if rules:
            spec["rules"] = rules
        else:
            spec.pop("rules", None)
        tools = dict(spec.get("tools") or {})
        tools["builtin"] = [t for t, v in self.v_tools.items() if v.get()]
        spec["tools"] = tools
        if not name:
            self.err.config(text="Falta el nombre.")
            return
        if not spec["role"]:
            self.err.config(text="Falta el rol: sin él el perfil no sabe qué hacer.")
            return
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)
        verb = "update" if self.name else "create"

        def done(res):
            try:
                os.unlink(path)
            except OSError:
                pass
            if res.get("error"):
                if self.winfo_exists():
                    self.err.config(text=res["error"])
                return
            self.app.toast("Perfil guardado" + (f" · se descartó: {', '.join(res['rejected'])}" if res.get("rejected") else ""))
            if self.on_saved:
                self.on_saved()
            if self.winfo_exists():
                self.destroy()
        _bg(lambda: run_profiles([verb, name, "--spec", path]), done)


class CallbackWindow(tk.Toplevel):
    """URL del anfitrión a la que el engine llamará (firmado con HMAC) para ejecutar sus herramientas."""

    def __init__(self, parent, p, on_done):
        super().__init__(parent)
        self.p, self.on_done, self.parent = p, on_done, parent
        self.title(f"Callback de {p['name']}")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        tk.Label(self, text=f"Callback de «{p['name']}»", font=theme.FT, fg=C["primary"], bg=C["bg"]).pack(padx=24, pady=(18, 4))
        lbl(self, "URL base de tu sistema donde ELIPSE llamará a sus herramientas (ej. http://127.0.0.1:8001/elipse/tools). "
                  "Solo tú la fijas desde aquí; la llave del sistema no puede cambiarla.", wrap=460).pack(padx=24)
        self.url = tk.StringVar(value=p.get("callback_url") or "")
        field(self, self.url).pack(fill="x", padx=24, pady=(10, 6), ipady=5)
        self.gen = tk.BooleanVar(value=not p["has_secret"])
        _check(self, "Generar un secreto nuevo (se muestra una sola vez; ponlo en el sistema anfitrión)", self.gen).pack(anchor="w", padx=24)
        self.err = lbl(self, "", fg=C["err"], wrap=460)
        self.err.pack(anchor="w", padx=24)
        row = tk.Frame(self, bg=C["bg"])
        row.pack(pady=14)
        Pill(row, "Guardar", self._save, kind="filled").pack(side="left")
        if p["callback_url"]:
            Pill(row, "Quitar", self._clear, kind="danger").pack(side="left", padx=(8, 0))
        Pill(row, "Cancelar", self.destroy, kind="ghost").pack(side="left", padx=(8, 0))
        safe_grab(self)

    def _save(self):
        args = ["set-callback", self.p["name"], "--url", self.url.get().strip()] + (["--generate"] if self.gen.get() else [])
        _bg(lambda: run_profiles(args), self._done)

    def _clear(self):
        _bg(lambda: run_profiles(["clear-callback", self.p["name"]]), self._done)

    def _done(self, res):
        if res.get("error"):
            self.err.config(text=res["error"])
            return
        self.on_done()
        self.destroy()
        if res.get("secret"):
            _secret_window(self.parent, "Secreto del callback", "Guárdalo ahora, no se vuelve a mostrar", res["secret"])


class KeyWindow(tk.Toplevel):
    """Llave nueva para un perfil, con su techo de permisos (caps)."""

    def __init__(self, parent, p, on_done):
        super().__init__(parent)
        self.p, self.on_done, self.parent = p, on_done, parent
        self.title(f"Llave para {p['name']}")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        tk.Label(self, text=f"Llave nueva para «{p['name']}»", font=theme.FT, fg=C["primary"], bg=C["bg"]).pack(padx=24, pady=(18, 8))
        lbl(self, "Nombre de la llave (ej. rbe-django)").pack(anchor="w", padx=24)
        self.name = tk.StringVar(value=f"{p['name']}-django")
        field(self, self.name).pack(fill="x", padx=24, pady=(4, 10), ipady=5)
        lbl(self, "Techo de permisos: lo máximo que esta llave puede darle al perfil", fg=C["text"], font=theme.FBB).pack(anchor="w", padx=24)
        self.v_tools = {}
        for tool, label in SAFE_TOOLS:
            self.v_tools[tool] = tk.BooleanVar(value=tool in p["builtin_tools"] or not p["builtin_tools"])
            _check(self, f"Puede usar: {label}", self.v_tools[tool]).pack(anchor="w", padx=24)
        self.host = tk.BooleanVar(value=True)
        _check(self, "Puede registrar herramientas de su propio sistema (anfitrión)", self.host).pack(anchor="w", padx=24)
        self.voice = tk.BooleanVar(value=False)
        _check(self, "Puede usar voz (siempre local)", self.voice).pack(anchor="w", padx=24)
        lbl(self, "Visión, adjuntos, pipelines, skills y MCP quedan apagados.", wrap=440).pack(anchor="w", padx=24, pady=(6, 0))
        self.err = lbl(self, "", fg=C["err"], wrap=440)
        self.err.pack(anchor="w", padx=24)
        row = tk.Frame(self, bg=C["bg"])
        row.pack(pady=14)
        Pill(row, "Crear llave", self._create, kind="filled").pack(side="left")
        Pill(row, "Cancelar", self.destroy, kind="ghost").pack(side="left", padx=(8, 0))
        safe_grab(self)

    def _create(self):
        name = self.name.get().strip()
        if not name:
            self.err.config(text="Falta el nombre.")
            return
        caps = {"tools": [t for t, v in self.v_tools.items() if v.get()], "host_tools": self.host.get(),
                "max_host_tools": 60, "pipelines": [], "vision": False, "attachments": False,
                "continuation": False, "mcp": [], "skills": [],
                "voice": {"allowed": self.voice.get(), "cloud": False}, "data_policy": "local_only"}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(caps, f)

        def run():
            try:
                return run_keys(["create", name, "--profile", self.p["name"], "--caps", path])
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        _bg(run, self._done)

    def _done(self, res):
        if res.get("error") or not res.get("api_key"):
            self.err.config(text=res.get("error") or "No se pudo crear la llave.")
            return
        self.on_done()
        self.destroy()
        _secret_window(self.parent, "Llave nueva", "Guárdala ahora, no se vuelve a mostrar", res["api_key"])