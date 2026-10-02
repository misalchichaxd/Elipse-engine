// docs.js — tarjetas de documentos generados (write_document) + visor Ver/Descargar.
// El backend marca el resultado con "[[DOCS:ruta1|ruta2]]"; chat.js llama a
// Elipse.docs.attachCards(msgDiv, toolEvents) cada vez que renderiza un mensaje.
window.Elipse = window.Elipse || {};
(function(){
  "use strict";
  const Elipse = window.Elipse;
  const DOCS_RE = /\[\[DOCS:([^\]]+)\]\]/;
  const LABEL = { pdf: "PDF", docx: "Word", md: "Markdown" };
  const extOf = (p) => p.split(".").pop().toLowerCase();
  const nameOf = (p) => p.split("/").pop();

  function el(tag, cls, text){
    const n = document.createElement(tag);
    if(cls) n.className = cls;
    if(text != null) n.textContent = text;
    return n;
  }

  // Los endpoints exigen Authorization: por eso fetch -> blob, no <a href> directo.
  // Caché de blobs: abrir/cambiar de pestaña/descargar no vuelve a pedir el archivo.
  const blobCache = new Map();
  async function fetchBlob(path, fresh){
    if(!fresh && blobCache.has(path)) return blobCache.get(path);
    const res = await fetch(
      Elipse.state.baseUrl + "/v1/workspace/download?path=" + encodeURIComponent(path),
      { headers: Elipse.api.authHeaders() }
    );
    if(!res.ok) throw new Error("No se pudo abrir " + nameOf(path) + " (" + res.status + ").");
    const blob = await res.blob();
    blobCache.set(path, blob);
    return blob;
  }

  async function download(path){
    const url = URL.createObjectURL(await fetchBlob(path, true));   // siempre fresco: el archivo pudo regenerarse
    const a = el("a");
    a.href = url;
    a.download = nameOf(path);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  // docx-preview (+ jszip) solo se carga la primera vez que abres un Word.
  let docxLibs = null;
  const loadScript = (src) => new Promise((ok, fail) => {
    const s = el("script");
    s.src = src;
    s.onload = ok;
    s.onerror = () => fail(new Error("No se pudo cargar el visor de Word."));
    document.head.appendChild(s);
  });
  function ensureDocx(){
    if(!docxLibs){
      docxLibs = loadScript("https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js")
        .then(() => loadScript("https://cdn.jsdelivr.net/npm/docx-preview@0.3.3/dist/docx-preview.min.js"))
        .catch((e) => { docxLibs = null; throw e; });   // permite reintentar
    }
    return docxLibs;
  }

  const renderers = {
    async pdf(path, body, urls){
      const url = URL.createObjectURL(await fetchBlob(path));
      urls.push(url);
      const f = el("iframe", "doc-frame");
      f.src = url;
      body.replaceChildren(f);
    },
    async md(path, body){
      const d = el("div", "doc-md");
      d.appendChild(Elipse.renderRichContent(await (await fetchBlob(path)).text()));
      body.replaceChildren(d);
    },
    async docx(path, body){
      await ensureDocx();
      const d = el("div", "doc-docx");
      body.replaceChildren(d);
      await window.docx.renderAsync(await fetchBlob(path), d);
    },
    async text(path, body){
      body.replaceChildren(codeView(await readText(path)));
    },
    async img(path, body, urls){
      const url = URL.createObjectURL(await fetchBlob(path));
      urls.push(url);
      const i = el("img", "doc-img");
      i.src = url;
      i.alt = nameOf(path);
      body.replaceChildren(i);
    },
    async html(path, body){
      const text = await readText(path);
      const wrap = el("div", "doc-split");
      const bar = el("div", "doc-subbar");
      const view = el("div", "doc-subview");
      const bPrev = el("button", "doc-tab active", "Vista previa");
      const bCode = el("button", "doc-tab", "Código");
      bPrev.type = bCode.type = "button";
      const showPrev = () => {
        bPrev.classList.add("active"); bCode.classList.remove("active");
        const f = el("iframe", "doc-frame");
        f.setAttribute("sandbox", "allow-scripts");     // sin allow-same-origin: la página no toca la consola ni la API key
        f.srcdoc = text;
        view.replaceChildren(f);
      };
      const showCode = () => {
        bCode.classList.add("active"); bPrev.classList.remove("active");
        view.replaceChildren(codeView(text));
      };
      bPrev.onclick = showPrev;
      bCode.onclick = showCode;
      bar.append(bPrev, bCode);
      wrap.append(bar, view);
      body.replaceChildren(wrap);
      showPrev();
    },
  };
  renderers.markdown = renderers.md;

  const IMG_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);
  const TEXT_EXT = new Set([
    "py", "java", "js", "jsx", "ts", "tsx", "css", "scss", "json", "txt", "sh", "bash", "sql",
    "c", "h", "cpp", "hpp", "go", "rs", "rb", "php", "yml", "yaml", "xml", "csv", "ini", "toml",
    "kt", "swift", "cs", "vue", "svelte", "env", "log", "dockerfile", "rtf", "",
  ]);
  const MAX_PREVIEW_BYTES = 1.5 * 1024 * 1024;

  function rendererFor(ext){
    if(renderers[ext]) return renderers[ext];
    if(IMG_EXT.has(ext)) return renderers.img;
    if(ext === "html" || ext === "htm") return renderers.html;
    if(TEXT_EXT.has(ext)) return renderers.text;
    return null;
  }
  const canPreview = (ext) => !!rendererFor((ext || "").toLowerCase());

  async function readText(path){
    const blob = await fetchBlob(path);
    if(blob.size > MAX_PREVIEW_BYTES) throw new Error("El archivo es demasiado grande para la vista previa.");
    return blob.text();
  }

  function copyToClipboard(text){
    if(navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return new Promise((ok) => {                        // http en LAN / file://: sin Clipboard API
      const ta = el("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;left:-9999px";
      document.body.appendChild(ta);
      ta.select();
      try{ document.execCommand("copy"); }catch(_){}
      ta.remove();
      ok();
    });
  }

  function codeView(text){
    const wrap = el("div", "doc-text-wrap");
    const copy = el("button", "doc-copy", "Copiar");
    copy.type = "button";
    copy.onclick = () => copyToClipboard(text).then(() => {
      copy.textContent = "Copiado";
      setTimeout(() => { copy.textContent = "Copiar"; }, 1400);
    });
    const pre = el("pre", "doc-text");
    const code = el("code");
    code.textContent = text;
    pre.appendChild(code);
    wrap.append(copy, pre);
    return wrap;
  }

  function openViewer(doc, startPath){
    blobCache.clear();                  // cada apertura baja la versión actual (el archivo pudo regenerarse)
    const urls = [];
    const overlay = el("div", "code-viewer-overlay");
    const panel = el("div", "doc-viewer-panel");
    const head = el("div", "doc-viewer-head");
    const tabs = el("div", "doc-tabs");
    const body = el("div", "doc-viewer-body");
    let current = null;
    let token = 0;                       // descarta renders viejos si cambias de pestaña rápido

    const onKey = (e) => { if(e.key === "Escape") close(); };
    function close(){
      document.removeEventListener("keydown", onKey);
      urls.forEach((u) => URL.revokeObjectURL(u));
      overlay.remove();
    }

    async function show(path){
      current = path;
      const mine = ++token;
      tabs.querySelectorAll(".doc-tab").forEach((t) => t.classList.toggle("active", t.dataset.path === path));
      body.replaceChildren(el("div", "doc-loading", "Cargando…"));
      try{
        const render = rendererFor(extOf(path));
        if(!render) throw new Error("Este formato no tiene vista previa.");
        await render(path, body, urls);
      }catch(e){
        if(mine === token) body.replaceChildren(el("div", "doc-loading", e.message + " Puedes descargarlo con el botón."));
      }
    }

    // Con un solo formato no hace falta la barra de pestañas.
    if(doc.files.length > 1){
      doc.files.forEach((p) => {
        const t = el("button", "doc-tab", LABEL[extOf(p)] || extOf(p));
        t.type = "button";
        t.dataset.path = p;
        t.onclick = () => show(p);
        tabs.appendChild(t);
      });
    }else{
      tabs.appendChild(el("span", "doc-card-title", doc.title));
    }

    const dl = el("button", "btn", "⬇ Descargar");
    dl.type = "button";
    dl.onclick = () => download(current).catch((e) => alert(e.message));
    const x = el("button", "btn-icon", "✕");
    x.type = "button";
    x.title = "Cerrar";
    x.onclick = close;
    const right = el("div");
    right.style.cssText = "display:flex;gap:8px;align-items:center";
    right.append(dl, x);

    head.append(tabs, right);
    panel.append(head, body);
    overlay.appendChild(panel);
    overlay.addEventListener("click", (e) => { if(e.target === overlay) close(); });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    show(startPath || doc.files[0]);
  }

  // Una tarjeta por documento. Si el modelo llamó write_document varias veces
  // con el mismo filename (mode=append), queda la última.
  function collectDocs(toolEvents){
    const byKey = new Map();
    (toolEvents || []).forEach((ev) => {
      if(ev.tool !== "write_document" || !ev.ok) return;
      const m = DOCS_RE.exec(ev.preview || "");
      if(!m) return;
      const files = m[1].split("|").map((s) => s.trim()).filter(Boolean);
      if(!files.length) return;
      const key = files[0].replace(/\.[^.]+$/, "");
      const title = (ev.arguments && ev.arguments.title) || nameOf(key).replace(/_/g, " ");
      byKey.set(key, { title, files });
    });
    return [...byKey.values()];
  }

  // Idempotente y barata: setMsgContent/renderTimelineSlot corren muchas veces
  // durante el streaming, así que solo se reconstruye el DOM si cambió algo.
  function attachCards(div, toolEvents){
    const docs = collectDocs(toolEvents);
    const sig = JSON.stringify(docs);
    const old = div.querySelector(":scope > .doc-cards");
    if(div.__docSig === sig && (old || !docs.length)) return;
    div.__docSig = sig;
    if(old) old.remove();
    if(!docs.length) return;

    const wrap = el("div", "doc-cards");
    docs.forEach((doc) => {
      const card = el("div", "doc-card");
      const info = el("div", "doc-card-info");
      info.append(
        el("div", "doc-card-title", doc.title),
        el("div", "doc-card-sub", doc.files.map((f) => LABEL[extOf(f)] || extOf(f)).join(" · "))
      );
      const view = el("button", "btn", "Ver");
      view.type = "button";
      view.onclick = () => openViewer(doc);
      card.append(info, view);
      doc.files.forEach((p) => {
        const b = el("button", "btn", "⬇ " + (LABEL[extOf(p)] || extOf(p)));
        b.type = "button";
        b.onclick = () => download(p).catch((e) => alert(e.message));
        card.appendChild(b);
      });
      wrap.appendChild(card);
    });

    const body = div.querySelector(":scope > .msg-body");
    const meta = div.querySelector(":scope > .meta");
    if(body) body.after(wrap);
    else if(meta) div.insertBefore(wrap, meta);
    else div.appendChild(wrap);
  }

  Elipse.docs = { attachCards, openViewer, download, canPreview };
})();