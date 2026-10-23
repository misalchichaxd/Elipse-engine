"""
Diálogo "Voz": activar, modo, proveedores (Fish Audio / local), enrutamiento, privacidad,
ID de voz de Fish con detección automática, ganancia. Guarda en .env (VOICE_*).

Los nombres coinciden 1:1 con los campos voice_* de app/config.py. Ojo: no se escriben variables
que no existan ahí (pydantic-settings rechaza claves desconocidas en el .env). El nombre de la voz
detectada se guarda aparte, en panel_prefs.json.

Al guardar se borran los ajustes de voz hechos desde el cliente web (tabla voice_settings de
elipse.db): esos pisan al .env, y si no se borraran, lo que cambias aquí no tendría efecto.
"""

import socket
import sqlite3
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlparse

from . import theme
from .env_store import env_dict, env_update, load_prefs, save_prefs
from .fish_api import describe_voice, extract_fish_id, fetch_fish_voice
from .paths import ROOT
from .theme import C
from .uiqueue import ui
from .nav import Page
from .widgets import Pill, Surface, field, lbl, safe_grab

MODES = (("off", "Apagada"), ("manual", "Manual"), ("smart", "Inteligente"), ("always", "Siempre"))
MODE_HINTS = {
    "off": "Nunca habla.",
    "manual": "Solo habla cuando pulsas 🔊 en un mensaje.",
    "smart": "Habla respuestas conversacionales cortas; calla si es sobre todo código, tablas o documentos. Recomendado.",
    "always": "Habla todo lo hablable de cada respuesta (nunca código, tablas ni rutas).",
}
ROUTINGS = (("failover", "Con respaldo"), ("round_robin", "Alternar"), ("manual", "Solo el principal"))
ROUTING_HINTS = {
    "failover": "Usa el principal; si falla, pasa al otro. Recomendado.",
    "round_robin": "Alterna entre proveedores (la voz puede cambiar de una respuesta a otra).",
    "manual": "Usa únicamente el proveedor principal, sin respaldo.",
}
PROVIDER_CHIPS = (("fish", "Fish Audio (nube)"), ("local", "Local (tu TTS)"))
FISH_MODELS = ["s2.1-pro-free", "s2-pro", "s1"]
LOCAL_MIMES = ["audio/mpeg", "audio/wav"]
MODE_LABEL = dict(MODES)


def _bool(v, default):
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _providers(env):
    return [p.strip() for p in env.get("VOICE_PROVIDERS", "fish,local").split(",") if p.strip() in ("fish", "local")]


def voice_summary() -> str:
    """Texto corto para la fila de Ajustes."""
    env = env_dict()
    if not _bool(env.get("VOICE_ENABLED"), True):
        return "Desactivada"
    mode = MODE_LABEL.get(env.get("VOICE_MODE", "smart"), "Inteligente")
    provs = _providers(env) or ["fish"]
    if provs[0] == "local":
        return f"Local · {env.get('VOICE_LOCAL_URL', 'http://127.0.0.1:8002/tts')} · {mode}"
    if not env.get("VOICE_FISH_API_KEY", "").strip():
        return "Sin configurar · falta la API key de Fish Audio"
    vid = env.get("VOICE_FISH_VOICE_ID", "").strip()
    prefs = load_prefs()
    name = prefs.get("voice_fish_name", "") if prefs.get("voice_fish_id") == vid and vid else ""
    who = f"«{name}»" if name else (f"voz {vid[:8]}…" if vid else "voz por defecto")
    return f"Fish Audio · {who} · {mode}"


def _clear_web_overrides():
    """Borra los ajustes de voz guardados desde el cliente web, para que mande lo guardado aquí."""
    db = ROOT / "elipse.db"
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(db, timeout=10)
        conn.execute("DELETE FROM voice_settings")
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass                                    # la tabla se crea la primera vez que el core usa la voz


