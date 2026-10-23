"""
Diálogo "¿Qué usa ELIPSE para pensar?" para UN rol (razonamiento general,
código o investigación). Es la misma UI que antes vivía fija dentro de la
pestaña "Modelo" de Ajustes, pero parametrizada por `prefix` para poder
abrirla una vez por rol sin duplicar código:

    prefix=""         -> rol general (lee/escribe DEFAULT_PROVIDER, OLLAMA_MODEL, ...)
    prefix="CODE_"     -> rol código (lee/escribe CODE_PROVIDER, CODE_OLLAMA_MODEL, ...)
    prefix="RESEARCH_" -> rol investigación (RESEARCH_PROVIDER, RESEARCH_OLLAMA_MODEL, ...)

Los nombres de variable coinciden 1:1 con los campos de app/config.py
(p. ej. `code_openai_base_url` -> `CODE_OPENAI_BASE_URL`), así que no hace
falta nada del lado del backend aparte de esos campos ya existir ahí.

Los roles que no son el general pueden "heredar" el rol general por completo
dejando su PROVIDER vacío: ese es el estado inicial y el que deja este
diálogo si el usuario tilda "Heredar de Razonamiento general".
"""

import concurrent.futures
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .env_store import env_dict, env_update
from .network import (
    check_gemini_tool_support,
    check_tool_support,
    fetch_gemini_models,
    fetch_models,
    list_ollama_models,
)
from .theme import C, FMONO, FT
from .uiqueue import ui
from .nav import Page
from .widgets import Pill, Surface, field, lbl, safe_grab

OPENAI_PRESETS = {
    "Mistral": ("https://api.mistral.ai/v1", "Usa «Obtener modelos». La capa gratuita tiene límites bajos."),
    "Groq": ("https://api.groq.com/openai/v1", "Gratis con cupos diarios; usa «Obtener modelos»."),
    "OpenRouter": ("https://openrouter.ai/api/v1", "Cientos de modelos; los que terminan en :free tienen cupo diario chico."),
    "OpenAI": ("https://api.openai.com/v1", "Por ejemplo gpt-4o-mini; revisa qué modelos tiene tu cuenta."),
    "LM Studio (local)": ("http://localhost:1234/v1", "La API key puede quedar vacía."),
    "Personalizado": ("", "Cualquier servidor compatible con la API de OpenAI (vLLM y similares)."),
}
ANTHROPIC_MODELS = ["claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-opus-5-5"]
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]
PROVIDERS = ("ollama", "anthropic", "openai", "gemini")

ROLES = {
    # prefix: (label, descripción corta)
    "": ("Razonamiento general", "Lo que usa ELIPSE por default para pensar y responder."),
    "CODE_": ("Código", "Consultas que el router detecta como de programación."),
    "RESEARCH_": ("Investigación", "El resumen de hallazgos dentro de investigar_tema."),
    "VISION_": ("Visión", "El modelo que MIRA las imágenes que adjuntas y las describe en texto. "
                          "No hereda del general (el modelo principal no ve imágenes): elige uno con visión."),
}

# Visión: a diferencia de CODE_/RESEARCH_, NO hereda del general. Si VISION_PROVIDER no está
# en el .env, el backend usa estos valores por defecto (ver app/config.py); si está VACÍO,
# la visión queda desactivada.
VISION_PREFIX = "VISION_"
VISION_DEFAULT_PROVIDER = "ollama"
VISION_DEFAULT_MODEL = "qwen3-vl:2b"
VISION_NAME_HINTS = ("vl", "llava", "vision", "minicpm-v", "moondream", "gemma3", "bakllava", "llama4", "mistral-small3")


