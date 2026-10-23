"""
Diálogo "Skills": instalar (URL, archivo .md/.zip o carpeta), activar/desactivar, actualizar,
eliminar y PROBAR qué skills se activarían con un mensaje.

Toda la lógica vive en app/core/skills.py; este panel solo la invoca con
`python -m app.core.skills --json ...` (igual que las llaves con manage_keys). Así el panel
prueba exactamente lo que hará el servidor, con el mismo .env, y los cambios se aplican en el
siguiente mensaje sin reiniciar ELIPSE.

Seguridad: lo que se instala queda PENDIENTE; no se usa hasta confirmarlo aquí. Una skill es
texto que se suma a las instrucciones del modelo: nunca ejecuta código.
"""

import json
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import theme
from .nav import Page
from .paths import ROOT
from .process_utils import run_skills
from .theme import C
from .uiqueue import ui
from .widgets import Pill, field, lbl

STATUS_FG = {"activa": C["good"], "pendiente": C["warm"], "desactivada": C["dim"]}
BODY_PREVIEW_CHARS = 6000


def skills_summary() -> str:
    """Texto corto para la fila de Ajustes. Lee archivos directamente (sin lanzar Python)."""
    d = ROOT / "skills"
    try:
        names = set()
        if d.is_dir():
            for p in d.iterdir():
                if p.is_dir() and any(c.name.lower() == "skill.md" for c in p.iterdir()):
                    names.add(p.name)
                elif p.suffix.lower() == ".md" and p.stem.lower() not in ("readme", "state"):
                    names.add(p.stem)
        sf = d / "state.json"
        state = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {}
    except Exception:
        return "No se pudo leer la carpeta skills/"
    if not names:
        return "Sin skills instaladas · instala una desde una URL o un archivo"
    pend = sum(1 for n in state.get("pending", []) if n in names)
    off = sum(1 for n in state.get("disabled", []) if n in names)
    parts = [f"{len(names) - pend - off} activas"]
    if pend:
        parts.append(f"{pend} pendientes de aprobar")
    if off:
        parts.append(f"{off} desactivadas")
    return " · ".join(parts)


