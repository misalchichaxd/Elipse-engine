window.Elipse = window.Elipse || {};
(function(){
  "use strict";
  const Elipse = window.Elipse;
  const $ = Elipse.$;

  function showBanner(el, message){
    el.textContent = message;
    el.classList.toggle("show", !!message);
  }

  // Espejo de ALLOWED_ATTACHMENT_EXTENSIONS en main.py. El backend vuelve a validar
  // esto igual (nunca confiar solo en el cliente), pero filtrar acá evita subir y
  // esperar una respuesta solo para enterarse de que el formato no está habilitado.
  const ALLOWED_ATTACHMENT_EXTENSIONS = new Set([
    "py", "java", "js", "jsx", "ts", "tsx", "html", "htm", "css", "scss",
    "json", "md", "markdown", "txt", "sh", "bash", "sql", "c", "h", "cpp",
    "hpp", "go", "rs", "rb", "php", "yml", "yaml", "xml", "csv", "ini",
    "toml", "kt", "swift", "cs", "vue", "svelte", "env", "log", "dockerfile",
  ]);
  const MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024; // 2MB por archivo, generoso para código fuente

  // --- Imágenes (las mira el modelo de visión del backend; ver app/core/vision.py) ---
  const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp", "gif", "bmp"]);
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;        // espejo de VISION_MAX_UPLOAD_MB
  const MAX_IMAGES_PER_MESSAGE = 3;                // el servidor describe como máximo 3 por mensaje
  const IMAGE_MAX_SIDE = 1600;                     // se reduce antes de subir (el servidor la baja a 768 igual)
  const IMAGE_RESIZE_ABOVE_BYTES = 1024 * 1024;    // por debajo de 1 MB y de 1600 px se sube tal cual

  // [{filename, content, encoding, isImage?, preview?}]. `preview` es solo para la miniatura: no se envía.
  let pendingAttachments = [];

  let chatHasMessages = false;

  function clearEmptyState(log){
    if(!chatHasMessages){ log.innerHTML = ""; chatHasMessages = true; }
  }

  function escapeHtml(s){
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function langLabel(lang){
    if(!lang) return "texto";
    const map = {
      js: "JavaScript", javascript: "JavaScript", ts: "TypeScript", typescript: "TypeScript",
      py: "Python", python: "Python", html: "HTML", css: "CSS", json: "JSON",
      md: "Markdown", markdown: "Markdown", sh: "Shell", bash: "Shell", shell: "Shell",
      sql: "SQL", rs: "Rust", rust: "Rust", go: "Go", java: "Java", c: "C", cpp: "C++",
      txt: "texto", text: "texto", plain: "texto"
    };
    return map[lang.toLowerCase()] || lang;
  }

  function extFor(lang){
    if(!lang) return "txt";
    const map = {
      javascript: "js", js: "js", typescript: "ts", ts: "ts",
      python: "py", py: "py", markdown: "md", md: "md",
      shell: "sh", bash: "sh", sh: "sh", plaintext: "txt", text: "txt", plain: "txt"
    };
    return map[lang.toLowerCase()] || lang.toLowerCase().replace(/[^a-z0-9]/g, "") || "txt";
  }

  function copyText(text, btn){
    const done = () => {
      if(!btn) return;
      const prev = btn.textContent;
      btn.textContent = "Copiado";
      btn.classList.add("copied");
      setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1400);
    };
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    }else{
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done){
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try{ document.execCommand("copy"); done(); }catch(_){}
    document.body.removeChild(ta);
  }

  function downloadText(text, filename){
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "archivo.txt";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---------- Inline markdown (negrita, cursiva, código, enlaces) ----------
  function inlineMd(text){
    let s = escapeHtml(text);
    // código inline `...`
    s = s.replace(/`([^`]+)`/g, "<code class=\"md-inline-code\">$1</code>");
    // negrita **...** o __...__
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    // cursiva *...* o _..._ (evitar overlap con negrita ya procesada)
    s = s.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^_])_([^_]+)_(?!_)/g, "$1<em>$2</em>");
    // enlaces [texto](url)
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // <br> literales que a veces mete el modelo
    s = s.replace(/&lt;br\s*\/?&gt;/gi, "<br>");
    return s;
  }

  function isTableSep(line){
    // |---|:---|---:| etc.
    return /^\|?[\s:|-]+\|[\s:|-]*\|?$/.test(line.trim()) && /---/.test(line);
  }

  function isTableRow(line){
    const t = line.trim();
    return t.includes("|") && !isTableSep(t);
  }

  function splitCells(line){
    let s = line.trim();
    if(s.startsWith("|")) s = s.slice(1);
    if(s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  }

  function buildTable(rows){
    // rows[0] = header, rows[1] = separator (ignorado), rows[2..] = body
    const wrap = document.createElement("div");
    wrap.className = "md-table-wrap";
    const table = document.createElement("table");
    table.className = "md-table";

    if(rows.length){
      const thead = document.createElement("thead");
      const tr = document.createElement("tr");
      splitCells(rows[0]).forEach((cell) => {
        const th = document.createElement("th");
        th.innerHTML = inlineMd(cell);
        tr.appendChild(th);
      });
      thead.appendChild(tr);
      table.appendChild(thead);
    }

    const bodyRows = rows.slice(1).filter((r) => !isTableSep(r));
    if(bodyRows.length){
      const tbody = document.createElement("tbody");
      bodyRows.forEach((row) => {
        const tr = document.createElement("tr");
        splitCells(row).forEach((cell) => {
          const td = document.createElement("td");
          td.innerHTML = inlineMd(cell);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
    }

    wrap.appendChild(table);
    return wrap;
  }

  /** Renderiza un bloque de markdown (sin code fences) a nodos DOM. */
  function renderMarkdownBlock(text){
    const frag = document.createDocumentFragment();
    if(!text || !text.trim()) return frag;

    const lines = text.replace(/\r\n/g, "\n").split("\n");
    let i = 0;
    let paraBuf = [];

    function flushPara(){
      if(!paraBuf.length) return;
      const raw = paraBuf.join("\n").trim();
      paraBuf = [];
      if(!raw) return;
      const p = document.createElement("div");
      p.className = "msg-text";
      p.innerHTML = inlineMd(raw);
      frag.appendChild(p);
    }

    while(i < lines.length){
      const line = lines[i];
      const trimmed = line.trim();

      // línea en blanco → cortar párrafo
      if(!trimmed){
        flushPara();
        i++;
        continue;
      }

      // tabla markdown
      if(isTableRow(line) && i + 1 < lines.length && isTableSep(lines[i + 1])){
        flushPara();
        const tableLines = [line, lines[i + 1]];
        i += 2;
        while(i < lines.length && isTableRow(lines[i])){
          tableLines.push(lines[i]);
          i++;
        }
        frag.appendChild(buildTable(tableLines));
        continue;
      }

      // encabezados # ## ###
      const hMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if(hMatch){
        flushPara();
        const level = hMatch[1].length;
        const el = document.createElement("h" + Math.min(level + 1, 5)); // h2..h5
        el.className = "md-h md-h" + level;
        el.innerHTML = inlineMd(hMatch[2]);
        frag.appendChild(el);
        i++;
        continue;
      }

      // separador ---
      if(/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)){
        flushPara();
        const hr = document.createElement("hr");
        hr.className = "md-hr";
        frag.appendChild(hr);
        i++;
        continue;
      }

      // blockquote >
      if(trimmed.startsWith(">")){
        flushPara();
        const quoteLines = [];
        while(i < lines.length && lines[i].trim().startsWith(">")){
          quoteLines.push(lines[i].trim().replace(/^>\s?/, ""));
          i++;
        }
        const bq = document.createElement("blockquote");
        bq.className = "md-quote";
        bq.innerHTML = inlineMd(quoteLines.join("\n"));
        frag.appendChild(bq);
        continue;
      }

      // lista - o * o 1.
      if(/^[-*]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)){
        flushPara();
        const ordered = /^\d+\.\s+/.test(trimmed);
        const list = document.createElement(ordered ? "ol" : "ul");
        list.className = "md-list";
        while(i < lines.length){
          const t = lines[i].trim();
          const itemMatch = ordered ? t.match(/^\d+\.\s+(.+)$/) : t.match(/^[-*]\s+(.+)$/);
          if(!itemMatch) break;
          const li = document.createElement("li");
          li.innerHTML = inlineMd(itemMatch[1]);
          list.appendChild(li);
          i++;
        }
        frag.appendChild(list);
        continue;
      }

      // párrafo normal
      paraBuf.push(line);
      i++;
    }
    flushPara();
    return frag;
  }

  //  // Heurística: ¿este texto "normal" en realidad es código/markup que el
  // modelo dejó de envolver en fences a mitad de la respuesta? Pasa seguido
  // con el modelo local en generaciones largas.
  function looksLikeCode(text){
    const t = (text || "").trim();
    if(!t) return false;
    if(t.startsWith("<")) return true;                            // arranca con una etiqueta HTML/XML
    if(/^[a-zA-Z0-9_.#*\-\s,]+\{[\s\S]*\}/m.test(t)) return true;  // regla CSS con llaves
    const lines = t.split("\n").filter((l) => l.trim());
    if(!lines.length) return false;
    const codeLike = lines.filter((l) => /[;{}>]\s*$/.test(l.trim())).length;
    return codeLike / lines.length > 0.4; // más del 40% de las líneas terminan como código
  }

  // Lista cerrada de lenguajes reconocidos como "etiqueta" en la primera línea
  // de un bloque (venga de una fence real o de un tramo detectado por
  // looksLikeCode) — evita confundir una línea de código cualquiera con una
  // etiqueta de lenguaje.
  const KNOWN_LANGS = new Set([
    "html","css","js","javascript","ts","typescript","py","python","json",
    "md","markdown","sh","bash","shell","sql","c","cpp","h","hpp","java",
    "go","rust","rs","kt","kotlin","php","xml","yaml","yml","txt","text",
  ]);

  function extractLangAndCode(raw){
    const seg = (raw || "").replace(/^\n+/, "");
    const nl = seg.indexOf("\n");
    const firstLine = (nl === -1 ? seg.trim() : seg.slice(0, nl).trim()).toLowerCase();
    if(firstLine && KNOWN_LANGS.has(firstLine) && nl !== -1){
      return { lang: firstLine, code: seg.slice(nl + 1).replace(/\n$/, "") };
    }
    return { lang: seg.trim().startsWith("<") ? "html" : "", code: seg.trim() };
  }

  /** Parsea texto con fences ``` y markdown del resto (tolera fences sin cerrar,
   * fences de más, tramos de código sin fence, y bloques de código pegados
   * uno al lado del otro que en realidad son el mismo archivo partido). */
  function renderRichContent(text){
    const frag = document.createDocumentFragment();
    if(text == null) text = "";
    text = String(text);

    const segments = text.split("```");
    const parts = []; // {type:"text", text} | {type:"code", lang, code}

    for(let i = 0; i < segments.length; i++){
      const seg = segments[i];
      if(i % 2 === 0){
        if(looksLikeCode(seg)){
          const { lang, code } = extractLangAndCode(seg);
          if(code.trim()) parts.push({ type: "code", lang, code });
        }else if(seg.trim()){
          parts.push({ type: "text", text: seg });
        }
      }else{
        const { lang, code } = extractLangAndCode(seg);
        if(code.trim() || lang) parts.push({ type: "code", lang, code });
      }
    }

    // El modelo local suele "cerrar" un bloque de código y abrir uno nuevo
    // para seguir el mismo archivo, en vez de continuar adentro del primero.
    // Dos bloques de código pegados sin texto real en el medio son, en la
    // práctica, siempre el mismo archivo partido en dos: los unimos.
    const merged = [];
    for(const part of parts){
      const prev = merged[merged.length - 1];
      if(part.type === "code" && prev && prev.type === "code"){
        prev.code = prev.code.replace(/\n+$/, "") + "\n" + part.code.replace(/^\n+/, "");
        prev.lang = prev.lang || part.lang;
        continue;
      }
      merged.push(Object.assign({}, part));
    }

    for(const part of merged){
      if(part.type === "code") frag.appendChild(buildCodeBlock(part.lang, part.code));
      else frag.appendChild(renderMarkdownBlock(part.text));
    }

    if(!frag.childNodes.length){
      frag.appendChild(renderMarkdownBlock(text));
    }
    return frag;
  }

  function buildCodeBlock(lang, code){
    const wrap = document.createElement("div");
    wrap.className = "code-block";

    const header = document.createElement("div");
    header.className = "code-header";

    const label = document.createElement("span");
    label.className = "code-lang";
    label.textContent = langLabel(lang);
    header.appendChild(label);

    const actions = document.createElement("div");
    actions.className = "code-actions";

    const btnCopy = document.createElement("button");
    btnCopy.type = "button";
    btnCopy.className = "code-btn";
    btnCopy.textContent = "Copiar";
    btnCopy.title = "Copiar al portapapeles";
    btnCopy.addEventListener("click", () => copyText(code, btnCopy));

    const btnDl = document.createElement("button");
    btnDl.type = "button";
    btnDl.className = "code-btn";
    btnDl.textContent = "Descargar";
    btnDl.title = "Descargar archivo";
    btnDl.addEventListener("click", () => {
      downloadText(code, "elipse." + extFor(lang));
    });

    const btnView = document.createElement("button");
    btnView.type = "button";
    btnView.className = "code-btn";
    btnView.textContent = "Ampliar";
    btnView.title = "Ver en pantalla completa";
    btnView.addEventListener("click", () => openCodeViewer(lang, code));

    actions.appendChild(btnCopy);
    actions.appendChild(btnDl);
    actions.appendChild(btnView);
    header.appendChild(actions);

    const pre = document.createElement("pre");
    const codeEl = document.createElement("code");
    codeEl.textContent = code;
    pre.appendChild(codeEl);

    wrap.appendChild(header);
    wrap.appendChild(pre);
    return wrap;
  }

  function openCodeViewer(lang, code){
    const existing = document.getElementById("codeViewerOverlay");
    if(existing) existing.remove();

    const overlay = document.createElement("div");
    overlay.id = "codeViewerOverlay";
    overlay.className = "code-viewer-overlay";

    const panel = document.createElement("div");
    panel.className = "code-viewer-panel";

    const head = document.createElement("div");
    head.className = "code-viewer-head";
    const title = document.createElement("span");
    title.textContent = langLabel(lang);
    head.appendChild(title);

    const headActions = document.createElement("div");
    headActions.className = "code-actions";

    const btnCopy = document.createElement("button");
    btnCopy.type = "button";
    btnCopy.className = "code-btn";
    btnCopy.textContent = "Copiar";
    btnCopy.addEventListener("click", () => copyText(code, btnCopy));

    const btnDl = document.createElement("button");
    btnDl.type = "button";
    btnDl.className = "code-btn";
    btnDl.textContent = "Descargar";
    btnDl.addEventListener("click", () => downloadText(code, "elipse." + extFor(lang)));

    const btnClose = document.createElement("button");
    btnClose.type = "button";
    btnClose.className = "code-btn code-btn-close";
    btnClose.textContent = "Cerrar";
    btnClose.addEventListener("click", () => overlay.remove());

    headActions.appendChild(btnCopy);
    headActions.appendChild(btnDl);
    headActions.appendChild(btnClose);
    head.appendChild(headActions);

    const body = document.createElement("pre");
    body.className = "code-viewer-body";
    const codeEl = document.createElement("code");
    codeEl.textContent = code;
    body.appendChild(codeEl);

    panel.appendChild(head);
    panel.appendChild(body);
    overlay.appendChild(panel);
    overlay.addEventListener("click", (e) => { if(e.target === overlay) overlay.remove(); });
    document.addEventListener("keydown", function onEsc(e){
      if(e.key === "Escape"){ overlay.remove(); document.removeEventListener("keydown", onEsc); }
    });
    document.body.appendChild(overlay);
  }

  // ---------- Timeline de herramientas: qué llamó el agente y con qué resultado ----------
  // Alimentado por los eventos "verificacion" que manda el backend por WebSocket
  // (uno por cada tool que corrió, con sus argumentos, si salió bien, y una
  // muestra del resultado crudo — ver core/core.py). Es la prueba visible de que
  // el agente realmente ejecutó lo que dice: no un resumen que el modelo podría
  // inventar, sino los datos que mandó el propio backend al ejecutar cada tool.
  const TOOL_LABELS = {
    research_topic: "Investigó en internet",
    write_code: "Generó código (modelo especializado)",
    write_file: "Escribió un archivo",
    read_file: "Leyó un archivo",
    run_python: "Ejecutó Python",
    search_web: "Buscó en la web",
    remember: "Guardó en memoria",
    recall: "Consultó la memoria",
    list_files: "Listó archivos",
    calculate: "Calculó",
    get_current_datetime: "Consultó la fecha/hora",
    write_document: "Generó un documento",
    read_url: "Leyó una página",
    search_images: "Buscó imágenes",
  };

  function toolLabel(name){
    return TOOL_LABELS[name] || ("Llamó a " + name);
  }

  function formatArgs(args){
    if(!args || typeof args !== "object" || !Object.keys(args).length) return "";
    try{ return JSON.stringify(args, null, 2); }catch(_){ return String(args); }
  }

  /** Construye el <details> colapsable con el resumen + el detalle por paso.
   * Devuelve null si no hay eventos todavía (nada que mostrar). */
  function buildToolTimeline(events, opts){
    if(!events || !events.length) return null;
    const stillRunning = !!(opts && opts.stillRunning);

    const details = document.createElement("details");
    details.className = "tool-timeline";

    const summary = document.createElement("summary");

    const label = document.createElement("span");
    label.className = "tool-timeline-label";
    label.textContent = events.length === 1
      ? toolLabel(events[0].tool)
      : "Usó " + events.length + " herramienta" + (events.length === 1 ? "" : "s");
    summary.appendChild(label);

    const okCount = events.filter((e) => e.ok).length;
    const allOk = okCount === events.length;
    const badge = document.createElement("span");
    badge.className = "tool-timeline-badge " + (stillRunning ? "live" : (allOk ? "" : "warn"));
    badge.textContent = stillRunning ? "en curso…" : (allOk ? "✓ " + events.length : okCount + "/" + events.length);
    summary.appendChild(badge);

    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "▾";
    summary.appendChild(chevron);

    details.appendChild(summary);

    const body = document.createElement("div");
    body.className = "tool-timeline-body";

    events.forEach((ev, idx) => {
      const step = document.createElement("div");
      step.className = "tool-step " + (ev.ok ? "ok" : "fail");

      const head = document.createElement("div");
      head.className = "tool-step-head";
      const icon = document.createElement("span");
      icon.className = "tool-step-icon";
      icon.textContent = ev.ok ? "✓" : "✗";
      head.appendChild(icon);
      const name = document.createElement("span");
      name.className = "tool-step-name";
      name.textContent = (idx + 1) + ". " + toolLabel(ev.tool);
      head.appendChild(name);
      const idSpan = document.createElement("span");
      idSpan.className = "tool-step-id";
      idSpan.textContent = ev.tool;
      head.appendChild(idSpan);
      step.appendChild(head);

      const argsText = formatArgs(ev.arguments);
      if(argsText){
        const argsLabel = document.createElement("div");
        argsLabel.className = "tool-step-label";
        argsLabel.textContent = "Argumentos enviados";
        step.appendChild(argsLabel);
        const args = document.createElement("pre");
        args.className = "tool-step-args";
        args.textContent = argsText;
        step.appendChild(args);
      }

      if(ev.preview){
        const prevLabel = document.createElement("div");
        prevLabel.className = "tool-step-label";
        prevLabel.textContent = "Resultado devuelto por la tool";
        step.appendChild(prevLabel);
        const prev = document.createElement("pre");
        prev.className = "tool-step-preview";
        prev.textContent = ev.preview;
        step.appendChild(prev);
      }

      if(ev.detalle){
        const verdict = document.createElement("div");
        verdict.className = "tool-step-verdict";
        verdict.textContent = ev.detalle;
        step.appendChild(verdict);
      }

      body.appendChild(step);
    });

    details.appendChild(body);
    return details;
  }

  /** Actualiza (o crea) el slot de timeline dentro de un msgDiv, preservando
   * si el usuario ya lo tenía abierto mientras seguía llegando texto. */
  function renderTimelineSlot(msgDiv, toolEvents, opts){
    let slot = msgDiv.querySelector(".msg-timeline");
    if(!slot){
      slot = document.createElement("div");
      slot.className = "msg-timeline";
      msgDiv.insertBefore(slot, msgDiv.firstChild);
    }
    const prevOpen = slot.querySelector("details.tool-timeline");
    const wasOpen = prevOpen ? prevOpen.open : false;
    slot.innerHTML = "";
    const tl = buildToolTimeline(toolEvents, opts);
    if(tl){
      if(wasOpen) tl.open = true;
      slot.appendChild(tl);
    }
    if(Elipse.docs) Elipse.docs.attachCards(msgDiv, toolEvents);
    return slot;
  }

  function appendMsg(log, role, text, meta, toolEvents){
    clearEmptyState(log);
    const div = document.createElement("div");
    div.className = "msg " + role;

    if(role === "assistant" && toolEvents && toolEvents.length){
      const slot = document.createElement("div");
      slot.className = "msg-timeline";
      const tl = buildToolTimeline(toolEvents);
      if(tl) slot.appendChild(tl);
      div.appendChild(slot);
    }

    const body = document.createElement("div");
    body.className = "msg-body";
    if(role === "assistant" && text){
      body.appendChild(renderRichContent(text));
    }else{
      const t = document.createElement("div");
      t.className = "msg-text";
      t.textContent = text || "";
      body.appendChild(t);
    }
    div.appendChild(body);

    if(meta){
      const m = document.createElement("span");
      m.className = "meta";
      m.textContent = meta;
      div.appendChild(m);
    }
    if(role === "assistant" && Elipse.docs) Elipse.docs.attachCards(div, toolEvents);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function appendPendingMsg(log, text){
    clearEmptyState(log);
    const div = document.createElement("div");
    div.className = "msg assistant";
    const body = document.createElement("div");
    body.className = "msg-body";
    const t = document.createElement("div");
    t.className = "msg-text";
    t.innerHTML = '<span class="spinner"></span>' + escapeHtml(text || "pensando...");
    body.appendChild(t);
    div.appendChild(body);
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function setMsgContent(div, text, meta, toolEvents, opts){
    toolEvents = toolEvents || [];
    div.__toolEvents = toolEvents; // para que continueGeneration pueda retomarlos
    renderTimelineSlot(div, toolEvents, opts);

    let body = div.querySelector(".msg-body");
    if(!body){
      body = document.createElement("div");
      body.className = "msg-body";
      div.appendChild(body);
    }
    body.innerHTML = "";
    body.appendChild(renderRichContent(text || ""));

    const oldMeta = div.querySelector(".meta");
    if(oldMeta) oldMeta.remove();
    if(meta){
      const m = document.createElement("span");
      m.className = "meta";
      m.textContent = meta;
      div.appendChild(m);
    }
  }

  function buildConfirmCard(pendingList, onDone){
    const card = document.createElement("div");
    card.className = "confirm-card";

    const title = document.createElement("div");
    title.className = "confirm-title";
    title.textContent = pendingList.length === 1
      ? "Se necesita tu confirmación"
      : "Se necesitan " + pendingList.length + " confirmaciones";
    card.appendChild(title);

    pendingList.forEach((p) => {
      const item = document.createElement("div");
      item.className = "confirm-item";
      item.dataset.actionId = p.id;

      const detail = document.createElement("div");
      detail.className = "confirm-detail";
      const tool = document.createElement("span");
      tool.className = "confirm-tool";
      tool.textContent = p.tool || "acción";
      detail.appendChild(tool);
      const desc = document.createElement("span");
      desc.className = "confirm-desc";
      desc.textContent = p.detail || ("id " + p.id);
      detail.appendChild(desc);
      item.appendChild(detail);

      const actions = document.createElement("div");
      actions.className = "confirm-actions";

      const btnOk = document.createElement("button");
      btnOk.type = "button";
      btnOk.className = "confirm-btn approve";
      btnOk.textContent = "Aprobar";
      btnOk.addEventListener("click", () => resolveOne(p.id, true, item, card, onDone));

      const btnNo = document.createElement("button");
      btnNo.type = "button";
      btnNo.className = "confirm-btn reject";
      btnNo.textContent = "Rechazar";
      btnNo.addEventListener("click", () => resolveOne(p.id, false, item, card, onDone));

      actions.appendChild(btnOk);
      actions.appendChild(btnNo);
      item.appendChild(actions);
      card.appendChild(item);
    });

    return card;
  }

  async function resolveOne(actionId, approved, itemEl, cardEl, onDone){
    const btns = itemEl.querySelectorAll("button");
    btns.forEach((b) => { b.disabled = true; });
    itemEl.classList.add("resolving");

    try{
      const res = await Elipse.api.apiFetch("/v1/confirm-action/" + actionId, {
        method: "POST",
        headers: Elipse.api.authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ approved: approved }),
      });

      itemEl.classList.remove("resolving");
      itemEl.classList.add(approved ? "approved" : "rejected");
      const status = document.createElement("div");
      status.className = "confirm-status";
      if(approved){
        status.textContent = "Aprobada" + (res && res.result ? " · " + String(res.result).slice(0, 120) : "");
      }else{
        status.textContent = "Rechazada — no se ejecutó nada";
      }
      const actions = itemEl.querySelector(".confirm-actions");
      if(actions) actions.replaceWith(status);

      const still = cardEl.querySelectorAll(".confirm-item:not(.approved):not(.rejected)");
      if(still.length === 0 && typeof onDone === "function"){
        onDone(approved);
      }
    }catch(e){
      itemEl.classList.remove("resolving");
      btns.forEach((b) => { b.disabled = false; });
      const err = document.createElement("div");
      err.className = "confirm-status error";
      err.textContent = e.message || "Error al confirmar";
      itemEl.appendChild(err);
    }
  }

  function extOf(filename){
    const i = filename.lastIndexOf(".");
    return i === -1 ? filename.toLowerCase() : filename.slice(i + 1).toLowerCase();
  }

  function renderAttachments(){
    const box = $("#chatAttachments");
    box.innerHTML = "";
    pendingAttachments.forEach((att, idx) => {
      const chip = document.createElement("div");
      chip.className = "attachment-chip";
      if(att.preview){
        const thumb = document.createElement("img");
        thumb.className = "att-thumb";
        thumb.src = att.preview;
        thumb.alt = att.filename;
        chip.appendChild(thumb);
      }
      const label = document.createElement("span");
      label.textContent = att.filename;
      chip.appendChild(label);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "att-remove";
      remove.title = "Quitar";
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        pendingAttachments.splice(idx, 1);
        renderAttachments();
      });
      chip.appendChild(remove);
      box.appendChild(chip);
    });
  }

  function readAsText(file){
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("No se pudo leer '" + file.name + "'."));
      reader.readAsText(file);
    });
  }

  function readAsDataURL(file){
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("No se pudo leer '" + file.name + "'."));
      reader.readAsDataURL(file);
    });
  }

  function loadImage(src){
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("no se pudo decodificar"));
      img.src = src;
    });
  }

  // Extensión efectiva de una imagen: la del nombre o, si no tiene (p. ej. pegada del portapapeles), la del MIME.
  function imageExtOf(file){
    const ext = extOf(file.name);
    if(IMAGE_EXTENSIONS.has(ext)) return ext;
    const m = /^image\/(png|jpe?g|webp|gif|bmp)$/.exec(file.type || "");
    return m ? (m[1] === "jpeg" ? "jpg" : m[1]) : "";
  }

  // Lee la imagen como data URL y, si es grande, la reduce a JPEG (lado mayor <= 1600 px).
  // Ahorra subida en LAN/celular. Si el navegador no puede decodificarla, se manda el original.
  async function prepareImage(file, ext){
    const original = await readAsDataURL(file);
    let dataUrl = original;
    let filename = file.name && IMAGE_EXTENSIONS.has(extOf(file.name))
      ? file.name
      : "captura-" + Date.now() + "." + ext;   // pegadas del portapapeles: "image.png" se repetiría
    try{
      const img = await loadImage(original);
      const w = img.naturalWidth, h = img.naturalHeight;
      if(Math.max(w, h) > IMAGE_MAX_SIDE || file.size > IMAGE_RESIZE_ABOVE_BYTES){
        const scale = Math.min(1, IMAGE_MAX_SIDE / Math.max(w, h));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(w * scale));
        canvas.height = Math.max(1, Math.round(h * scale));
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#fff";                     // PNG/WebP con transparencia: fondo blanco, no negro
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        const jpeg = canvas.toDataURL("image/jpeg", 0.85);
        if(jpeg.length < original.length || Math.max(w, h) > IMAGE_MAX_SIDE){
          dataUrl = jpeg;
          filename = filename.replace(/\.[^.]+$/, "") + ".jpg";
        }
      }
    }catch(_){ /* se queda con el original */ }
    return { filename: filename, content: dataUrl, encoding: "base64", isImage: true, preview: dataUrl };
  }

  async function handleFilesSelected(fileList){
    const banner = $("#chatBanner");
    const files = Array.from(fileList || []);
    const rejected = [];

    for(const file of files){
      const imgExt = imageExtOf(file);
      if(imgExt){
        if(pendingAttachments.filter((a) => a.isImage).length >= MAX_IMAGES_PER_MESSAGE){
          rejected.push(file.name + " (máximo " + MAX_IMAGES_PER_MESSAGE + " imágenes por mensaje)");
          continue;
        }
        if(file.size > MAX_IMAGE_BYTES){
          rejected.push(file.name + " (la imagen pesa más de " + (MAX_IMAGE_BYTES / 1024 / 1024) + " MB)");
          continue;
        }
        try{
          pendingAttachments.push(await prepareImage(file, imgExt));
        }catch(e){
          rejected.push(file.name + " (error al leer la imagen)");
        }
        continue;
      }
      const ext = extOf(file.name);
      if(!ALLOWED_ATTACHMENT_EXTENSIONS.has(ext)){
        rejected.push(file.name + " (." + ext + ")");
        continue;
      }
      if(file.size > MAX_ATTACHMENT_BYTES){
        rejected.push(file.name + " (supera el tamaño máximo)");
        continue;
      }
      try{
        const content = await readAsText(file);
        pendingAttachments.push({ filename: file.name, content: content, encoding: "text" });
      }catch(e){
        rejected.push(file.name + " (error al leer)");
      }
    }

    renderAttachments();

    if(rejected.length){
      showBanner(banner, "No se pudo adjuntar: " + rejected.join(", "));
    }else{
      showBanner(banner, "");
    }
  }

  // ---------- Enviar / Detener ----------
  // currentRun != null mientras hay una respuesta en curso. El botón de enviar se
  // convierte en "Detener"; al detener se libera el input al instante (aunque el
  // servidor tarde unos segundos en cortar) y lo que llegue de esa tarea se ignora.
  let currentRun = null; // { pending, aborted, taskId, abort, getText, toolEvents }

  function setRunning(on){
    const btn = $("#chatSendBtn");
    btn.textContent = on ? "⏹ Detener" : "Enviar";
    btn.title = on ? "Detener la respuesta (Esc)" : "";
    btn.classList.toggle("stop", on);
    btn.disabled = false;
  }

  function stopChat(){
    const run = currentRun;
    if(!run || run.aborted) return;
    run.aborted = true;
    Elipse.voice.stop();
    if(run.abort) run.abort();                                      // libera el WebSocket del navegador
    if(run.taskId) Elipse.api.cancelTask(run.taskId);               // corta la generación en el servidor
    setMsgContent(run.pending, (run.getText() + "\n\n⏹ *Detenido.*").trim(), "detenido por ti", run.toolEvents);
    currentRun = null;
    setRunning(false);
    const input = $("#chatInput");
    input.disabled = false;
    input.focus();
    const log = $("#chatLog");
    log.scrollTop = log.scrollHeight;
  }

  async function sendChat(){
    if(currentRun) return;
    Elipse.voice.stop();      // corta el audio del turno anterior
    Elipse.voice.unlock();    // este clic/Enter es el gesto que permite reproducir audio después
    Elipse.voice.liveBegin(); // voz en vivo: empieza a hablar frase por frase mientras llega el texto
    const input = $("#chatInput");
    const log = $("#chatLog");
    const banner = $("#chatBanner");

    const text = input.value.trim();
    const attachments = pendingAttachments.slice();
    if(!text && !attachments.length) return;

    const hasImages = attachments.some((a) => a.isImage);
    const onlyImages = hasImages && attachments.every((a) => a.isImage);
    const messageToSend = text || (onlyImages
      ? "Describe lo que ves en la imagen adjunta."
      : "Revisá el/los archivo(s) adjunto(s).");
    const displayText = attachments.length
      ? messageToSend + "\n\n📎 " + attachments.map((a) => a.filename).join(", ")
      : messageToSend;

    showBanner(banner, "");
    input.value = "";
    pendingAttachments = [];
    renderAttachments();
    const userDiv = appendMsg(log, "user", displayText);
    if(hasImages){                                   // miniaturas dentro de la burbuja del usuario
      const strip = document.createElement("div");
      strip.className = "msg-images";
      attachments.filter((a) => a.isImage).forEach((a) => {
        const im = document.createElement("img");
        im.src = a.preview;
        im.alt = a.filename;
        strip.appendChild(im);
      });
      const body = userDiv.querySelector(".msg-body");
      if(body) body.appendChild(strip);
      log.scrollTop = log.scrollHeight;
    }
    const pending = appendPendingMsg(log, hasImages
      ? "mirando la imagen (si Ollama cambia de modelo, tarda unos segundos)..."
      : "pensando...");

    let streamedText = "";
    const toolEvents = []; // eventos "verificacion" (uno por cada tool que corrió), en orden
    const run = {
      pending, aborted: false, done: false, taskId: null, abort: null, toolEvents,
      getText: () => streamedText,
    };
    currentRun = run;
    setRunning(true);

    try{
      let renderQueued = false;
      const flush = () => {
        renderQueued = false;
        if(run.aborted || run.done) return;
        setMsgContent(pending, streamedText, "escribiendo...", toolEvents, { stillRunning: true });
        log.scrollTop = log.scrollHeight;
      };

      const result = await Elipse.api.runTaskStreaming("/v1/chat", {
        message: messageToSend,
        attachments: attachments.map((a) => ({ filename: a.filename, content: a.content, encoding: a.encoding || "text" })),
      }, {
        onStart(taskId, abort){
          run.taskId = taskId;
          run.abort = abort;
          if(run.aborted){                       // se apretó Detener antes de tener el task_id
            abort();
            Elipse.api.cancelTask(taskId);
          }
        },
        onChunk(texto){
          if(run.aborted) return;
          streamedText += texto;
          Elipse.voice.liveFeed(texto);
          // throttle con requestAnimationFrame: no re-parsear markdown/código
          // en cada token, solo una vez por frame de pintado.
          if(!renderQueued){
            renderQueued = true;
            requestAnimationFrame(flush);
          }
        },
        onEvent(event){
          if(run.aborted) return;
          if(event.type === "verificacion"){
            // Prueba real de qué corrió el agente (tool, argumentos, resultado):
            // se muestra ya mismo en el timeline colapsable.
            toolEvents.push(event);
            renderTimelineSlot(pending, toolEvents, { stillRunning: true });
            log.scrollTop = log.scrollHeight;
          }
          // progreso / plan / verificación: mientras no haya texto todavía,
          // se muestra como estado en vez del "pensando..." fijo.
          if(streamedText) return;
          const label = event.mensaje
            || (event.type === "plan" && "Planificando los siguientes pasos...")
            || (event.type === "verificacion" && (event.tool + (event.ok ? ": verificado" : ": reintentando")))
            || null;
          if(label){
            const t = pending.querySelector(".msg-body .msg-text") || pending.querySelector(".msg-body") || pending;
            t.innerHTML = '<span class="spinner"></span>' + escapeHtml(label);
          }
        },
      });

      run.done = true;                            // un flush de animación pendiente ya no debe pisar la respuesta final
      if(run.aborted) return;

      if(result.status === "error"){
        Elipse.voice.stop();
        pending.remove();
        showBanner(banner, result.result?.detail || result.result?.reply || "Ocurrió un error procesando el mensaje.");
      }else if(result.status === "esperando_confirmacion"){
        Elipse.voice.liveFinish();
        const r = result.result || {};
        const reply = r.reply || "Esta acción necesita tu confirmación.";
        setMsgContent(pending, reply, (r.provider || "") + (r.reason ? " · " + r.reason : "") + " · esperando confirmación", toolEvents);

        const list = r.pending_confirmations || [];
        if(list.length){
          const card = buildConfirmCard(list, (anyApproved) => {
            const note = document.createElement("div");
            note.className = "confirm-done";
            note.textContent = anyApproved
              ? "Listo. Si querés que el agente continúe con el resultado, mandá otro mensaje (ej. «seguí» o «¿quedó bien?»)."
              : "Acción rechazada. Podés pedir otra cosa cuando quieras.";
            pending.appendChild(note);
            log.scrollTop = log.scrollHeight;
          });
          pending.appendChild(card);
        }
      }else if(result.status === "cancelado"){
        Elipse.voice.stop();
        const r = result.result || {};
        setMsgContent(pending, r.reply || "⏹ Detenido.", "detenido", toolEvents);
      }else{
        const r = result.result || {};
        const parts = [];
        if(r.provider) parts.push(r.provider);
        if(r.reason) parts.push(r.reason);
        if(r.used_tools) parts.push("usó herramientas");
        if(typeof r.prompt_tokens === "number") parts.push(r.prompt_tokens + " tokens de prompt");
        if(r.truncated) parts.push(canContinue(r) ? "cortada por límite de tokens" : "cortada por límite de tokens (no se puede continuar automáticamente)");
        setMsgContent(pending, r.reply || "", parts.join(" · "), toolEvents);
        if(canContinue(r)) addContinueButton(pending, r.reply || "");
                // Voz en vivo: si ya fue hablando mientras se escribía, solo se vacía lo que falte y NO se
        // reproduce el mensaje entero otra vez. Si no dijo nada (p. ej. solo código), decide el servidor.
        const liveHandled = Elipse.voice.liveFinish();
        if(run.taskId && r.reply){
          Elipse.voice.attachButton(pending, run.taskId, r.reply);                 // 🔊 por mensaje
          if(!liveHandled && r.voice && r.voice.speak) Elipse.voice.autoplay(run.taskId, r.voice); // habla sola si el servidor lo decidió
        }
      }
    }catch(e){
      if(run.aborted) return;                    // el rechazo "abortado" es esperado al detener
      Elipse.voice.stop();
      pending.remove();
      showBanner(banner, e.message);
    }finally{
      if(currentRun === run){                    // si el usuario ya empezó otro turno, no pisarlo
        currentRun = null;
        setRunning(false);
        log.scrollTop = log.scrollHeight;
        input.focus();
      }
    }
  }

  function init(){
    const sendBtn = $("#chatSendBtn");
    const input = $("#chatInput");
    const attachBtn = $("#chatAttachBtn");
    const fileInput = $("#chatFileInput");

    sendBtn.addEventListener("click", () => { currentRun ? stopChat() : sendChat(); });
    input.addEventListener("keydown", (e) => {
      if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); if(!currentRun) sendChat(); }
    });
    document.addEventListener("keydown", (e) => {          // Esc también detiene
      if(e.key === "Escape" && currentRun && !document.getElementById("codeViewerOverlay")) stopChat();
    });

    // Pegar una imagen del portapapeles (Ctrl+V) en el cuadro de texto.
    input.addEventListener("paste", (e) => {
      const imgs = Array.from((e.clipboardData && e.clipboardData.files) || []).filter((f) => (f.type || "").startsWith("image/"));
      if(imgs.length){ e.preventDefault(); handleFilesSelected(imgs); }
    });
    // Arrastrar archivos o imágenes sobre el compositor.
    const composer = document.querySelector(".composer");
    if(composer){
      ["dragenter", "dragover"].forEach((ev) => composer.addEventListener(ev, (e) => {
        if(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")){
          e.preventDefault();
          composer.classList.add("drag-over");
        }
      }));
      ["dragleave", "drop"].forEach((ev) => composer.addEventListener(ev, () => composer.classList.remove("drag-over")));
      composer.addEventListener("drop", (e) => {
        if(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length){
          e.preventDefault();
          handleFilesSelected(e.dataTransfer.files);
        }
      });
    }

    attachBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      handleFilesSelected(fileInput.files);
      fileInput.value = ""; // permite volver a elegir el mismo archivo después
    });
  }

  // El servidor recuerda qué quedó a medias (continuation.py): alcanza con un mensaje corto.
  // Antes se mandaba un prompt de ~500 caracteres que el planificador trataba como un pedido nuevo.
  const CONTINUE_LABEL = "⏩ Continuar (la respuesta se cortó por el límite de longitud)";
  const canContinue = (r) => !!r.truncated && r.can_continue !== false;   // can_continue ausente = backend viejo

  function buildContinuePrompt(){
    return "continúa";
  }
  function addContinueButton(msgDiv, replyText){
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "continue-btn";
    btn.textContent = CONTINUE_LABEL;
    btn.addEventListener("click", () => continueGeneration(msgDiv, replyText, btn));
    msgDiv.appendChild(btn);
  }

  async function continueGeneration(msgDiv, previousReply, btn){
    const banner = $("#chatBanner");
    const log = $("#chatLog");
    btn.disabled = true;
    btn.textContent = "Continuando...";

    try{
      let streamedText = "";
      let renderQueued = false;
      // La continuación es sobre el mismo mensaje: arrancamos el timeline con
      // los eventos que ya se habían mostrado antes (si los hubo), para no
      // perderlos — quedaron guardados en el propio nodo por setMsgContent.
      const toolEvents = (msgDiv.__toolEvents || []).slice();

      const flush = () => {
        renderQueued = false;
        setMsgContent(msgDiv, previousReply + streamedText, "escribiendo...", toolEvents, { stillRunning: true });
        log.scrollTop = log.scrollHeight;
      };

      const result = await Elipse.api.runTaskStreaming("/v1/chat", {
        message: buildContinuePrompt(previousReply),
        attachments: [],
      }, {
        onChunk(texto){
          streamedText += texto;
          if(!renderQueued){
            renderQueued = true;
            requestAnimationFrame(flush);
          }
        },
        onEvent(event){
          if(event.type === "verificacion"){
            toolEvents.push(event);
            renderTimelineSlot(msgDiv, toolEvents, { stillRunning: true });
            log.scrollTop = log.scrollHeight;
          }
        },
      });

      if(result.status === "error"){
        btn.disabled = false;
        btn.textContent = CONTINUE_LABEL;
        showBanner(banner, result.result?.detail || result.result?.reply || "Ocurrió un error continuando la respuesta.");
      }else if(result.status === "esperando_confirmacion"){
        // Esto sí necesita su propia interacción (aprobar/rechazar), no es texto
        // para pegar al mensaje anterior, así que va como mensaje aparte.
        btn.remove();
        const r = result.result || {};
        const pending = appendMsg(log, "assistant", r.reply || "Esta acción necesita tu confirmación.",
          (r.provider || "") + (r.reason ? " · " + r.reason : "") + " · esperando confirmación", toolEvents);
        const list = r.pending_confirmations || [];
        if(list.length) pending.appendChild(buildConfirmCard(list, () => {}));
      }else{
        const r = result.result || {};
        const fullText = r.replace_previous ? (r.reply || "") : previousReply + (r.reply || "");
        const parts = [];
        if(r.provider) parts.push(r.provider);
        if(r.reason) parts.push(r.reason);
        if(typeof r.prompt_tokens === "number") parts.push(r.prompt_tokens + " tokens de prompt");
        if(r.truncated) parts.push(canContinue(r) ? "cortada por límite de tokens" : "cortada por límite de tokens (no se puede continuar automáticamente)");
        setMsgContent(msgDiv, fullText, parts.join(" · "), toolEvents);
        if(canContinue(r)) addContinueButton(msgDiv, fullText);
      }
    }catch(e){
      btn.disabled = false;
      btn.textContent = CONTINUE_LABEL;
      showBanner(banner, e.message);
    }finally{
      log.scrollTop = log.scrollHeight;
    }
  }
  Elipse.chat = { init };
  Elipse.renderRichContent = renderRichContent;
})();