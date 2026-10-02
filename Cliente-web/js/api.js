window.Elipse = window.Elipse || {};
(function(){
  "use strict";
  const Elipse = window.Elipse;

  const POLL_INTERVAL_MS = 1400;
  const POLL_TIMEOUT_MS = 5 * 60 * 1000;   // una tarea con modelo local puede tardar
  const MAX_POLL_FAILURES = 3;              // fallos de red seguidos tolerados mientras se espera
  const WS_OPEN_TIMEOUT_MS = 4000;          // si el WebSocket no abre en este tiempo, cae a polling

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // Headers con la API key. 'extra' permite sumar otros (ej. Content-Type).
  function authHeaders(extra){
    const headers = Object.assign({}, extra || {});
    if(Elipse.state.apiKey){
      headers["Authorization"] = "Bearer " + Elipse.state.apiKey;
    }
    return headers;
  }

  // fetch contra la baseUrl guardada. Devuelve el JSON, o lanza Error con un
  // mensaje legible (sin conexión, 401, detail de FastAPI, etc).
  async function apiFetch(path, opts){
    let res;
    try{
      res = await fetch(Elipse.state.baseUrl + path, opts || {});
    }catch(e){
      throw new Error("No se pudo conectar con ELIPSE en " + Elipse.state.baseUrl + ". ¿Está corriendo el servidor?");
    }

    let data = null;
    try{ data = await res.json(); }catch(_){ /* respuesta sin JSON */ }

    if(!res.ok){
      let detail = data && data.detail;
      if(detail && typeof detail !== "string") detail = JSON.stringify(detail);
      throw new Error(detail || ("Error " + res.status + " del servidor."));
    }
    return data;
  }

  // Polling puro contra /v1/task/{id}. Lo usan tanto runTask como el fallback
  // de runTaskStreaming cuando el WebSocket no pudo abrirse o se cortó.
  async function pollTask(pollPath){
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    let failures = 0;

    while(Date.now() < deadline){
      await sleep(POLL_INTERVAL_MS);

      let task;
      try{
        task = await apiFetch(pollPath, { headers: authHeaders() });
        failures = 0;
      }catch(e){
        failures += 1;
        if(failures >= MAX_POLL_FAILURES) throw e;
        continue;
      }

      if(task.status === "en_progreso") continue;

      // Caso "no existe esa tarea": el backend responde {status:"error", detail:...} sin 'result'.
      if(task.status === "error" && !task.result){
        throw new Error(task.detail || "La tarea falló.");
      }
      return task;
    }

    throw new Error("La tarea tardó demasiado y se dejó de esperar.");
  }

  // Para endpoints que corren en background (/v1/chat, /v1/research):
  // dispara la tarea y hace polling a /v1/task/{id} hasta que deje de estar
  // "en_progreso". Devuelve {status, result}, tal como lo entrega el backend.
  async function runTask(path, body){
    const started = await apiFetch(path, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    });

    if(!started || !started.task_id){
      throw new Error("El servidor no devolvió un task_id.");
    }

    return pollTask(started.poll_url || ("/v1/task/" + started.task_id));
  }

  // Como runTask, pero además abre el WebSocket (ws_url) que main.py expone
  // para recibir eventos en vivo: onChunk(texto) por cada pedazo de texto que
  // el modelo va generando, onEvent(evento) para el resto (progreso/plan/
  // verificacion). Si el WebSocket no llega a abrir o se corta a mitad de
  // camino, cae solo a pollTask — el resultado final llega igual, solo que
  // sin el efecto de "se va escribiendo".
  function runTaskStreaming(path, body, handlers){
    handlers = handlers || {};
    const onChunk = handlers.onChunk || function(){};
    const onEvent = handlers.onEvent || function(){};
    const onStart = handlers.onStart || function(){};

    return apiFetch(path, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    }).then((started) => {
      if(!started || !started.task_id){
        throw new Error("El servidor no devolvió un task_id.");
      }
      const pollPath = started.poll_url || ("/v1/task/" + started.task_id);
      const wsPath = started.ws_url || ("/v1/ws/" + started.task_id);

      return new Promise((resolve, reject) => {
        let settled = false;
        let ws = null;

        // Corta la espera desde el cliente (botón Detener): cierra el WebSocket y rechaza.
        // Frenar la generación en el servidor es cancelTask(); esto solo libera al navegador.
        const abort = () => {
          if(settled) return;
          settled = true;
          try{ if(ws) ws.close(); }catch(_){}
          reject(new Error("abortado"));
        };
        onStart(started.task_id, abort);

        const finishWithPolling = () => {
          if(settled) return;
          settled = true;
          pollTask(pollPath).then(resolve).catch(reject);
        };

        try{
          const wsUrl = (Elipse.state.baseUrl + wsPath).replace(/^http/, "ws")
            + "?token=" + encodeURIComponent(Elipse.state.apiKey || "");
          ws = new WebSocket(wsUrl);
        }catch(e){
          finishWithPolling();
          return;
        }

        const openTimer = setTimeout(finishWithPolling, WS_OPEN_TIMEOUT_MS);
        ws.onopen = () => clearTimeout(openTimer);

        ws.onmessage = (msg) => {
          let event;
          try{ event = JSON.parse(msg.data); }catch(_){ return; }

          if(event.type === "chunk"){ onChunk(event.texto || ""); return; }
          if(event.type === "ping") return;

          if(event.type === "final"){
            settled = true;
            ws.close();
            // Se pide una vez más a /v1/task/{id}: para ese momento ya está listo
            // (responde al toque) y trae el mismo shape {status, result} que ya
            // usa el resto del cliente, sin duplicar esa lógica acá.
            apiFetch(pollPath, { headers: authHeaders() }).then(resolve).catch(reject);
            return;
          }
          if(event.type === "error"){
            settled = true;
            ws.close();
            reject(new Error(event.mensaje || "Error en la tarea."));
            return;
          }
          onEvent(event); // progreso / plan / verificacion
        };

        ws.onerror = () => { clearTimeout(openTimer); finishWithPolling(); };
        ws.onclose = () => { clearTimeout(openTimer); if(!settled) finishWithPolling(); };
      });
    });
  }

  // Pide al backend que corte la tarea (se detiene en el próximo evento del agente).
  function cancelTask(taskId){
    return apiFetch("/v1/task/" + taskId + "/cancel", {
      method: "POST",
      headers: authHeaders(),
    }).catch(() => null);
  }

  Elipse.api = { authHeaders, apiFetch, runTask, runTaskStreaming, cancelTask };
})();