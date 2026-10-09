window.Elipse = window.Elipse || {};
(function(){
  "use strict";
  const Elipse = window.Elipse;
  const $ = Elipse.$;

  const MUTE_KEY = "elipse_voice_muted";
  // WAV de silencio: se "reproduce" en el gesto del usuario (clic en Enviar) para que el navegador
  // (sobre todo Safari/iOS) deje sonar el audio que llega después, cuando ya no hay gesto.
  const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=";

  let audio = null, ctx = null, gainNode = null;
  let cfg = null;                 // último /v1/voice/status
  let current = null;             // { btn, onError, blobUrl } mientras suena (o carga) algo
  let muted = false;
  try{ muted = localStorage.getItem(MUTE_KEY) === "1"; }catch(e){}

  // ---------- audio ----------
  function getAudio(){
    if(audio) return audio;
    audio = new Audio();
    audio.crossOrigin = "anonymous";      // necesario para la ganancia (Web Audio) con el audio de la API
    audio.preload = "auto";
    audio.addEventListener("ended", () => {
      finish();
      if(live && live.playing){ live.playing = false; liveNext(live); }   // voz en vivo: siguiente frase
    });
    audio.addEventListener("error", () => {
      if(!audio.getAttribute("src")) return;         // lo provocó stop(): se ignora
      const c = current; finish();
      if(c && c.onError) c.onError();
    });
    return audio;
  }

  function ensureGraph(){
    const a = getAudio();
    if(!ctx){
      try{
        const AC = window.AudioContext || window.webkitAudioContext;
        ctx = new AC();
        const src = ctx.createMediaElementSource(a);
        gainNode = ctx.createGain();
        src.connect(gainNode);
        gainNode.connect(ctx.destination);
      }catch(e){ ctx = null; gainNode = null; }
    }
    if(ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
    return a;
  }

  function setGain(g){
    g = Number(g) > 0 ? Number(g) : 1;
    if(gainNode){ gainNode.gain.value = g; getAudio().volume = 1; }
    else getAudio().volume = Math.min(1, g);          // sin Web Audio: no se puede amplificar, solo bajar
  }

  function unlock(){
    const a = ensureGraph();
    if(current) return;
    try{ a.src = SILENT_WAV; const p = a.play(); if(p && p.catch) p.catch(() => {}); }catch(e){}
  }

  function finish(){
    const c = current; current = null;
    if(c){
      if(c.btn) c.btn.classList.remove("playing");
      if(c.blobUrl) URL.revokeObjectURL(c.blobUrl);
    }
    refreshBtn();
  }

  function halt(){                    // corta solo el audio que suena (no toca la voz en vivo)
    finish();
    if(!audio) return;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }

  function stop(){                    // corta todo: audio actual + frases pendientes de la voz en vivo
    liveCancel();
    halt();
  }

  function start(url, gain, opts){
    opts = opts || {};
    if(opts.keepLive) halt(); else stop();
    const a = ensureGraph();
    setGain(gain);
    current = { btn: opts.btn || null, onError: opts.onError || null, blobUrl: opts.blobUrl || null };
    if(current.btn) current.btn.classList.add("playing");
    refreshBtn();
    a.src = url;
    const p = a.play();
    if(p && p.catch) p.catch((e) => {
      if(e && e.name === "AbortError") return;
      const c = current; finish();
      if(c && c.onError) c.onError(e);
    });
  }

  // ---------- red ----------
  function streamUrl(taskId, force){
    return Elipse.state.baseUrl + "/v1/voice/" + encodeURIComponent(taskId) + "/stream?token="
      + encodeURIComponent(Elipse.state.apiKey || "") + (force ? "&force=1" : "");
  }

  // POST /say no se puede usar como <audio src>: se baja completo y se reproduce desde memoria (sin disco).
  async function sayBlob(text, fallback){
    let res;
    try{
      res = await fetch(Elipse.state.baseUrl + "/v1/voice/say", {
        method: "POST",
        headers: Elipse.api.authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ text: text.slice(0, 20000), fallback: fallback !== false }),
      });
    }catch(e){ throw new Error("No se pudo conectar con ELIPSE."); }
    if(res.status === 204) return null;                // nada hablable en ese texto
    if(!res.ok){
      let d = null; try{ d = await res.json(); }catch(_){}
      let detail = d && d.detail; if(detail && typeof detail !== "string") detail = JSON.stringify(detail);
      throw new Error(detail || ("Error " + res.status + " de la voz."));
    }
    return res.blob();
  }

  function report(msg){
    const b = $("#chatBanner");
    if(!b) return;
    b.textContent = msg; b.classList.add("show");
    setTimeout(() => { if(b.textContent === msg) b.classList.remove("show"); }, 6000);
  }


  // ---------- voz en vivo: habla frase por frase MIENTRAS el texto se escribe ----------
  // En vez de esperar al `final` y pedir todo el audio de golpe, el chat le pasa cada trozo de
  // texto (liveFeed). Aquí se corta en frases, se pide el audio de cada una por adelantado
  // (POST /say, que sigue pasando por el filtro del servidor) y se reproducen en orden.
  const LIVE_FIRST_MIN = 25;        // la 1.ª frase puede ser corta: lo que importa es empezar rápido
  const LIVE_MIN = 70;              // las demás se agrupan hasta ~70 caracteres (menos peticiones, mejor prosodia)
  const LIVE_PREFETCH = 2;          // peticiones de audio simultáneas como máximo
  const SENT_END = /[.!?…]+["')\]]*\s+|\n+/g;

  let live = null;

  function liveAvailable(){
    return !muted && !!cfg && !!cfg.enabled && !!cfg.ready && (cfg.mode === "smart" || cfg.mode === "always");
  }

  function liveBegin(){
    liveCancel();
    if(!liveAvailable()) return false;
    warmUp();
    live = {
      buf: "", tail: "", fence: false,            // texto pendiente / backticks partidos / dentro de ``` (no se habla)
      items: [], n: 0, spoken: 0,
      cap: 4 * ((cfg && cfg.max_chars) || 400),   // tope de lo que se habla en vivo (4× max_chars; antes 1×: cortaba a media respuesta)
      inflight: 0, playing: false, closed: false, failed: false,
    };
    return true;
  }

  // Abre la conexión con el proveedor mientras el modelo todavía está pensando (best-effort, sin crédito).
  function warmUp(){
    try{
      fetch(Elipse.state.baseUrl + "/v1/voice/warm", { method: "POST", headers: Elipse.api.authHeaders() }).catch(() => {});
    }catch(_){}
  }

  function liveCancel(){
    const s = live; live = null;
    if(s) s.items.forEach((it) => { it.state = "skip"; it.blob = null; });
  }

  function liveEmit(s, text){
    text = (text || "").trim();
    if(!text || s.spoken >= s.cap) return;
    s.spoken += text.length;
    s.n++;
    s.items.push({ text: text.slice(0, 600), state: "wait", blob: null });
    livePump(s);
  }

  // Saca de s.buf las frases completas (>= mínimo). force = vaciarlo todo (fin de texto o entrada a un bloque de código).
  function liveCut(s, force){
    let last = 0, m;
    SENT_END.lastIndex = 0;
    while((m = SENT_END.exec(s.buf))){
      const end = m.index + m[0].length;
      const min = s.n === 0 ? LIVE_FIRST_MIN : LIVE_MIN;
      if(s.buf.slice(last, end).trim().length >= min){
        liveEmit(s, s.buf.slice(last, end));
        last = end;
      }
    }
    s.buf = s.buf.slice(last);
    if(force && s.buf.trim()){ liveEmit(s, s.buf); s.buf = ""; }
  }

  function liveFeed(text){
    const s = live;
    if(!s || s.closed || !text) return;
    let t = s.tail + text; s.tail = "";
    const m = t.match(/`{1,2}$/);                       // ``` partido entre dos chunks
    if(m){ s.tail = m[0]; t = t.slice(0, -m[0].length); }
    for(;;){
      const i = t.indexOf("```");
      const seg = i < 0 ? t : t.slice(0, i);
      if(!s.fence) s.buf += seg;                        // dentro de un bloque de código no se habla
      if(i < 0) break;
      s.fence = !s.fence;
      if(s.fence) liveCut(s, true);                     // "Aquí va el código:" se dice antes de callar
      t = t.slice(i + 3);
    }
    liveCut(s, false);
  }

  function livePump(s){
    if(live !== s) return;
    for(const it of s.items){
      if(s.inflight >= LIVE_PREFETCH) break;
      if(it.state !== "wait") continue;
      it.state = "fetch"; s.inflight++;
      sayBlob(it.text, false)
        .then((blob) => { it.blob = blob; it.state = blob ? "ready" : "skip"; })
        .catch((e) => {
          it.state = "skip";
          if(!s.failed){ s.failed = true; report(e.message || "No se pudo generar la voz."); }
        })
        .finally(() => { s.inflight--; if(live !== s) return; livePump(s); liveNext(s); });
    }
  }

  function liveNext(s){
    if(live !== s || s.playing) return;
    while(s.items.length && s.items[0].state === "skip") s.items.shift();
    const it = s.items[0];
    if(!it){                                            // cola vacía
      if(s.closed && s.inflight === 0){ live = null; refreshBtn(); }
      return;
    }
    if(it.state !== "ready") return;                    // se sigue bajando: livePump volverá a llamar aquí
    s.items.shift();
    s.playing = true;
    const u = URL.createObjectURL(it.blob);
    const next = () => { if(live === s){ s.playing = false; liveNext(s); } };
    start(u, cfg ? cfg.gain : 1, {
      keepLive: true, blobUrl: u,
      onError: (e) => {
        if(e && e.name === "NotAllowedError"){          // el navegador bloqueó el audio: no insistir
          report("El navegador bloqueó el audio. Pulsa 🔊 en el mensaje para escucharlo.");
          liveCancel();
        }else{
          if(!s.failed){ s.failed = true; report("No se pudo reproducir parte de la voz."); }
          next();
        }
      },
    });
  }

  // Llega `final`: se vacía lo que quede. true = la voz en vivo se encarga (no hacer autoplay del mensaje entero).
  function liveFinish(){
    const s = live;
    if(!s) return false;
    s.closed = true;
    if(!s.fence) liveCut(s, true);
    const handled = s.n > 0;
    if(handled) liveNext(s);
    else live = null;                                   // no dijo nada (p. ej. solo código): decide el servidor
    return handled;
  }

  // ---------- API pública para el chat ----------
  // Al llegar `final` con bloque voice: reproduce el audio ya filtrado por el servidor.
  function autoplay(taskId, voiceBlock){
    if(muted) return;
    start(streamUrl(taskId, false), voiceBlock && voiceBlock.gain, {
      onError: () => report("No se pudo reproducir la voz. Revisa la sección Voz."),
    });
  }

  // Botón 🔊 por mensaje: gana sobre interruptor y modo (force=1). Si la tarea ya expiró, cae a /say.
  function speakMessage(btn, taskId, reply){
    unlock();
    if(current && current.btn === btn){ stop(); return; }
    const gain = cfg ? cfg.gain : 1;
    const viaSay = async () => {
      try{
        const blob = await sayBlob(reply);
        const u = URL.createObjectURL(blob);
        start(u, gain, { btn, blobUrl: u, onError: () => report("No se pudo reproducir el audio.") });
      }catch(e){ btn.classList.remove("playing"); report(e.message); }
    };
    start(streamUrl(taskId, true), gain, { btn, onError: viaSay });
  }

  function attachButton(msgDiv, taskId, reply){
    const old = msgDiv.querySelector(".voice-msg-btn");
    if(old) old.remove();
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "voice-msg-btn";
    btn.title = "Escuchar este mensaje";
    btn.textContent = "🔊";
    btn.addEventListener("click", () => speakMessage(btn, taskId, reply));
    msgDiv.appendChild(btn);
  }

  // Botón del compositor: ⏹ si está sonando; si no, silenciar / activar en este dispositivo.
  function refreshBtn(){
    const b = $("#voiceBtn");
    if(!b) return;
    b.textContent = current ? "⏹" : (muted ? "🔇" : "🔊");
    b.title = current ? "Parar la voz" : (muted ? "Voz silenciada en este dispositivo (clic para activar)" : "Voz activa (clic para silenciar)");
    b.classList.toggle("muted", muted && !current);
    const m = $("#voiceMuted"); if(m) m.checked = muted;
  }

  function setMuted(on){
    muted = !!on;
    try{ localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); }catch(e){}
    if(muted) stop();
    refreshBtn();
  }

  // ---------- vista "Voz" ----------
  function note(msg, bad){
    const n = $("#voiceNote");
    if(!n) return;
    n.textContent = msg; n.classList.toggle("bad", !!bad);
  }

  function fillForm(st){
    cfg = st;
    $("#voiceEnabled").checked = !!st.enabled;
    $("#voiceMode").value = st.mode;
    $("#voiceRouting").value = st.routing;
    $("#voiceProvider").value = st.provider || "fish";
    $("#voiceCloud").checked = !!st.cloud_allowed;
    st.providers.forEach((p) => {
      const key = p.name === "fish" ? "Fish" : "Local";
      $("#voice" + key + "Id").value = p.voice_id || "";
      $("#voice" + key + "Gain").value = p.gain;
    });
    $("#voiceStatusBox").innerHTML = st.providers.map((p) => {
      const estado = !p.available ? "sin configurar"
        : p.in_cooldown ? "en pausa " + p.cooldown_left_s + " s" : "disponible";
      const lat = p.first_chunk_ms != null ? " · primer audio " + p.first_chunk_ms + " ms" : "";
      const tot = p.total_ms != null ? " · total " + p.total_ms + " ms" : "";
      const err = p.last_error ? '<div class="voice-err">' + p.last_error.replace(/</g, "&lt;") + "</div>" : "";
      return "<div><b>" + p.name + "</b> (" + (p.is_cloud ? "nube" : "local") + ") — " + estado + lat + tot
        + " · " + p.ok + " ok / " + p.errors + " errores" + err + "</div>";
    }).join("");
    refreshBtn();
  }

  async function load(){
    try{
      const st = await Elipse.api.apiFetch("/v1/voice/status", { headers: Elipse.api.authHeaders() });
      fillForm(st);
      note("");
    }catch(e){ note(e.message, true); }
  }

  async function save(){
    const patch = {
      enabled: $("#voiceEnabled").checked,
      mode: $("#voiceMode").value,
      routing: $("#voiceRouting").value,
      provider: $("#voiceProvider").value,
      cloud_allowed: $("#voiceCloud").checked,
      providers: {
        fish:  { voice_id: $("#voiceFishId").value.trim(),  gain: parseFloat($("#voiceFishGain").value) || 1 },
        local: { voice_id: $("#voiceLocalId").value.trim(), gain: parseFloat($("#voiceLocalGain").value) || 1 },
      },
    };
    try{
      const st = await Elipse.api.apiFetch("/v1/voice/settings", {
        method: "POST",
        headers: Elipse.api.authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(patch),
      });
      fillForm(st);
      note("Guardado.");
    }catch(e){ note(e.message, true); }
  }

  async function test(){
    unlock();
    note("Pidiendo audio de prueba...");
    try{
      const blob = await sayBlob("Hola, soy Elipse. Así suena mi voz.");
      const u = URL.createObjectURL(blob);
      start(u, cfg ? cfg.gain : 1, { blobUrl: u, onError: () => note("No se pudo reproducir el audio.", true) });
      note("");
      load();                                   // refresca latencias y estado de los proveedores
    }catch(e){ note(e.message, true); load(); }
  }

  function init(){
    const b = $("#voiceBtn");
    if(b) b.addEventListener("click", () => { unlock(); current ? stop() : setMuted(!muted); });
    const save_ = $("#voiceSave"); if(save_) save_.addEventListener("click", save);
    const test_ = $("#voiceTest"); if(test_) test_.addEventListener("click", test);
    const stop_ = $("#voiceStop"); if(stop_) stop_.addEventListener("click", stop);
    const mu = $("#voiceMuted"); if(mu) mu.addEventListener("change", () => setMuted(mu.checked));
    refreshBtn();
    load();
  }

  Elipse.voice = { init, load, unlock, stop, autoplay, attachButton, setMuted, liveBegin, liveFeed, liveFinish };
})();