class SkillsDialog(Page):
    WIDTH, HEIGHT = 780, 760

    def __init__(self, app, on_changed=None):
        super().__init__(app)
        self.app, self.on_changed = app, on_changed
        self.skills: dict = {}
        self.busy = False
        self._show_token = 0
        self.title("Skills de ELIPSE")
        self.configure(bg=C["bg"])
        h = min(self.HEIGHT, max(520, self.winfo_screenheight() - 170))
        self.geometry(f"{self.WIDTH}x{h}")
        self.minsize(760, 560)
        self._style_scrollbars()
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ construcción

    def _style_scrollbars(self):
        s = ttk.Style(self)
        s.configure("Skills.Vertical.TScrollbar", background=C["outline"], troughcolor=C["card"],
                    bordercolor=C["card"], lightcolor=C["outline"], darkcolor=C["outline"], arrowcolor=C["text"])
        # sin nada que desplazar, ttk la pone "disabled" y clam la pinta clara: se la fuerza al tema
        s.map("Skills.Vertical.TScrollbar", background=[("disabled", C["card_hi"]), ("active", C["dim"])],
              arrowcolor=[("disabled", C["outline"])])
        s.configure("Skills.Treeview", bordercolor=C["card"], lightcolor=C["card"], darkcolor=C["card"])

    def _build(self):
        head = tk.Frame(self, bg=C["bg"])
        head.pack(fill="x", padx=26, pady=(20, 4))
        tk.Label(head, text="Skills", font=theme.FDS, fg=C["primary"], bg=C["bg"]).pack(side="left")
        lbl(self, "Criterio reutilizable que ELIPSE aplica según lo que le pidas (cómo revisar código, cómo escribir un "
                  "informe…). Son texto, nunca ejecutan código. Lo que instales queda pendiente hasta que lo apruebes.",
            wrap=self.WIDTH - 60).pack(anchor="w", padx=26, pady=(0, 10))

        bar = tk.Frame(self, bg=C["bg"])
        bar.pack(fill="x", padx=26)
        Pill(bar, "Instalar desde URL", self._install_url, kind="filled", height=38).pack(side="left")
        Pill(bar, "Archivo .md / .zip…", self._install_file, kind="tonal", height=38).pack(side="left", padx=(8, 0))
        Pill(bar, "Carpeta…", self._install_dir, kind="tonal", height=38).pack(side="left", padx=(8, 0))
        Pill(bar, "Recargar", self.refresh, kind="ghost", height=38).pack(side="right")

        # --- lista
        box = tk.Frame(self, bg=C["bg"])
        box.pack(fill="x", padx=26, pady=(12, 0))
        cols = (("estado", "Estado", 92), ("nombre", "Nombre", 150), ("origen", "Origen", 140),
                ("tam", "Tamaño", 96), ("desc", "Descripción", 300))
        self.tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=4,
                                 selectmode="browse", style="Skills.Treeview")
        for c, tt, w in cols:
            self.tree.heading(c, text=tt, anchor="w")
            self.tree.column(c, width=w, anchor="w", stretch=(c == "desc"))
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview, style="Skills.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")          # primero: si no, la tabla se come todo el ancho
        self.tree.pack(side="left", fill="x", expand=True)
        for st, fg in STATUS_FG.items():
            self.tree.tag_configure(st, foreground=fg)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())

        act = tk.Frame(self, bg=C["bg"])
        act.pack(fill="x", padx=26, pady=(10, 0))
        self.toggle_btn = Pill(act, "Activar", self._toggle, kind="tonal", height=36)
        self.toggle_btn.pack(side="left")
        self.update_btn = Pill(act, "Actualizar", self._update, kind="tonal", height=36)
        self.update_btn.pack(side="left", padx=(8, 0))
        self.delete_btn = Pill(act, "Eliminar", self._delete, kind="danger", height=36)
        self.delete_btn.pack(side="left", padx=(8, 0))
        self.status = lbl(act, "", wrap=420)
        self.status.pack(side="right")
        self._set_actions(None)

        paned = tk.PanedWindow(self, orient="vertical", bg=C["bg"], bd=0, sashwidth=8, sashrelief="flat",
                               showhandle=False, opaqueresize=True)
        paned.pack(fill="both", expand=True, padx=0, pady=(6, 14))

        # --- detalle de la skill elegida
        top = tk.Frame(paned, bg=C["bg"])
        tk.Label(top, text="Contenido de la skill", font=theme.FBB, fg=C["text"], bg=C["bg"]).pack(
            anchor="w", padx=26, pady=(6, 4))
        self.detail = self._text(top, theme.FB, height=5)
        paned.add(top, minsize=120, stretch="always")

        # --- probador
        bottom = tk.Frame(paned, bg=C["bg"])
        tk.Label(bottom, text="Probar con un mensaje", font=theme.FBB, fg=C["text"], bg=C["bg"]).pack(
            anchor="w", padx=26, pady=(6, 4))
        row = tk.Frame(bottom, bg=C["bg"])
        row.pack(fill="x", padx=26)
        self.msg = tk.StringVar()
        e = field(row, self.msg)
        e.pack(side="left", fill="x", expand=True, ipady=7)
        e.bind("<Return>", lambda ev: self._test())
        Pill(row, "Probar", self._test, kind="filled", height=36).pack(side="left", padx=(8, 0))
        self.compact = tk.BooleanVar(value=False)
        tk.Checkbutton(bottom, text="Como lo vería un modelo local (modo compacto, Ollama)", variable=self.compact,
                       bg=C["bg"], fg=C["text"], selectcolor=C["card_hi"], activebackground=C["bg"],
                       activeforeground=C["text"], font=theme.FB, highlightthickness=0, bd=0).pack(
            anchor="w", padx=22, pady=(4, 6))
        self.result = self._text(bottom, theme.FMONO, height=8)
        paned.add(bottom, minsize=150, stretch="always")
        self._set_text(self.result, "Escribe un mensaje de ejemplo y pulsa «Probar»: verás qué skill se activaría, "
                                    "con qué puntaje y el texto exacto que se le agregaría al modelo.")

    def _text(self, parent, font, height, pady=0):
        frame = tk.Frame(parent, bg=C["bg"])
        frame.pack(fill="both", expand=True, padx=26, pady=pady)
        t = tk.Text(frame, height=height, wrap="word", bg=C["card_hi"], fg=C["text"], insertbackground=C["text"],
                    relief="flat", font=font, padx=12, pady=10, highlightthickness=0, state="disabled")
        sb = ttk.Scrollbar(frame, orient="vertical", command=t.yview, style="Skills.Vertical.TScrollbar")
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return t

    # ------------------------------------------------------------------ utilidades

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _say(self, text, level="muted"):
        self.status.config(text=text, fg=C.get(level, C["muted"]))

    def _bg(self, fn, done, busy_msg="", exclusive=True):
        """Corre fn en un hilo y entrega (resultado, error) a `done` en el hilo de la UI."""
        if exclusive:
            if self.busy:
                return
            self.busy = True
        if busy_msg:
            self._say(busy_msg)

        def run():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, str(e)
            ui(self._finish, done, res, err, exclusive)
        threading.Thread(target=run, daemon=True).start()

    def _finish(self, done, res, err, exclusive):
        if exclusive:
            self.busy = False
        if self.winfo_exists():
            done(res, err)

    def _selected(self):
        sel = self.tree.selection()
        return self.skills.get(sel[0]) if sel else None

    def _changed(self):
        if self.on_changed:
            try:
                self.on_changed()
            except Exception:
                pass

    def _set_actions(self, sk):
        has = sk is not None
        self.toggle_btn.set("Desactivar" if has and sk["status"] == "activa" else "Activar", enabled=has)
        self.update_btn.set(enabled=bool(has and sk["origin"] != "local"))
        self.delete_btn.set(enabled=has)

    # ------------------------------------------------------------------ lista

    def refresh(self):
        self._bg(lambda: run_skills(["list"]), self._on_list, "Leyendo skills…")

    def _on_list(self, res, err):
        if err or res.get("status") == "error":
            self._say(f"No se pudieron leer las skills: {err or res.get('detail')}", "err")
            return
        keep = (self._selected() or {}).get("name")
        self.skills = {s["name"]: s for s in res["skills"]}
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for s in res["skills"]:
            origin = "local" if s["origin"] == "local" else s["origin"].replace("https://", "")[:34]
            self.tree.insert("", "end", iid=s["name"], tags=(s["status"],),
                             values=(s["status"], s["name"], origin, f"{s['chars']} car.", s["description"]))
        if keep in self.skills:
            self.tree.selection_set(keep)
        else:
            self._set_actions(None)
            self._set_text(self.detail, "Elige una skill de la lista para ver su contenido." if self.skills else
                           "Todavía no hay skills. Instala una desde una URL (por ejemplo, un SKILL.md de GitHub) "
                           "o desde un archivo.")
        errs = res.get("errors") or []
        pend = sum(1 for s in res["skills"] if s["status"] == "pendiente")
        if errs:
            self._say(f"{len(errs)} archivo(s) con errores: {errs[0]['file'].split('/')[-1]}: {errs[0]['error']}", "err")
        elif pend:
            self._say(f"{pend} pendiente(s): revísalas y actívalas para usarlas.", "warm")
        else:
            self._say(f"{len(self.skills)} skill(s).")
        self._changed()

    def _on_select(self):
        sk = self._selected()
        self._set_actions(sk)
        if not sk:
            return
        self._show_token += 1
        token = self._show_token
        self._set_text(self.detail, "Cargando…")
        self._bg(lambda: run_skills(["show", sk["name"]]), lambda r, e: self._on_show(token, r, e), exclusive=False)

    def _on_show(self, token, res, err):
        if token != self._show_token:
            return   # llegó tarde: ya eligieron otra
        if err or res.get("status") == "error":
            self._set_text(self.detail, f"No se pudo leer la skill: {err or res.get('detail')}")
            return
        trig = ", ".join(res["triggers"]) if res["triggers"] else "(sin triggers: se activa por su nombre y descripción)"
        lines = [f"{res['name']}  ·  {res['estado']}  ·  origen: {res['origin']}", res["description"], "",
                 f"Se activa con: {trig}", f"Se aplica en: {', '.join(res['scope'])}  ·  prioridad {res['priority']}"]
        if res["tools_declared"]:
            lines.append(f"Declara tools {res['tools_declared']}: se ignoran; las tools siguen pidiendo confirmación.")
        body = res["body"]
        if len(body) > BODY_PREVIEW_CHARS:
            body = body[:BODY_PREVIEW_CHARS] + f"\n[… {len(res['body']) - BODY_PREVIEW_CHARS} caracteres más]"
        lines += ["", "─" * 60, body]
        if res["compact"]:
            lines += ["", "─" * 60, "Versión compacta (modelos locales):", res["compact"]]
        self._set_text(self.detail, "\n".join(lines))

    # ------------------------------------------------------------------ instalar

    def _install_url(self):
        url = simpledialog.askstring(
            "Instalar skill", "URL de un SKILL.md o de un .zip\n(sirve el link de GitHub, se convierte solo):", parent=self)
        if url and url.strip():
            self._install(url.strip())

    def _install_file(self):
        path = filedialog.askopenfilename(parent=self, title="Elegir skill",
                                          filetypes=[("Skill (.md / .zip)", "*.md *.zip"), ("Todos", "*.*")])
        if path:
            self._install(path)

    def _install_dir(self):
        path = filedialog.askdirectory(parent=self, title="Elegir carpeta con SKILL.md")
        if path:
            self._install(path)

    def _install(self, source):
        self._bg(lambda: run_skills(["install", source], timeout=90), self._after_install, "Instalando…")

    def _after_install(self, res, err):
        if err:
            messagebox.showerror("No se pudo instalar", err, parent=self)
            self._say("Falló la instalación.", "err")
            return
        problems = [f"• {r['name']}: {r['error']}" for r in res.get("skills", []) if "error" in r]
        if res.get("status") == "error" and not any("error" not in r for r in res.get("skills", [])):
            messagebox.showwarning("No se instaló nada", res.get("detail", "") + ("\n\n" + "\n".join(problems) if problems else ""),
                                   parent=self)
            self._say("No se instaló nada.", "err")
            return
        to_enable = []
        for r in res["skills"]:
            if "error" in r:
                continue
            msg = [f"{r['description']}", "", f"Tamaño: {r['chars']} caracteres"]
            msg.append("Se activa con: " + (", ".join(r["triggers"]) if r["triggers"] else "su nombre y descripción"))
            msg += [f"⚠ {w}" for w in r["warnings"]]
            msg += ["", "Vista previa:", r["preview"][:400], "",
                    "Quedó PENDIENTE. Una skill es texto que se suma a las instrucciones de ELIPSE: "
                    "actívala solo si confías en el origen.", "", "¿Activarla ahora?"]
            if messagebox.askyesno(f"Skill «{r['name']}»", "\n".join(msg), parent=self):
                to_enable.append(r["name"])
        if problems:
            messagebox.showwarning("Algunas no se instalaron", "\n".join(problems), parent=self)
        if to_enable:
            self._bg(lambda: [run_skills(["enable", n]) for n in to_enable],
                     lambda r, e: self._after_action(e, f"Activada: {', '.join(to_enable)}"), "Activando…")
        else:
            self._after_action(None, "Instalada, pendiente de activar.")

    # ------------------------------------------------------------------ acciones

    def _after_action(self, err, ok_msg):
        if err:
            messagebox.showerror("Error", err, parent=self)
            self._say(err, "err")
        else:
            self.app.toast(ok_msg)
        self.refresh()

    def _toggle(self):
        sk = self._selected()
        if not sk:
            return
        cmd = "disable" if sk["status"] == "activa" else "enable"
        self._bg(lambda: run_skills([cmd, sk["name"]]),
                 lambda r, e: self._after_action(e or (r.get("detail") if r.get("status") == "error" else None),
                                                 f"«{sk['name']}» " + ("desactivada" if cmd == "disable" else "activada")),
                 "Aplicando…")

    def _update(self):
        sk = self._selected()
        if not sk:
            return
        if not messagebox.askyesno(
                "Actualizar skill", f"Se vuelve a descargar «{sk['name']}» desde su origen. Si cambió, quedará "
                "pendiente para que la apruebes de nuevo. ¿Continuar?", parent=self):
            return
        self._bg(lambda: run_skills(["update", sk["name"]], timeout=90), self._after_update, "Descargando…")

    def _after_update(self, res, err):
        if err or res.get("status") == "error" and not res.get("skills"):
            messagebox.showinfo("Actualizar", err or res.get("detail", "No se pudo actualizar."), parent=self)
            self._say("No se actualizó.", "err")
            self.refresh()
            return
        self._after_install(res, None)

    def _delete(self):
        sk = self._selected()
        if not sk:
            return
        if not messagebox.askyesno("Eliminar skill", f"¿Eliminar «{sk['name']}»? Se borra de la carpeta skills/. "
                                   "Si solo quieres dejar de usarla, usa «Desactivar».", parent=self):
            return
        self._bg(lambda: run_skills(["remove", sk["name"]]),
                 lambda r, e: self._after_action(e or (r.get("detail") if r.get("status") == "error" else None),
                                                 f"«{sk['name']}» eliminada"), "Eliminando…")

    # ------------------------------------------------------------------ probador

    def _test(self):
        text = self.msg.get().strip()
        if not text:
            self.app.toast("Escribe un mensaje de ejemplo")
            return
        args = ["test"] + (["--compact"] if self.compact.get() else []) + ["--", text]
        self._set_text(self.result, "Probando…")
        self._bg(lambda: run_skills(args), self._on_test, exclusive=False)

    def _on_test(self, res, err):
        if err or res.get("status") == "error":
            self._set_text(self.result, f"No se pudo probar: {err or res.get('detail')}")
            return
        sel = res.get("selected", [])
        out = [("Se activaría: " + ", ".join(sel)) if sel else "Ninguna skill se activaría con este mensaje.", ""]
        scores = sorted(res.get("scores", {}).items(), key=lambda kv: -kv[1])
        if scores:
            out.append(f"Puntajes (mínimo para activarse: {res.get('min_score', 4)}):")
            out += [f"  {'✔' if n in sel else ' '} {n}: {s}" for n, s in scores]
            out += ["", "Solo cuentan las skills activas (las pendientes y desactivadas no participan)."]
        else:
            out.append("No hay skills activas para este tipo de mensaje.")
        if res.get("injected"):
            out += ["", f"Texto que se agrega al mensaje ({res['chars_injected']} caracteres):", "─" * 60, res["injected"]]
        self._set_text(self.result, "\n".join(out))
