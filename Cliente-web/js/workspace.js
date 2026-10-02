// workspace.js — explorador de archivos del workspace: ver, descargar, borrar.
// Backend: GET /v1/workspace/files, GET /v1/workspace/download, DELETE /v1/workspace/file.
// El visor (PDF / Word / Markdown / código / imágenes / HTML) es el mismo de docs.js.
window.Elipse = window.Elipse || {};
(function(){
  "use strict";
  const Elipse = window.Elipse;
  const $ = Elipse.$;

  const KINDS = [
    { id: "all",    label: "Todo" },
    { id: "doc",    label: "Documentos" },
    { id: "code",   label: "Código" },
    { id: "img",    label: "Imágenes" },
    { id: "other",  label: "Otros" },
  ];
  const DOC_EXT = new Set(["pdf", "docx", "doc", "md", "markdown", "txt", "rtf", "odt"]);
  const IMG_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);
  const CODE_EXT = new Set([
    "py", "js", "jsx", "ts", "tsx", "html", "htm", "css", "scss", "json", "java", "sh", "bash",
    "sql", "c", "h", "cpp", "hpp", "go", "rs", "rb", "php", "yml", "yaml", "xml", "toml", "ini",
    "kt", "swift", "cs", "vue", "svelte", "csv", "log", "env", "dockerfile",
  ]);

  let files = [];
  let kind = "all";
  let loaded = false;

  const kindOf = (ext) => IMG_EXT.has(ext) ? "img" : DOC_EXT.has(ext) ? "doc" : CODE_EXT.has(ext) ? "code" : "other";

  function el(tag, cls, text){
    const n = document.createElement(tag);
    if(cls) n.className = cls;
    if(text != null) n.textContent = text;
    return n;
  }

  function showBanner(message){
    const b = $("#wsBanner");
    b.textContent = message || "";
    b.classList.toggle("show", !!message);
  }

  function fmtSize(n){
    if(n < 1024) return n + " B";
    if(n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  function fmtDate(sec){
    try{
      return new Date(sec * 1000).toLocaleString("es-MX", { dateStyle: "medium", timeStyle: "short" });
    }catch(_){
      return new Date(sec * 1000).toLocaleString();
    }
  }

  function visible(){
    const q = ($("#wsSearch").value || "").trim().toLowerCase();
    const sort = $("#wsSort").value;
    const list = files.filter((f) =>
      (kind === "all" || kindOf(f.ext) === kind) && (!q || f.path.toLowerCase().includes(q)));
    if(sort === "name") list.sort((a, b) => a.path.localeCompare(b.path, "es"));
    else if(sort === "size") list.sort((a, b) => b.size - a.size);
    else list.sort((a, b) => b.mtime - a.mtime);
    return list;
  }

  function renderChips(){
    const box = $("#wsChips");
    box.replaceChildren();
    KINDS.forEach((k) => {
      const n = k.id === "all" ? files.length : files.filter((f) => kindOf(f.ext) === k.id).length;
      const b = el("button", "ws-chip" + (kind === k.id ? " active" : ""), k.label + " · " + n);
      b.type = "button";
      b.onclick = () => { kind = k.id; render(); };
      box.appendChild(b);
    });
  }

  function open(f){
    if(Elipse.docs.canPreview(f.ext)) Elipse.docs.openViewer({ title: f.name, files: [f.path] });
    else Elipse.docs.download(f.path).catch((e) => showBanner(e.message));
  }

  async function remove(f){
    if(!confirm("¿Borrar \"" + f.path + "\"?\nEsta acción no se puede deshacer.")) return;
    try{
      await Elipse.api.apiFetch("/v1/workspace/file?path=" + encodeURIComponent(f.path), {
        method: "DELETE",
        headers: Elipse.api.authHeaders(),
      });
      files = files.filter((x) => x.path !== f.path);
      showBanner("");
      render();
    }catch(e){
      showBanner(e.message);
    }
  }

  function row(f){
    const r = el("div", "ws-row");

    const badge = el("span", "ws-ext ws-" + kindOf(f.ext), f.ext ? f.ext.slice(0, 5) : "—");
    const info = el("div", "ws-info");
    const name = el("button", "ws-name", f.name);
    name.type = "button";
    name.title = f.path;
    name.onclick = () => open(f);
    info.appendChild(name);
    if(f.dir) info.appendChild(el("div", "ws-dir", f.dir + "/"));

    const size = el("span", "ws-size", fmtSize(f.size));
    const date = el("span", "ws-date", fmtDate(f.mtime));

    const acts = el("div", "ws-actions");
    if(Elipse.docs.canPreview(f.ext)){
      const v = el("button", "btn", "Ver");
      v.type = "button";
      v.onclick = () => open(f);
      acts.appendChild(v);
    }
    const d = el("button", "btn-icon", "⬇");
    d.type = "button";
    d.title = "Descargar";
    d.onclick = () => Elipse.docs.download(f.path).catch((e) => showBanner(e.message));
    const x = el("button", "btn-icon ws-del", "🗑");
    x.type = "button";
    x.title = "Borrar";
    x.onclick = () => remove(f);
    acts.append(d, x);

    r.append(badge, info, size, date, acts);
    return r;
  }

  function render(){
    renderChips();
    const list = visible();
    const box = $("#wsList");
    box.replaceChildren();
    $("#wsCount").textContent = list.length === files.length
      ? files.length + " archivo" + (files.length === 1 ? "" : "s")
      : list.length + " de " + files.length + " archivos";
    if(!list.length){
      box.appendChild(el("div", "empty", files.length
        ? "Ningún archivo coincide con el filtro."
        : "El workspace está vacío. Pídele a ELIPSE un documento o un código y aparecerá aquí."));
      return;
    }
    list.forEach((f) => box.appendChild(row(f)));
  }

  async function load(){
    showBanner("");
    try{
      const data = await Elipse.api.apiFetch("/v1/workspace/files", { headers: Elipse.api.authHeaders() });
      files = data.files || [];
      loaded = true;
      if(data.truncated) showBanner("Se muestran solo los primeros " + files.length + " archivos.");
      render();
    }catch(e){
      showBanner(e.message);
      if(!loaded) render();
    }
  }

  function init(){
    $("#wsSearch").addEventListener("input", render);
    $("#wsSort").addEventListener("change", render);
    $("#wsRefresh").addEventListener("click", load);
  }

  Elipse.workspace = { init, load };
})();