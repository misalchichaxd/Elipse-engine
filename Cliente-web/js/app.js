window.Elipse = window.Elipse || {};
(function(){
  "use strict";
  const Elipse = window.Elipse;
  const $ = Elipse.$;

  async function checkStatus(){
    const dot = $("#statusDot"), text = $("#statusText");
    const meta = $("#statusMeta");
    try{
      const data = await Elipse.api.apiFetch("/v1/status", {});
      dot.className = "dot ok";
      const p = data.providers || {};
      text.textContent = data.name + " · " + (data.model || "modelo activo");
      if(meta){
        meta.textContent = "General: " + (p.general || "—")
          + " · Código: " + (p.code || "—")
          + " · Investigación: " + (p.research || "—");
      }
    }catch(e){
      console.error("checkStatus falló:", e);
      dot.className = "dot bad";
      text.textContent = "sin conexión";
      if(meta) meta.textContent = e.message || "Revisa la sección Conexión abajo";
    }
  }

  function bindSettings(){
    $("#baseUrlInput").value = Elipse.state.baseUrl;
    $("#apiKeyInput").value = Elipse.state.apiKey;
    $("#saveSettingsBtn").addEventListener("click", () => {
      Elipse.state.baseUrl = $("#baseUrlInput").value.trim().replace(/\/$/, "") || "http://127.0.0.1:8000";
      Elipse.state.apiKey = $("#apiKeyInput").value.trim();
      Elipse.saveState();
      checkStatus();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    Elipse.nav.init();
    bindSettings();
    checkStatus();
    Elipse.chat.init();
    Elipse.research.init();
    Elipse.memory.init();
    Elipse.keys.init();
    Elipse.workspace.init();
  });
})();