def role_summary(prefix: str) -> str:
    """Texto corto para la fila de la lista de roles, sin abrir el diálogo."""
    env = env_dict()
    provider_key = "DEFAULT_PROVIDER" if not prefix else f"{prefix}PROVIDER"
    is_vision = prefix == VISION_PREFIX
    provider = env.get(provider_key, VISION_DEFAULT_PROVIDER if is_vision else ("" if prefix else "ollama")).strip().lower()

    if is_vision and not provider:
        return "Desactivada (las imágenes adjuntas no se analizan)"
    if prefix and not provider:
        return "Hereda de Razonamiento general"

    if provider not in PROVIDERS:
        provider = "ollama"

    model_key = {
        "ollama": f"{prefix}OLLAMA_MODEL",
        "anthropic": f"{prefix}ANTHROPIC_MODEL",
        "openai": f"{prefix}OPENAI_MODEL",
        "gemini": f"{prefix}GEMINI_MODEL",
    }[provider]
    model = env.get(model_key, "").strip()
    if is_vision and provider == "ollama" and not model:
        model = VISION_DEFAULT_MODEL
    label = {"ollama": "Ollama", "anthropic": "Claude", "openai": "Compatible con OpenAI", "gemini": "Gemini"}[provider]
    return f"{label}" + (f" · {model}" if model else "")