class VoiceDialog(Page):
    WIDTH = 780

    def __init__(self, app, on_saved=None):
        super().__init__(app)
        self.app, self.on_saved = app, on_saved
        self.title("Ajustes de ELIPSE — Voz")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(app)
        safe_grab(self)
        self._tok = 0
        self._after = None
        self._detected = None
        env = env_dict()

        # --- armazón: pie fijo + contenido con scroll (igual que ProviderDialog) ---
        footer = self._footer = tk.Frame(self, bg=C["bg"])
        footer.pack(side="bottom", fill="x", padx=28, pady=(10, 22))
        wrap = tk.Frame(self, bg=C["bg"])
        wrap.pack(side="top", fill="both", expand=True, padx=28, pady=(22, 0))
        canvas = self._canvas = tk.Canvas(wrap, bg=C["bg"], highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        f = self._content = tk.Frame(canvas, bg=C["bg"])
        win = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(-1 * int(event.delta / 120) if event.delta else (-1 if event.num == 4 else 1), "units")
        canvas.bind("<Enter>", lambda e: (canvas.bind_all("<MouseWheel>", _wheel), canvas.bind_all("<Button-4>", _wheel),
                                          canvas.bind_all("<Button-5>", _wheel)))
        canvas.bind("<Leave>", lambda e: (canvas.unbind_all("<MouseWheel>"), canvas.unbind_all("<Button-4>"),
                                          canvas.unbind_all("<Button-5>")))
        self.bind("<Destroy>", lambda e: self._cancel_detect() if e.widget is self else None)

        tk.Label(f, text="Voz", font=theme.FT, fg=C["primary"], bg=C["bg"]).pack(anchor="w")
        lbl(f, "ELIPSE habla solo cuando quieras y solo lo que tiene sentido decir en voz alta (nunca código, tablas ni rutas).",
            wrap=700).pack(anchor="w", pady=(2, 14))

        # ---------------- General ----------------
        provs = _providers(env) or ["fish", "local"]
        self.enabled = tk.BooleanVar(value=_bool(env.get("VOICE_ENABLED"), True))
        self.mode = tk.StringVar(value=env.get("VOICE_MODE", "smart") if env.get("VOICE_MODE", "smart") in MODE_LABEL else "smart")
        self.routing = tk.StringVar(value=env.get("VOICE_ROUTING", "failover") if env.get("VOICE_ROUTING", "failover") in dict(ROUTINGS) else "failover")
        self.primary = tk.StringVar(value=provs[0])
        self.backup = tk.BooleanVar(value=len(provs) > 1)
        self.cloud = tk.BooleanVar(value=_bool(env.get("VOICE_CLOUD_ALLOWED"), True))

        g = Surface(f)
        g.pack(fill="x", pady=(0, 12))
        gi = g.inner
        self._check(gi, "Voz activada", self.enabled).pack(anchor="w")
        lbl(gi, "Modo", fg=C["text"], font=theme.FBB).pack(anchor="w", pady=(10, 4))
        self.mode_chips = self._chips(gi, MODES, self.mode, self._mode_changed)
        self.mode_hint = lbl(gi, MODE_HINTS[self.mode.get()], wrap=660)
        self.mode_hint.pack(anchor="w", pady=(6, 0))
        lbl(gi, "Proveedor principal", fg=C["text"], font=theme.FBB).pack(anchor="w", pady=(12, 4))
        self._chips(gi, PROVIDER_CHIPS, self.primary, self._primary_changed)
        self._check(gi, "Usar el otro como respaldo", self.backup).pack(anchor="w", pady=(8, 0))
        lbl(gi, "Enrutamiento", fg=C["text"], font=theme.FBB).pack(anchor="w", pady=(10, 4))
        self.routing_chips = self._chips(gi, ROUTINGS, self.routing, self._routing_changed)
        self.routing_hint = lbl(gi, ROUTING_HINTS[self.routing.get()], wrap=660)
        self.routing_hint.pack(anchor="w", pady=(6, 0))
        self._check(gi, "Permitir proveedores en la nube", self.cloud).pack(anchor="w", pady=(10, 0))
        lbl(gi, "Desmarcado = modo privado: el texto nunca sale de tu PC y solo se usa el proveedor local.", wrap=660).pack(anchor="w")

        # ---------------- Fish Audio ----------------
        self.f_key = tk.StringVar(value=env.get("VOICE_FISH_API_KEY", ""))
        self.f_voice = tk.StringVar(value=env.get("VOICE_FISH_VOICE_ID", ""))
        self.f_model = tk.StringVar(value=env.get("VOICE_FISH_MODEL", "") or FISH_MODELS[0])
        self.f_gain = tk.StringVar(value=env.get("VOICE_FISH_GAIN", "") or "1.0")
        fb = Surface(f)
        fb.pack(fill="x", pady=(0, 12))
        fi = fb.inner
        tk.Label(fi, text="Fish Audio (nube)", font=theme.FBB, fg=C["text"], bg=C["card"]).pack(anchor="w")
        lbl(fi, "API key de Fish Audio", ).pack(anchor="w", pady=(8, 0))
        r = tk.Frame(fi, bg=C["card"])
        r.pack(fill="x", pady=(4, 8))
        e = field(r, self.f_key, show="•")
        e.pack(side="left", fill="x", expand=True, ipady=7)
        Pill(r, "Ver", lambda e=e: e.config(show="" if e.cget("show") else "•"), kind="ghost", width=60, height=36).pack(side="left", padx=(8, 0))
        lbl(fi, "ID de la voz (pega el ID o el enlace de fish.audio; se detecta sola)").pack(anchor="w")
        r = tk.Frame(fi, bg=C["card"])
        r.pack(fill="x", pady=(4, 4))
        field(r, self.f_voice).pack(side="left", fill="x", expand=True, ipady=7)
        Pill(r, "Detectar", lambda: self._detect(), kind="tonal", height=36).pack(side="left", padx=(8, 0))
        self.f_status = lbl(fi, "", wrap=640)
        self.f_status.pack(anchor="w", pady=(0, 8))
        row = tk.Frame(fi, bg=C["card"])
        row.pack(fill="x")
        left = tk.Frame(row, bg=C["card"])
        left.pack(side="left", fill="x", expand=True)
        lbl(left, "Modelo de voz de Fish").pack(anchor="w")
        ttk.Combobox(left, textvariable=self.f_model, values=FISH_MODELS).pack(fill="x", pady=(4, 0), padx=(0, 10))
        right = tk.Frame(row, bg=C["card"])
        right.pack(side="left")
        lbl(right, "Ganancia (volumen)").pack(anchor="w")
        field(right, self.f_gain).pack(pady=(4, 0), ipady=7)
        lbl(fi, "Modelo «s2.1-pro-free»: sin garantías de servicio, las solicitudes pueden retenerse. "
                "La ganancia sube o baja el volumen de esta voz (1.0 = normal).", wrap=660).pack(anchor="w", pady=(8, 0))

        # ---------------- Local ----------------
        self.l_url = tk.StringVar(value=env.get("VOICE_LOCAL_URL", "") or "http://127.0.0.1:8002/tts")
        self.l_voice = tk.StringVar(value=env.get("VOICE_LOCAL_VOICE_ID", ""))
        self.l_mime = tk.StringVar(value=env.get("VOICE_LOCAL_MIME", "") or LOCAL_MIMES[0])
        self.l_gain = tk.StringVar(value=env.get("VOICE_LOCAL_GAIN", "") or "1.0")
        lb = Surface(f)
        lb.pack(fill="x", pady=(0, 6))
        li = lb.inner
        tk.Label(li, text="Local (tu servidor TTS)", font=theme.FBB, fg=C["text"], bg=C["card"]).pack(anchor="w")
        lbl(li, "URL (recibe POST {text, voice_id} y devuelve audio por streaming)").pack(anchor="w", pady=(8, 0))
        r = tk.Frame(li, bg=C["card"])
        r.pack(fill="x", pady=(4, 4))
        field(r, self.l_url).pack(side="left", fill="x", expand=True, ipady=7)
        Pill(r, "Probar conexión", self._probe_local, kind="tonal", height=36).pack(side="left", padx=(8, 0))
        self.l_status = lbl(li, "", wrap=640)
        self.l_status.pack(anchor="w", pady=(0, 8))
        row = tk.Frame(li, bg=C["card"])
        row.pack(fill="x")
        a = tk.Frame(row, bg=C["card"])
        a.pack(side="left", fill="x", expand=True)
        lbl(a, "ID de voz (opcional)").pack(anchor="w")
        field(a, self.l_voice).pack(fill="x", pady=(4, 0), padx=(0, 10), ipady=7)
        b = tk.Frame(row, bg=C["card"])
        b.pack(side="left", padx=(0, 10))
        lbl(b, "Formato").pack(anchor="w")
        ttk.Combobox(b, textvariable=self.l_mime, values=LOCAL_MIMES, width=12).pack(pady=(4, 0))
        c = tk.Frame(row, bg=C["card"])
        c.pack(side="left")
        lbl(c, "Ganancia").pack(anchor="w")
        field(c, self.l_gain).pack(pady=(4, 0), ipady=7)

        lbl(f, "Guardar aquí reemplaza lo que hayas cambiado en la pestaña «Voz» del cliente web.", wrap=700).pack(anchor="w", pady=(8, 8))

        Pill(footer, "Guardar y aplicar", self._save, kind="filled", height=46).pack(anchor="e")

        self.f_voice.trace_add("write", self._voice_changed)
        if self.f_voice.get().strip():
            self.after(300, lambda: self._detect(auto=True))
        self.after_idle(self._autosize)
        self.after(150, self._autosize)

    # ---------- helpers de UI ----------
    def _check(self, parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var, bg=C["card"], fg=C["text"], selectcolor=C["card_hi"],
                              activebackground=C["card"], activeforeground=C["text"], highlightthickness=0, font=theme.FB, anchor="w")

    def _chips(self, parent, options, var, on_change):
        box = tk.Frame(parent, bg=C["card"])
        box.pack(anchor="w")
        chips = {}

        def pick(v):
            var.set(v)
            for k, b in chips.items():
                b.set(kind="filled" if k == v else "tonal")
            on_change()
        for v, t in options:
            b = Pill(box, t, lambda v=v: pick(v), kind="filled" if var.get() == v else "tonal", height=36)
            b.pack(side="left", padx=(0, 8))
            chips[v] = b
        return chips

    def _mode_changed(self):
        self.mode_hint.config(text=MODE_HINTS[self.mode.get()])

    def _routing_changed(self):
        self.routing_hint.config(text=ROUTING_HINTS[self.routing.get()])

    def _primary_changed(self):
        pass

    def _autosize(self):
        try:
            self.update_idletasks()
            footer_h = self._footer.winfo_reqheight()
            content_h = self._content.winfo_reqheight()
            margins = 22 + 10 + 22
            max_canvas_h = max(240, self.winfo_screenheight() - 100 - footer_h - margins)
            canvas_h = min(content_h, max_canvas_h)
            self._canvas.configure(height=canvas_h)
            self.geometry(f"{self.WIDTH}x{canvas_h + footer_h + margins}")
        except tk.TclError:
            pass

    def _say(self, label, text, level="muted"):
        color = {"ok": C["good"], "warn": C["warm"], "err": C["err"]}.get(level, C["muted"])
        label.config(text=text, fg=color)
        self.after(30, self._autosize)

    # ---------- detección de la voz de Fish ----------
    def _cancel_detect(self):
        if self._after:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _voice_changed(self, *_):
        raw = self.f_voice.get().strip()
        vid = extract_fish_id(raw)
        if vid and vid != raw:                       # pegaron un enlace: se deja solo el ID
            self.f_voice.set(vid)                    # (Tk no vuelve a disparar el trace desde dentro de sí mismo)
            raw = vid
        self._cancel_detect()
        self._detected = None
        if not raw:
            self._tok += 1
            self._say(self.f_status, "")
            return
        self._say(self.f_status, "Detectando la voz…")
        self._after = self.after(700, lambda: self._detect(auto=True))

    def _detect(self, auto=False):
        if not self.winfo_exists():
            return
        vid = extract_fish_id(self.f_voice.get())
        if not vid:
            self._say(self.f_status, "Pega el ID de la voz (32 caracteres) o el enlace de fish.audio.", "warn")
            return
        key = self.f_key.get().strip()
        if not key and auto:
            self._say(self.f_status, "Pon tu API key de Fish para comprobar la voz.", "warn")
            return
        self._tok += 1
        token = self._tok
        self._say(self.f_status, "Consultando Fish Audio…")

        def run():
            try:
                ui(self._got_voice, token, vid, fetch_fish_voice(key, vid), None)
            except Exception as e:
                ui(self._got_voice, token, vid, None, str(e))
        threading.Thread(target=run, daemon=True).start()

    def _got_voice(self, token, vid, info, err):
        if not self.winfo_exists() or token != self._tok:
            return
        if err:
            self._detected = None
            self._say(self.f_status, err, "err")
            return
        self._detected = {"id": vid, "title": info["title"]}
        text, level = describe_voice(info)
        self._say(self.f_status, text, level)

    # ---------- proveedor local ----------
    def _probe_local(self):
        u = urlparse(self.l_url.get().strip())
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        if not host:
            self._say(self.l_status, "Escribe una URL válida, p. ej. http://127.0.0.1:8002/tts", "warn")
            return
        self._say(self.l_status, "Probando…")

        def run():
            try:
                socket.create_connection((host, port), timeout=2.5).close()
                ui(self._say, self.l_status, f"✓ Hay algo escuchando en {host}:{port}.", "ok")
            except OSError:
                ui(self._say, self.l_status, f"✗ No responde nadie en {host}:{port}. ¿Está encendido tu servidor TTS?", "err")
        threading.Thread(target=run, daemon=True).start()

    # ---------- guardar ----------
    def _gain(self, var, name):
        try:
            g = float(var.get().strip().replace(",", "."))
        except ValueError:
            g = None
        if g is None or not 0.1 <= g <= 4.0:
            messagebox.showerror("Ganancia inválida", f"La ganancia de {name} debe ser un número entre 0.1 y 4.", parent=self)
            return None
        return g

    def _save(self):
        fg, lg = self._gain(self.f_gain, "Fish Audio"), self._gain(self.l_gain, "el proveedor local")
        if fg is None or lg is None:
            return
        vid = extract_fish_id(self.f_voice.get()) if self.f_voice.get().strip() else ""
        if any(c.isspace() for c in self.l_voice.get().strip()):
            messagebox.showerror("ID de voz inválido", "El ID de voz local no puede tener espacios.", parent=self)
            return
        first = self.primary.get()
        other = "local" if first == "fish" else "fish"
        provs = [first] + ([other] if self.backup.get() else [])
        up = {
            "VOICE_ENABLED": "true" if self.enabled.get() else "false",
            "VOICE_MODE": self.mode.get(),
            "VOICE_ROUTING": self.routing.get(),
            "VOICE_PROVIDERS": ",".join(provs),
            "VOICE_CLOUD_ALLOWED": "true" if self.cloud.get() else "false",
            "VOICE_FISH_API_KEY": self.f_key.get().strip(),
            "VOICE_FISH_MODEL": self.f_model.get().strip() or FISH_MODELS[0],
            "VOICE_FISH_VOICE_ID": vid,
            "VOICE_FISH_GAIN": f"{fg:g}",
            "VOICE_LOCAL_URL": self.l_url.get().strip(),
            "VOICE_LOCAL_VOICE_ID": self.l_voice.get().strip(),
            "VOICE_LOCAL_MIME": self.l_mime.get().strip() or LOCAL_MIMES[0],
            "VOICE_LOCAL_GAIN": f"{lg:g}",
        }
        try:
            env_update(up)
        except Exception as e:
            messagebox.showerror("No se pudo guardar", str(e), parent=self)
            return
        _clear_web_overrides()
        prefs = load_prefs()
        if self._detected and self._detected["id"] == vid and self._detected["title"]:
            prefs["voice_fish_id"], prefs["voice_fish_name"] = vid, self._detected["title"]
        elif prefs.get("voice_fish_id") != vid:
            prefs.pop("voice_fish_id", None)
            prefs.pop("voice_fish_name", None)
        save_prefs(prefs)
        self.app.toast("Voz guardada en .env")
        if self.on_saved:
            self.on_saved()
        core = self.app.svc["elipse"]
        self.destroy()
        if core.state == "on" and messagebox.askyesno(
            "Reiniciar ELIPSE Core", "ELIPSE Core está corriendo. ¿Reiniciarlo para aplicar los cambios de voz?", parent=self.app
        ):
            core.restart()
