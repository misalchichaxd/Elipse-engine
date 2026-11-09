"""
Página «Adaptar skills»: compila cada skill para modelos chicos (4B) en un proceso aparte.

Toda la lógica vive en app/core/skillc.py; este panel solo la invoca con
`python -m app.core.skillc --json ...` (igual que skills_dialog con skills.py). Compilar puede tardar
(una llamada a la IA), por eso corre en un hilo y en otro proceso: el panel no se congela.

Seguridad: todo paquete compilado queda PENDIENTE y no se usa hasta aprobarlo aquí.
"""

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import theme
from .env_store import env_dict, env_update
from .nav import Page
from .process_utils import run_skillc
from .theme import C
from .uiqueue import ui
from .widgets import Pill, field, lbl

STATUS_FG = {"aprobada": C["good"], "pendiente": C["warm"], "desactualizada": C["err"]}
COMPILERS = ["Auto (modelo general)", "gemini", "anthropic", "openai", "ollama"]


def _cell(p):
    if not p:
        return "—"
    extra = f"{p['rules']} reglas · {p['steps']} pasos" if p["steps"] else f"{p['rules']} reglas"
    return f"{p['status']} · {extra}"


class SkillCompilerDialog(Page):
    WIDTH, HEIGHT = 820, 780

    def __init__(self, app, select=None, on_changed=None):
        super().__init__(app)
        self.app, self.on_changed, self.want = app, on_changed, select
        self.rows: dict = {}
        self.busy = False
        self.profile = tk.StringVar(value="tiny")
        self.title("Adaptar skills")
        self.configure(bg=C["bg"])
        self.geometry(f"{self.WIDTH}x{min(self.HEIGHT, max(560, self.winfo_screenheight() - 170))}")
        self.minsize(780, 560)
        self._build()
        self.refresh()
        self._info()

    # ------------------------------------------------------------------ construcción

    def _build(self):
        tk.Label(self, text="Adaptar skills", font=theme.FDS, fg=C["primary"], bg=C["bg"]).pack(anchor="w", padx=26, pady=(20, 4))
        lbl(self, "Una IA mejor lee la skill una sola vez y la convierte en un paquete a la medida de un modelo chico: "
                  "un checklist corto y, si es un procedimiento, pasos que ELIPSE ejecuta uno por llamada. "
                  "Lo compilado queda pendiente hasta que lo apruebes.", wrap=self.WIDTH - 60).pack(anchor="w", padx=26, pady=(0, 8))

        # --- compilador
        row = tk.Frame(self, bg=C["bg"])
        row.pack(fill="x", padx=26, pady=(0, 2))
        tk.Label(row, text="Compilador:", font=theme.FBB, fg=C["text"], bg=C["bg"]).pack(side="left")
        env = env_dict()
        cur = (env.get("SKILLC_PROVIDER") or "").strip().lower()
        self.comp = ttk.Combobox(row, values=COMPILERS, state="readonly", width=22)
        self.comp.set(cur if cur in COMPILERS else COMPILERS[0])
        self.comp.pack(side="left", padx=(8, 8))
        self.model = tk.StringVar(value=env.get(f"SKILLC_{cur.upper()}_MODEL", "") if cur else "")
        field(row, self.model).pack(side="left", ipady=5, padx=(0, 8))
        Pill(row, "Guardar", self._save_compiler, kind="tonal", height=34).pack(side="left")
        self.info = lbl(self, "", wrap=self.WIDTH - 60)
        self.info.pack(anchor="w", padx=26, pady=(2, 8))

        # --- lista
        box = tk.Frame(self, bg=C["bg"])
        box.pack(fill="x", padx=26)
        cols = (("nombre", "Skill", 170), ("tiny", "Modelo chico (≤5B)", 270), ("small", "Mediano (6–14B)", 270))
        self.tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", height=5, selectmode="browse")
        for c, tt, w in cols:
            self.tree.heading(c, text=tt, anchor="w")
            self.tree.column(c, width=w, anchor="w", stretch=(c != "nombre"))
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show())

        # --- acciones
        act = tk.Frame(self, bg=C["bg"])
        act.pack(fill="x", padx=26, pady=(10, 0))
        tk.Label(act, text="Perfil:", font=theme.FBB, fg=C["text"], bg=C["bg"]).pack(side="left")
        pc = ttk.Combobox(act, textvariable=self.profile, values=["tiny", "small"], state="readonly", width=7)
        pc.pack(side="left", padx=(6, 10))
        pc.bind("<<ComboboxSelected>>", lambda e: self._show())
        Pill(act, "Adaptar con IA", lambda: self._compile(True), kind="filled", height=36).pack(side="left")
        Pill(act, "Sin IA", lambda: self._compile(False), kind="tonal", height=36).pack(side="left", padx=(8, 0))
        Pill(act, "Aprobar", self._approve, kind="tonal", height=36).pack(side="left", padx=(8, 0))
        Pill(act, "Descartar", self._reject, kind="danger", height=36).pack(side="left", padx=(8, 0))
        self.status = lbl(act, "", wrap=300)
        self.status.pack(side="right")

        # --- detalle + A/B
        paned = tk.PanedWindow(self, orient="vertical", bg=C["bg"], bd=0, sashwidth=8, sashrelief="flat", opaqueresize=True)
        paned.pack(fill="both", expand=True, pady=(8, 14))
        top = tk.Frame(paned, bg=C["bg"])
        tk.Label(top, text="Paquete compilado", font=theme.FBB, fg=C["text"], bg=C["bg"]).pack(anchor="w", padx=26, pady=(4, 4))
        self.detail = self._text(top, theme.FMONO, 6)
        paned.add(top, minsize=120, stretch="always")
        bot = tk.Frame(paned, bg=C["bg"])
        tk.Label(bot, text="Probar A/B con tu modelo local (skill recortada como hoy  vs  por pasos)", font=theme.FBB,
                 fg=C["text"], bg=C["bg"]).pack(anchor="w", padx=26, pady=(4, 4))
        r = tk.Frame(bot, bg=C["bg"])
        r.pack(fill="x", padx=26)
        self.req = tk.StringVar()
        e = field(r, self.req)
        e.pack(side="left", fill="x", expand=True, ipady=7)
        e.bind("<Return>", lambda ev: self._ab())
        Pill(r, "Probar", self._ab, kind="filled", height=36).pack(side="left", padx=(8, 0))
        self.result = self._text(bot, theme.FMONO, 8)
        paned.add(bot, minsize=150, stretch="always")
        self._set(self.detail, "Elige una skill y pulsa «Adaptar con IA».")
        self._set(self.result, "Escribe un pedido de ejemplo (p. ej. «hazme una página web para un taller mecánico»). "
                               "Corre las dos versiones con tu modelo y las muestra una debajo de la otra. "
                               "Puede tardar un par de minutos.")

    def _text(self, parent, font, height):
        fr = tk.Frame(parent, bg=C["bg"])
        fr.pack(fill="both", expand=True, padx=26)
        t = tk.Text(fr, height=height, wrap="word", bg=C["card_hi"], fg=C["text"], insertbackground=C["text"], relief="flat",
                    font=font, padx=12, pady=10, highlightthickness=0, state="disabled")
        sb = ttk.Scrollbar(fr, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return t

    # ------------------------------------------------------------------ utilidades

    def _set(self, w, text):
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", text)
        w.configure(state="disabled")

    def _say(self, text, level="muted"):
        self.status.config(text=text, fg=C.get(level, C["muted"]))

    def _bg(self, fn, done, msg="", exclusive=True):
        if exclusive:
            if self.busy:
                return
            self.busy = True
        if msg:
            self._say(msg)

        def run():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, str(e)
            ui(self._fin, done, res, err, exclusive)
        threading.Thread(target=run, daemon=True).start()

    def _fin(self, done, res, err, exclusive):
        if exclusive:
            self.busy = False
        if self.winfo_exists():
            done(res, err)

    def _sel(self):
        s = self.tree.selection()
        return s[0] if s else None

    def _failed(self, res, err):
        return err or (res or {}).get("status") == "error"

    def _err_text(self, res, err):
        return err or (res or {}).get("detail") or "error desconocido"

    # ------------------------------------------------------------------ compilador y modelos

    def _save_compiler(self):
        sel = self.comp.get()
        prov = "" if sel == COMPILERS[0] else sel
        upd = {"SKILLC_PROVIDER": prov}
        if prov:
            upd[f"SKILLC_{prov.upper()}_MODEL"] = self.model.get().strip()
        env_update(upd)
        self.app.toast("Compilador guardado")
        self._info()

    def _info(self):
        self._bg(lambda: run_skillc(["info"]), self._on_info, exclusive=False)

    def _on_info(self, res, err):
        if self._failed(res, err):
            self.info.config(text=f"No se pudo leer la configuración: {self._err_text(res, err)}", fg=C["err"])
            return
        txt = f"Modelo de ELIPSE: {res.get('general')} (perfil {res.get('general_profile')})  ·  "
        if res.get("compiler"):
            txt += f"Compilador: {res['compiler']}" + (" (el mismo modelo general)" if res.get("compiler_origin") == "general" else "")
            warn = res.get("compiler_profile") == "tiny"
            self.info.config(text=txt + ("  ⚠ un modelo chico compilando baja la calidad: mejor usa uno de nube" if warn else ""),
                             fg=C["warm"] if warn else C["muted"])
        else:
            self.info.config(text=txt + f"Compilador: no disponible ({res.get('compiler_error', '')})", fg=C["err"])

    # ------------------------------------------------------------------ lista y detalle

    def refresh(self):
        self._bg(lambda: run_skillc(["list"]), self._on_list, "Leyendo…")

    def _on_list(self, res, err):
        if self._failed(res, err):
            self._say(self._err_text(res, err), "err")
            return
        keep = self._sel() or self.want
        self.want = None
        self.rows = {s["name"]: s for s in res["skills"]}
        for i in self.tree.get_children():
            self.tree.delete(i)
        for s in res["skills"]:
            p = s["profiles"]
            tag = (p["tiny"] or {}).get("status") or "none"
            self.tree.insert("", "end", iid=s["name"], values=(s["name"], _cell(p["tiny"]), _cell(p["small"])), tags=(tag,))
        for st, fg in STATUS_FG.items():
            self.tree.tag_configure(st, foreground=fg)
        if keep in self.rows:
            self.tree.selection_set(keep)
        self._say(f"{len(self.rows)} skill(s)")
        if self.on_changed:
            try:
                self.on_changed()
            except Exception:
                pass

    def _show(self):
        name = self._sel()
        if not name:
            return
        prof = self.profile.get()
        self._bg(lambda: run_skillc(["show", name, "--profile", prof]), lambda r, e: self._on_show(name, prof, r, e), exclusive=False)

    def _on_show(self, name, prof, res, err):
        if self._sel() != name or self.profile.get() != prof:
            return
        if self._failed(res, err):
            self._set(self.detail, f"«{name}» todavía no está adaptada para el perfil {prof}.")
            return
        p = res["package"]
        out = [f"{name} · {prof} · {res['estado']} · {p.get('mode')} ({p.get('compiler')})", "", "REGLAS (viajan en cada llamada):"]
        out += [f"  - {r}" for r in p["rules"]] + [f"  - NO: {r}" for r in p["never"]]
        if p["steps"]:
            out += ["", f"PASOS (se ejecutan uno por llamada, resultado: {p['assemble']}):"]
            for i, s in enumerate(p["steps"], 1):
                rep = f"  [por cada «{s['foreach']}»]" if s["foreach"] else ""
                out += [f"  {i}. [{s['kind']}] {s['title']}{rep}", f"     {s['instruction']}"]
                if s["guide"]:
                    out.append(f"     guía: {s['guide']}")
        else:
            out += ["", "Sin pasos: es una skill de reglas; solo se usa el checklist."]
        if p.get("triggers"):
            out += ["", "Se activa con: " + ", ".join(p["triggers"])]
        self._set(self.detail, "\n".join(out))

    # ------------------------------------------------------------------ acciones

    def _compile(self, ai):
        name = self._sel()
        if not name:
            self.app.toast("Elige una skill")
            return
        args = ["compile", name, "--profile", self.profile.get()] + ([] if ai else ["--no-ai"])
        self._bg(lambda: run_skillc(args, timeout=420), self._after_compile,
                 "Compilando… puede tardar un minuto" if ai else "Extrayendo…")

    def _after_compile(self, res, err):
        if self._failed(res, err):
            messagebox.showerror("No se pudo adaptar", self._err_text(res, err), parent=self)
            self._say("Falló.", "err")
            return
        if res.get("warnings"):
            messagebox.showwarning("Aviso", "\n\n".join(res["warnings"]), parent=self)
        self.app.toast("Listo: revisa el paquete y apruébalo")
        self.refresh()
        self._show()

    def _approve(self):
        self._set_status("approve", "Aprobada")

    def _reject(self):
        name = self._sel()
        if name and messagebox.askyesno("Descartar", f"¿Borrar el paquete {self.profile.get()} de «{name}»?", parent=self):
            self._set_status("reject", "Descartada")

    def _set_status(self, cmd, ok_msg):
        name = self._sel()
        if not name:
            return
        self._bg(lambda: run_skillc([cmd, name, "--profile", self.profile.get()]),
                 lambda r, e: self._after_status(r, e, ok_msg), "Aplicando…")

    def _after_status(self, res, err, ok_msg):
        if self._failed(res, err):
            messagebox.showerror("Error", self._err_text(res, err), parent=self)
        else:
            self.app.toast(ok_msg)
        self.refresh()
        self._show()

    def _ab(self):
        name, text = self._sel(), self.req.get().strip()
        if not name or not text:
            self.app.toast("Elige una skill y escribe un pedido")
            return
        args = ["test", name, "--profile", self.profile.get(), "--", text]
        self._set(self.result, "Corriendo las dos versiones con tu modelo…")
        self._bg(lambda: run_skillc(args, timeout=900), self._on_ab, "Probando…")

    def _on_ab(self, res, err):
        if self._failed(res, err):
            self._set(self.result, f"No se pudo probar: {self._err_text(res, err)}")
            self._say("")
            return
        a, b = res["a"], res["b"]
        out = [f"Modelo: {res['model']} · perfil {res['profile']}", "",
               f"━━ A · {a['label']} — {a['secs']} s, {a['chars']} caracteres ━━", a["output"], "",
               f"━━ B · {b['label']} — {b['secs']} s, {b['calls']} llamadas, {b['retries']} reintentos, "
               f"{'OK' if b['ok'] else 'FALLÓ'} ━━"]
        out += [f"  ! {n}" for n in b["notes"]]
        out.append(b["output"] or "(sin resultado)")
        self._set(self.result, "\n".join(out))
        self._say("Comparación lista")