class ProviderDialog(Page):
    """Página: configura el proveedor de un solo rol y guarda al cerrar."""

    WIDTH = 780

    def __init__(self, app, prefix: str, on_saved=None,start_unchecked=False):
        super().__init__(app)
        self.app = app
        self.prefix = prefix
        self.on_saved = on_saved
        role_label, role_desc = ROLES[prefix]
        self.title(f"Ajustes de ELIPSE — {role_label}")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(app)
        safe_grab(self)

        env = env_dict()
        is_vision = self.is_vision = prefix == VISION_PREFIX

        # Barra inferior FIJA: el botón de Guardar vive acá, fuera del área que
        # crece con el checklist de modelos. Así nunca queda tapado ni cortado
        # fuera de la ventana, sin importar cuántos modelos se detecten.
        footer = self._footer = tk.Frame(self, bg=C["bg"])
        footer.pack(side="bottom", fill="x", padx=28, pady=(10, 22))

        # Área de contenido con scroll propio: si el checklist crece mucho,
        # scrollea acá adentro en vez de estirar la ventana sin límite.
        canvas_wrap = tk.Frame(self, bg=C["bg"])
        canvas_wrap.pack(side="top", fill="both", expand=True, padx=28, pady=(22, 0))
        canvas = self._canvas = tk.Canvas(canvas_wrap, bg=C["bg"], highlightthickness=0)
        vsb = ttk.Scrollbar(canvas_wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        f = self._content = tk.Frame(canvas, bg=C["bg"])
        f_window = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(f_window, width=e.width))

        def _wheel(event):
            delta = -1 * int(event.delta / 120) if event.delta else (-1 if event.num == 4 else 1)
            canvas.yview_scroll(delta, "units")
        canvas.bind("<Enter>", lambda e: (
            canvas.bind_all("<MouseWheel>", _wheel),
            canvas.bind_all("<Button-4>", _wheel),
            canvas.bind_all("<Button-5>", _wheel),
        ))
        canvas.bind("<Leave>", lambda e: (
            canvas.unbind_all("<MouseWheel>"),
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>"),
        ))

        tk.Label(f, text=role_label, font=FT, fg=C["primary"], bg=C["bg"]).pack(anchor="w")
        lbl(f, role_desc, wrap=700).pack(anchor="w", pady=(2, 14))

        self.inherit = tk.BooleanVar(value=False)
        if prefix:
            provider_env = env.get(f"{prefix}PROVIDER", "").strip()
            if is_vision:
                # Visión: clave ausente = activa por default; clave presente y vacía = desactivada.
                self.inherit.set(f"{prefix}PROVIDER" in env and not provider_env)
                chk_text = "Desactivar la visión (ELIPSE no analizará las imágenes adjuntas)"
            else:
                # Si no hay proveedor propio guardado y el diálogo se abrió desde
                # "+ Agregar", arranca directo en el selector (desmarcado) en vez
                # de esconderlo detrás del checkbox de heredar.
                self.inherit.set(bool(not provider_env) and not start_unchecked)
                chk_text = "Heredar de Razonamiento general (no usar un proveedor distinto para este rol)"
            chk = tk.Checkbutton(
                f, text=chk_text,
                variable=self.inherit, command=self._toggle_inherit,
                bg=C["bg"], fg=C["text"], selectcolor=C["card_hi"], activebackground=C["bg"],
                activeforeground=C["text"], highlightthickness=0,
            )
            chk.pack(anchor="w", pady=(0, 12))

        self._picker_title = tk.Label(f, text="¿Qué usa ELIPSE para ver imágenes?" if is_vision else "¿Qué usa ELIPSE para pensar?", font=FT, fg=C["text"], bg=C["bg"])
        self._picker_title.pack(anchor="w", pady=(4, 10))
        chips = self._chips_frame = tk.Frame(f, bg=C["bg"])
        chips.pack(anchor="w", pady=(0, 12))
        prov = env.get("DEFAULT_PROVIDER" if not prefix else f"{prefix}PROVIDER", "ollama" if (not prefix or is_vision) else "").strip().lower()
        self.prov = tk.StringVar(value=prov if prov in PROVIDERS else "ollama")
        self.chips = {}
        for v, t in (
            ("ollama", "Ollama, local"),
            ("anthropic", "Claude"),
            ("openai", "Compatible con OpenAI"),
            ("gemini", "Gemini"),
        ):
            b = Pill(chips, t, lambda v=v: self._set_prov(v), height=38)
            b.pack(side="left", padx=(0, 8))
            self.chips[v] = b
        box = self.model_box = Surface(f)
        box.pack(fill="x")
        box.inner.grid_columnconfigure(0, weight=1)
        self.pages = {k: tk.Frame(box.inner, bg=C["card"]) for k in PROVIDERS}
        for p in self.pages.values():
            p.grid(row=0, column=0, sticky="nsew")

        # Ollama
        p = self.pages["ollama"]
        lbl(p, "Modelo de Ollama").pack(anchor="w")
        r = tk.Frame(p, bg=C["card"])
        r.pack(fill="x", pady=(4, 8))
        self.o_model = tk.StringVar(value=env.get(f"{prefix}OLLAMA_MODEL", "") or (VISION_DEFAULT_MODEL if is_vision else ""))
        self.o_combo = ttk.Combobox(r, textvariable=self.o_model, values=[])
        self.o_combo.pack(side="left", fill="x", expand=True)
        Pill(r, "↻", self._refresh_ollama, kind="ghost", width=44, height=36).pack(side="left", padx=(8, 0))
        self.o_status = lbl(p, "Buscando modelos instalados…", wrap=600)
        self.o_status.pack(anchor="w")

        # Anthropic
        p = self.pages["anthropic"]
        lbl(p, "API key de Anthropic").pack(anchor="w")
        r = tk.Frame(p, bg=C["card"])
        r.pack(fill="x", pady=(4, 12))
        self.a_key = tk.StringVar(value=env.get(f"{prefix}ANTHROPIC_API_KEY", ""))
        e = field(r, self.a_key, show="•")
        e.pack(side="left", fill="x", expand=True, ipady=7)
        Pill(r, "Ver", lambda e=e: e.config(show="" if e.cget("show") else "•"), kind="ghost", width=60, height=36).pack(side="left", padx=(8, 0))
        lbl(p, "Modelo").pack(anchor="w")
        self.a_model = tk.StringVar(value=env.get(f"{prefix}ANTHROPIC_MODEL", "") or ("claude-sonnet-5" if not prefix else ""))
        ttk.Combobox(p, textvariable=self.a_model, values=ANTHROPIC_MODELS).pack(fill="x", pady=(4, 6))
        lbl(p, "Vacío = usa el mismo modelo que Razonamiento general." if prefix
            else "claude-haiku-4-5-20251001 es el más barato de la lista.").pack(anchor="w")

        # Compatible con OpenAI
        p = self.pages["openai"]
        base = env.get(f"{prefix}OPENAI_BASE_URL", "")
        guess = next((n for n, (u, _) in OPENAI_PRESETS.items() if u and u == base), "Personalizado" if base else "Mistral")
        lbl(p, "Servicio").pack(anchor="w")
        self.x_preset = tk.StringVar(value=guess)
        pc = ttk.Combobox(p, textvariable=self.x_preset, values=list(OPENAI_PRESETS), state="readonly")
        pc.pack(fill="x", pady=(4, 8))
        pc.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())
        lbl(p, "URL base").pack(anchor="w")
        self.x_base = tk.StringVar(value=base or (OPENAI_PRESETS[guess][0] if not prefix else ""))
        field(p, self.x_base).pack(fill="x", pady=(4, 8), ipady=7)
        lbl(p, "API key (vacía si el servidor es local)").pack(anchor="w")
        r = tk.Frame(p, bg=C["card"])
        r.pack(fill="x", pady=(4, 8))
        self.x_key = tk.StringVar(value=env.get(f"{prefix}OPENAI_API_KEY", ""))
        e = field(r, self.x_key, show="•")
        e.pack(side="left", fill="x", expand=True, ipady=7)
        Pill(r, "Ver", lambda e=e: e.config(show="" if e.cget("show") else "•"), kind="ghost", width=60, height=36).pack(side="left", padx=(8, 0))
        lbl(p, "Modelo" + (" (obligatorio)" if not prefix else " (vacío = hereda el general)")).pack(anchor="w")
        r = tk.Frame(p, bg=C["card"])
        r.pack(fill="x", pady=(4, 6))
        self.x_model = tk.StringVar(value=env.get(f"{prefix}OPENAI_MODEL", ""))
        self.x_combo = ttk.Combobox(r, textvariable=self.x_model)
        self.x_combo.pack(side="left", fill="x", expand=True)
        Pill(r, "Obtener modelos", self._fetch, kind="tonal", height=36).pack(side="left", padx=(8, 0))
        self.x_status = lbl(p, "")
        self.x_status.pack(anchor="w")
        self.x_checklist = tk.Text(p, height=1, bg=C["card_hi"], fg=C["text"], font=FMONO, relief="flat", bd=0,
                                   padx=10, pady=8, wrap="none", highlightthickness=0, state="disabled")
        for tag, color in (("ok", C["good"]), ("no", C["err"]), ("und", C["dim"])):
            self.x_checklist.tag_configure(tag, foreground=color)
        self.x_hint = lbl(p, OPENAI_PRESETS[guess][1], wrap=600)
        self.x_hint.pack(anchor="w", pady=(2, 0))

        # Gemini (Google AI)
        p = self.pages["gemini"]
        lbl(p, "API key de Google AI (Gemini)").pack(anchor="w")
        r = tk.Frame(p, bg=C["card"])
        r.pack(fill="x", pady=(4, 12))
        self.g_key = tk.StringVar(value=env.get(f"{prefix}GEMINI_API_KEY", ""))
        e = field(r, self.g_key, show="•")
        e.pack(side="left", fill="x", expand=True, ipady=7)
        Pill(r, "Ver", lambda e=e: e.config(show="" if e.cget("show") else "•"), kind="ghost", width=60, height=36).pack(side="left", padx=(8, 0))
        lbl(p, "Modelo").pack(anchor="w")
        r = tk.Frame(p, bg=C["card"])
        r.pack(fill="x", pady=(4, 6))
        self.g_model = tk.StringVar(value=env.get(f"{prefix}GEMINI_MODEL", "") or ("gemini-2.5-flash" if not prefix else ""))
        self.g_combo = ttk.Combobox(r, textvariable=self.g_model, values=GEMINI_MODELS)
        self.g_combo.pack(side="left", fill="x", expand=True)
        Pill(r, "Obtener modelos", self._fetch_gemini, kind="tonal", height=36).pack(side="left", padx=(8, 0))
        self.g_status = lbl(p, "Usá «Obtener modelos» para listar lo que permite tu API key.", wrap=600)
        self.g_status.pack(anchor="w")
        self.g_checklist = tk.Text(p, height=1, bg=C["card_hi"], fg=C["text"], font=FMONO, relief="flat", bd=0,
                                   padx=10, pady=8, wrap="none", highlightthickness=0, state="disabled")
        for tag, color in (("ok", C["good"]), ("no", C["err"]), ("und", C["dim"])):
            self.g_checklist.tag_configure(tag, foreground=color)
        lbl(p, "✓ = soporta tools (necesario para ELIPSE). ✗ = no. ? = no se pudo comprobar.", wrap=600).pack(anchor="w", pady=(2, 0))

        self._save_btn = Pill(footer, "Guardar y aplicar", self._save, kind="filled", height=46)
        self._save_btn.pack(anchor="e")
        self._set_prov(self.prov.get())
        self._toggle_inherit()
        self._refresh_ollama()
        self.after_idle(self._autosize)
        self.after(150, self._autosize)

    def _autosize(self):
        try:
            self.update_idletasks()
            footer_h = self._footer.winfo_reqheight()
            content_h = self._content.winfo_reqheight()
            margins = 22 + 10 + 22  # pady del canvas_wrap (arriba) + pady del footer (arriba, abajo)
            max_total = self.winfo_screenheight() - 100
            max_canvas_h = max(240, max_total - footer_h - margins)  # 240 = alto mínimo razonable
            canvas_h = min(content_h, max_canvas_h)
            self._canvas.configure(height=canvas_h)
            total_h = canvas_h + footer_h + margins
            self.geometry(f"{self.WIDTH}x{total_h}")
        except tk.TclError:
            pass

    def _toggle_inherit(self):
        """Solo aplica a roles no-generales: oculta/muestra el selector de proveedor."""
        if not self.prefix:
            return
        if self.inherit.get():
            for w in (self._picker_title, self._chips_frame, self.model_box):
                w.pack_forget()
        else:
            self._picker_title.pack(anchor="w", pady=(4, 10))
            self._chips_frame.pack(anchor="w", pady=(0, 12))
            self.model_box.pack(fill="x")
        self.after(30, self._autosize)

    def _set_prov(self, v):
        self.prov.set(v)
        self.pages[v].tkraise()
        for k, b in self.chips.items():
            b.set(kind="filled" if k == v else "tonal")

    def _refresh_ollama(self):
        self.o_status.config(text="Buscando modelos instalados…", fg=C["muted"])

        def run():
            models, notes = list_ollama_models()
            ui(self._got_ollama, models, notes)
        threading.Thread(target=run, daemon=True).start()

    def _got_ollama(self, models, notes):
        if not self.winfo_exists():
            return
        self.o_combo["values"] = models
        current = self.o_model.get().strip()
        if models:
            msg = f"{len(models)} modelos instalados. Elige uno de la lista o escríbelo igual que en «ollama pull»."
            if current and current not in models:
                if self.is_vision:
                    msg += f"\n«{current}» no está instalado. Instálalo con:  ollama pull {current}"
                else:
                    hint = next((m for m in models if current.split(":")[0] in m), models[0])
                    msg += f"\n«{current}» no está instalado. ¿Quizá quisiste «{hint}»?"
            elif self.is_vision and current and not any(h in current.lower() for h in VISION_NAME_HINTS):
                msg += f"\nOjo: «{current}» no parece un modelo con visión (p. ej. qwen3-vl, llava, gemma3, minicpm-v)."
            self.o_status.config(text=msg, fg=C["muted"])
        else:
            detail = (" — " + notes[-1]) if notes else ""
            self.o_status.config(
                text="No pude listar modelos instalados" + detail
                + ".\nEscribí el nombre a mano, p. ej. qwen2.5-coder:3b, o dale a ↻ de nuevo.",
                fg=C["err"] if notes else C["muted"],
            )

    def _apply_preset(self):
        url, hint = OPENAI_PRESETS[self.x_preset.get()]
        if url:
            self.x_base.set(url)
        self.x_hint.config(text=hint)

    def _fetch(self):
        base, key = self.x_base.get().strip(), self.x_key.get().strip()
        if not base:
            messagebox.showerror("Falta la URL base", "Escribe la URL base del servicio primero.", parent=self)
            return
        self.x_status.config(text="Consultando…", fg=C["muted"])
        self.x_checklist.pack_forget()
        self.model_box.fit()
        self._autosize()

        def run():
            try:
                ui(self._got_models, fetch_models(base, key), None)
            except Exception as e:
                ui(self._got_models, [], str(e))
        threading.Thread(target=run, daemon=True).start()

    def _fetch_gemini(self):
        key = self.g_key.get().strip()
        if not key:
            messagebox.showerror("Falta la API key", "Ingresa tu API key de Google AI primero.", parent=self)
            return
        self.g_status.config(text="Consultando modelos de Gemini…", fg=C["muted"])
        self.g_checklist.pack_forget()
        self.model_box.fit()
        self._autosize()

        def run():
            try:
                ui(self._got_gemini_models, fetch_gemini_models(key), None)
            except Exception as e:
                ui(self._got_gemini_models, [], str(e))
        threading.Thread(target=run, daemon=True).start()

    def _got_gemini_models(self, models, err):
        if not self.winfo_exists():
            return
        if err or not models:
            self.g_status.config(
                text=err or "La API no devolvió modelos con generateContent.",
                fg=C["err"] if err else C["muted"],
            )
            return
        self.g_combo["values"] = models
        if self.g_model.get().strip() not in models:
            self.g_model.set(models[0])
        self.g_status.config(
            text=f"{len(models)} modelos encontrados. Verificando cuáles soportan tool-calling…",
            fg=C["good"],
        )
        self._check_gemini_tool_support(models)

    def _check_gemini_tool_support(self, models):
        key = self.g_key.get().strip()
        icon = {True: ("✓", "ok"), False: ("✗", "no"), None: ("?", "und")}
        results = {}
        self.g_checklist.pack(fill="x", pady=(6, 8))
        self._render_gemini_checklist(models, results, icon)

        def worker():
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(check_gemini_tool_support, key, m): m for m in models}
                for fut in concurrent.futures.as_completed(futs):
                    m = futs[fut]
                    try:
                        results[m] = fut.result()
                    except Exception:
                        results[m] = None
                    ui(self._render_gemini_checklist, models, dict(results), icon)
            ui(self._gemini_checklist_done, len(models))
        threading.Thread(target=worker, daemon=True).start()

    def _render_gemini_checklist(self, models, results, icon):
        if not self.winfo_exists():
            return
        self.g_checklist.config(state="normal", height=min(len(models), 12))
        self.g_checklist.delete("1.0", "end")
        for m in models:
            mark, tag = icon[results[m]] if m in results else ("…", "und")
            self.g_checklist.insert("end", f"{mark}  ", tag)
            self.g_checklist.insert("end", f"{m}\n")
        self.g_checklist.config(state="disabled")
        self.model_box.fit()
        self._autosize()

    def _gemini_checklist_done(self, total):
        if not self.winfo_exists():
            return
        self.g_status.config(text=f"{total} modelos encontrados.", fg=C["good"])

    def _got_models(self, models, err):
        if not self.winfo_exists():
            return
        if err or not models:
            self.x_status.config(text=err or "El servidor no devolvió modelos.", fg=C["err"] if err else C["muted"])
            return
        self.x_combo["values"] = models
        if self.x_model.get().strip() not in models:
            self.x_model.set(models[0])
        self.x_status.config(text=f"{len(models)} modelos encontrados. Verificando cuáles soportan tool-calling…", fg=C["good"])
        self._check_tool_support(models)

    def _check_tool_support(self, models):
        base, key = self.x_base.get().strip(), self.x_key.get().strip()
        icon = {True: ("✓", "ok"), False: ("✗", "no"), None: ("?", "und")}
        results = {}
        self.x_checklist.pack(fill="x", pady=(6, 8))
        self._render_checklist(models, results, icon)

        def worker():
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futs = {ex.submit(check_tool_support, base, key, m): m for m in models}
                for fut in concurrent.futures.as_completed(futs):
                    m = futs[fut]
                    try:
                        results[m] = fut.result()
                    except Exception:
                        results[m] = None
                    ui(self._render_checklist, models, dict(results), icon)
            ui(self._checklist_done, len(models))
        threading.Thread(target=worker, daemon=True).start()

    def _render_checklist(self, models, results, icon):
        if not self.winfo_exists():
            return
        self.x_checklist.config(state="normal", height=min(len(models), 10))
        self.x_checklist.delete("1.0", "end")
        for m in models:
            mark, tag = icon[results[m]] if m in results else ("…", "und")
            self.x_checklist.insert("end", f"{mark}  ", tag)
            self.x_checklist.insert("end", f"{m}\n")
        self.x_checklist.config(state="disabled")
        self.model_box.fit()
        self._autosize()

    def _checklist_done(self, total):
        if not self.winfo_exists():
            return
        self.x_status.config(text=f"{total} modelos encontrados.", fg=C["good"])

    def _save(self):
        prefix = self.prefix

        if prefix and self.inherit.get():
            # Hereda: solo hace falta vaciar el proveedor propio del rol.
            env_update({f"{prefix}PROVIDER": ""})
            self.app.toast("Visión desactivada" if self.is_vision else "Guardado: este rol hereda Razonamiento general")
            self._finish()
            return

        p = self.prov.get()
        provider_key = "DEFAULT_PROVIDER" if not prefix else f"{prefix}PROVIDER"
        up = {provider_key: p}

        if p == "ollama":
            model = self.o_model.get().strip() or (VISION_DEFAULT_MODEL if self.is_vision else "")
            if model:
                up[f"{prefix}OLLAMA_MODEL"] = model
        elif p == "anthropic":
            if not prefix and not self.a_key.get().strip():
                messagebox.showerror("Falta la API key", "Ingresa tu API key de Anthropic.", parent=self)
                return
            if self.a_key.get().strip():
                up[f"{prefix}ANTHROPIC_API_KEY"] = self.a_key.get().strip()
            up[f"{prefix}ANTHROPIC_MODEL"] = self.a_model.get().strip() or ("claude-sonnet-5" if not prefix else "")
        elif p == "gemini":
            if not prefix and not self.g_key.get().strip():
                messagebox.showerror("Falta la API key", "Ingresa tu API key de Google AI (Gemini).", parent=self)
                return
            if self.g_key.get().strip():
                up[f"{prefix}GEMINI_API_KEY"] = self.g_key.get().strip()
            up[f"{prefix}GEMINI_MODEL"] = self.g_model.get().strip() or ("gemini-2.5-flash" if not prefix else "")
        else:
            if (not prefix or self.is_vision) and not self.x_model.get().strip():
                messagebox.showerror("Falta el modelo", "El modelo es obligatorio" + (" (debe tener visión)." if self.is_vision else "."), parent=self)
                return
            up[f"{prefix}OPENAI_BASE_URL"] = self.x_base.get().strip() or ("https://api.openai.com/v1" if not prefix else "")
            if self.x_key.get().strip():
                up[f"{prefix}OPENAI_API_KEY"] = self.x_key.get().strip()
            up[f"{prefix}OPENAI_MODEL"] = self.x_model.get().strip()

        try:
            env_update(up)
        except Exception as e:
            messagebox.showerror("No se pudo guardar", str(e), parent=self)
            return
        self.app.toast("Guardado en .env")
        self._finish()

    def _finish(self):
        if self.on_saved:
            self.on_saved()
        core = self.app.svc["elipse"]
        if core.state == "on" and messagebox.askyesno(
            "Reiniciar ELIPSE Core", "ELIPSE Core está corriendo. ¿Reiniciarlo para aplicar los cambios?", parent=self
        ):
            core.restart()
        self.destroy()