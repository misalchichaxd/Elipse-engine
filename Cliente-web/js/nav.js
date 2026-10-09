window.Elipse = window.Elipse || {};
(function(){
  "use strict";
  const Elipse = window.Elipse;
  const KEY = "elipse_sidebar_collapsed";

  function setCollapsed(on){
    const app = document.querySelector(".app");
    if(!app) return;
    app.classList.toggle("sidebar-collapsed", !!on);
    const btn = document.getElementById("sidebarToggle");
    if(btn){
      btn.setAttribute("aria-label", on ? "Expandir panel" : "Minimizar panel");
      btn.title = on ? "Expandir panel" : "Minimizar panel";
    }
    try{ localStorage.setItem(KEY, on ? "1" : "0"); }catch(e){}
  }

  function init(){
    document.querySelectorAll("nav button").forEach(btn => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
        document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
        btn.classList.add("active");
        const view = document.querySelector("#view-" + btn.dataset.view);
        if(view) view.classList.add("active");
        if(btn.dataset.view === "keys" && Elipse.keys) Elipse.keys.load();
        if(btn.dataset.view === "workspace" && Elipse.workspace) Elipse.workspace.load();
        if(btn.dataset.view === "voice" && Elipse.voice) Elipse.voice.load();
      });
    });

    const toggle = document.getElementById("sidebarToggle");
    if(toggle){
      let collapsed = false;
      try{ collapsed = localStorage.getItem(KEY) === "1"; }catch(e){}
      setCollapsed(collapsed);
      toggle.addEventListener("click", () => {
        const app = document.querySelector(".app");
        setCollapsed(!app.classList.contains("sidebar-collapsed"));
      });
    }
  }

  Elipse.nav = { init };
})();