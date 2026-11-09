## Perfiles de anfitrión (multi-tenant) — Fases 0 a 4 hechas, Fase 5 pendiente

> **Léelo completo antes de tocar nada de perfiles.** Diseño y reglas completas: `PLAN_PERFILES_ELIPSE_v2.md` (R1–R13 = reglas, E1–E16 = puntos de enforcement). Este apartado dice **qué quedó construido, cómo, dónde y qué sigue**.

### Para qué sirve

ELIPSE es un solo motor con un solo dueño. Un **perfil** deja que un sistema externo (el **anfitrión**; el caso real es **RBE**, una app Django) use el motor con **su propio rol, sus propias tools, su memoria y su historial**, sin ver nada de ELIPSE ni de otros anfitriones. El anfitrión se conecta con una **API key de anfitrión** (atada a un perfil y con un *techo* de permisos) y, para que el modelo ejecute cosas dentro de su sistema, expone un **callback HMAC** (tools del anfitrión).

- **`elipse`** es el perfil implícito: es el comportamiento de siempre, **no tiene fila** en la tabla y **no cambia ni un byte** (lo protegen los 6 tests dorados `tests/test_golden_default.py`).
- Cualquier otro perfil (`rbe`…) **nace sin permisos** (deny-by-default) y solo puede lo que su *spec* declare **y** el *techo* (caps) de su llave permita: **efectivo = spec ∩ caps**. Lo que el techo recorta se informa (`rejected`), no falla en silencio.
- ⚠️ Glosario: "perfil" aquí **no** es el "perfil de modelo" (tiny/small/large) de `skillc`/skills.

### Estado por fase

| Fase | Qué es | Estado |
|---|---|---|
| 0–1 | Cimientos: migración de la base, llaves con `profile` y `caps`, `Principal`, 403 a llaves de anfitrión en todo lo que no esté abierto, tests dorados | ✅ |
| 2 | Capacidades y gestión: `profiles.py`, `/v1/profiles`, `manage_profiles.py`, pantalla **Perfiles** del launcher, tools filtradas por perfil | ✅ |
| 3 | Aislamiento: chat para llaves de anfitrión; el perfil viaja por código hasta `execute_tool`; historial, memoria, prompt, voz y poda por perfil | ✅ |
| 4 | Callback HMAC al anfitrión, `confirm_for`, `actor`/`session` en acciones pendientes, prompt de perfiles limpio (sin creador, con fecha) | ✅ probado de punta a punta con `callback_demo.py` y el modelo local |
| 5 | SDK `elipse_sdk`, helper de Django y migración de los intents de RBE a tools | ⏳ ver "Lo que falta" |

### Modelo en una página

- **Llave admin** = llave con `caps NULL` y perfil `elipse`. Todas las llaves anteriores a los perfiles son admin (la migración las marca así). Es la única que usa `/v1/profiles` (salvo lo propio), voz, skills, memoria global, etc.
- **Llave de anfitrión** = llave con `profile='rbe'` y `caps` (JSON, el techo). Se crea con `python -m app.manage_keys create NOMBRE --profile rbe --caps caps.json`. `Principal` (`profiles.py`) sale **siempre de la llave**, nunca del texto del mensaje ni de lo que mande el modelo (R4).
- **`Profile`** (dataclass inmutable): `name`, `is_default`, `spec`, `caps`, `host_tools` (ya recortadas por caps), `synced`, `enabled`. **`Capabilities(profile)`** responde las preguntas de enforcement: `tool_allowed()`, `is_host_tool()`, `host_tool()`, `confirm_for()`, `memory()`, `limits()`, `vision_allowed()`, `continuation_allowed()`, `attachments_allowed()`, `data_policy()`… Para `elipse` todo es "permitido" y no copia el esquema.
- **Spec** (lo que declara el perfil; claves válidas en `SPEC_KEYS`, cualquier otra se descarta e informa): `role`, `brief`, `traits`, `rules`, `tools` (`{"builtin":[...], "host":[{name, description, parameters (JSON schema), risk: read|write|danger}]}`), `pipelines`, `vision`, `attachments`, `skills`, `continuation`, `voice`, `model`, `memory` (`max_items` 1–5000, `max_distance` 0–2 o `null`), `limits`, `confirm`, `data_policy`, `retention`. Tope 32 KB.
- **Caps** (techo de la llave; ejemplo en `profiles.default_caps()` o `GET /v1/profiles/caps-template`): `tools`, `host_tools`, `max_host_tools`, `pipelines`, `vision`, `attachments`, `continuation`, `mcp`, `skills`, `voice{allowed,cloud}`, `data_policy`. Si el techo es `NULL` en una llave de anfitrión → todo apagado.
- **Límites por perfil** (`DEFAULT_LIMITS` / `LIMIT_BOUNDS`): `rounds` 3 (1–6), `tool_timeout_s` 10 (1–60), `max_rows` 100, `history_limit` 8, `queue_timeout_s` 120 (5–600).
- **Tools propias abiertas a perfiles** (`PROFILE_SAFE_BUILTINS`): `get_current_datetime`, `calculate`, `remember`, `recall`. El resto de las tools propias (archivos, web, código, visión, research, documentos) **siguen cerradas** para perfiles: pedirlas se recorta e informa. `remember`/`recall` son **profile-aware** (`PROFILE_AWARE_TOOLS`): `execute_tool` les inyecta el perfil por código y descarta cualquier `profile`/`actor`/`session` que venga en los argumentos (R13, `PROFILE_ARG_KEYS`).
- **Confirmación** (`Capabilities.confirm_for(risk)`): `danger` **siempre** confirma y no se puede apagar; `read` no confirma por defecto; `write` sí; se ajusta con `spec.confirm = {"read|write|danger": "always|never"}`; un valor desconocido confirma.

### Qué se guarda y dónde

- **Base (`elipse.db`)**: `SCHEMA_VERSION = 1` (`PRAGMA user_version`). La migración (en `db.py`) es **idempotente**, hace un respaldo automático `elipse.pre_perfiles.backup.db` antes de tocar una base con datos y agrega columnas `profile` (default `'elipse'`) a `messages`, `facts`, `router_log`, `pending_actions`, y `caps` a `api_keys`; además `session`/`actor`/`tools`/`status` en lo que lo necesita. Tabla nueva **`profiles`**: `name` (PK), `spec` (JSON), `enabled`, `callback_url`, `callback_secret`, `created_at`, `updated_at`.
- **Poda por perfil** (`db.prune_old_rows`): `elipse` conserva exactamente los topes de siempre; los demás se podan **solo entre sí** (tope y días de `spec.retention`: `messages_max_rows`, `messages_days`, `router_log_max_rows`…). Un perfil nunca desplaza filas de `elipse`.
- **Memoria semántica** (`memory.py`): `elipse` usa la colección de siempre (`settings.chroma_collection`, **intacta**, R8); cualquier otro perfil usa `<colección>__<perfil>` (si el nombre termina en `_` o `-` se le agrega `.z`, porque Chroma exige terminar en alfanumérico). Todas las funciones aceptan `profile=` (`add_memory`, `search_memory`, `delete_memory`, `count_memories`, `enforce_retention_policy`, `reset_memory`). Cliente y modelo de embeddings son uno solo; cambia la colección.
- **Voz por perfil** (`voice/store.py`): filas `profile:<nombre>:<clave>` en `voice_settings`; capas `.env < globales < spec.voice ∩ caps < filas del perfil`. `plan_for_final(..., profile)`.
- **Callback**: URL y secreto del admin (`set_callback`, solo admin, R12). ⚠️ El secreto se guarda **en claro** en SQLite (hace falta para firmar); la API **nunca** lo devuelve (solo `has_secret`); el launcher y el CLI lo muestran una sola vez al generarlo.

### API

| Ruta | Quién |
|---|---|
| `POST /v1/chat` (acepta `session` y `actor`, solo de llaves de anfitrión), `GET /v1/task/{id}`, `POST /v1/task/{id}/cancel`, `WS /v1/ws/{id}`, `GET /v1/pending-actions`, `POST /v1/confirm-action/{id}` | admin **y** anfitrión (`require_principal`). La tarea/acción debe pertenecer al perfil de la llave; si es ajena → **404**. Chat de anfitrión: **409** si el perfil no está sincronizado/sin rol, **403** si manda adjuntos no permitidos, **422** si `session` es inválida. |
| `GET /v1/profiles`, `GET /v1/profiles/caps-template`, `POST /v1/profiles`, `POST /v1/profiles/{n}/enabled`, `DELETE /v1/profiles/{n}`, `PUT/DELETE /v1/profiles/{n}/callback` | solo admin (`require_admin_key`) |
| `GET/PUT /v1/profiles/{n}` | admin, o una llave de anfitrión **solo sobre su propio perfil** (el `PUT` informa en `rejected` lo que su techo recortó; **nunca** toca el callback) |
| todo lo demás (`/v1/memory*`, `/v1/skills*`, `/v1/voice/*`, `/v1/files*`, `/v1/auth/keys*`, `/v1/tools`, `/v1/mcp/status`, `/v1/research*`, `/v1/install-package`…) | solo admin (`require_api_key` da 403 a llaves de anfitrión) |

### CLI

```
python -m app.manage_profiles [--json] list | show N | create N --spec f.json | update N --spec f.json
                                       | enable N | disable N | delete N | set-callback N --url U (--secret S | --generate)
                                       | clear-callback N | caps-template
python -m app.manage_keys create NOMBRE [--profile rbe --caps caps.json] | list | revoke ID
```

`set-callback` exige que el perfil **ya exista** (`create` primero). El launcher usa estos mismos comandos con `--json` (`launcher/process_utils.py::run_profiles`).

### Tools del anfitrión y callback HMAC (`app/core/callback.py`)

Las tools que corren dentro del sistema del anfitrión se declaran en `spec.tools.host` y viajan al modelo como cualquier tool. Al llamarlas, `execute_tool` → `callback.call_host_tool(...)` hace un **POST firmado** a `<callback_url>/<tool>`:

- Headers: `X-Elipse-Ts` (epoch s) y `X-Elipse-Sig` = `hex(HMAC_SHA256(secreto, f"{ts}.{perfil}.{tool}.{sha256_hex(body)}"))`.
- Body JSON: `{"call_id","tool","args","actor","session"}`. Respuesta: `{"ok":true,"result":<json>}` o `{"ok":false,"error":"texto corto"}`.
- **`actor` y `session` salen de la petición autenticada, jamás de los argumentos del modelo.** Sin redirects, sin proxies del entorno, timeout total = `limits.tool_timeout_s`, respuesta acotada (`mcp_max_output_chars`, truncada con marcador), argumentos ≤ 32 KB. Todo error vuelve como texto `"Error: ..."` **sin** URL ni secreto; nunca lanza.
- `callback.verify_request(secret, profile, tool, body, ts, sig)` es el lado del anfitrión (ventana de 60 s, comparación en tiempo constante). **El SDK de la Fase 5 debe llevar una copia exacta.**
- **Acciones pendientes** (tool con riesgo que pide confirmación): se guardan envueltas `{"v":2,"args":…,"ctx":{"actor","session"}}` para perfiles ≠ `elipse` (el modelo nunca puede forjar `ctx`: lo suyo va dentro de `args`). `resolve_pending_action` desenvuelve y ejecuta con ese `actor`/`session`. El formato de `elipse` no cambia.
- `safety.exec_profile(nombre, caller)`: con qué perfil se ejecuta una acción al confirmarla (llave de anfitrión → su propio perfil con su techo real; admin → el perfil de la acción con el techo restrictivo por defecto; falla cerrado).

### Cómo viaja el perfil por el código (parámetro explícito, **no** `ContextVar`: hay `ThreadPoolExecutor`)

`main.chat` → `tasks.create_task(profile, session, actor)` → `run_agent_task` → `ElipseCore.run(profile, session, actor)` → `_execute_calls(..., profile, session, actor)` → `tools.execute_tool(name, args, profile, actor, session)`. Los dos llamadores de `execute_tool` son `core.py::_execute_calls` y `safety.resolve_pending_action`. Dentro de `core.py` el perfil decide: `get_recent_messages`/`save_message`/`log_turn_metadata` (con `profile`, `session`, `actor`, `tools`, `status`; también en la ruta de cancelación de `tasks.py`), `REQUIRED_TOOLS` (se ignoran las no permitidas), visión/pipelines/continuación/skills (se saltan si el techo los niega), y `personality.build_system_prompt(compact, profile)`.

- **Prompt de un perfil ≠ `elipse`** (`personality._build_profile_prompt`): nombre + `role` + valores + rasgos + reglas de estilo **sin** las que hablan del creador o mandan a hacer preguntas de relleno + regla de brevedad + reglas técnicas mínimas (resultados de tools = datos; no inventar; contar errores; usar la fecha actual; regla de memoria si tiene `remember`/`recall`) + hechos del perfil + **fecha y hora actuales**. Es bastante más corto que el de `elipse` (≈700 vs ≈1000 tokens). El de `elipse` es el código de siempre, intacto.
- `tasks.py`: la cola tiene timeout solo para perfiles ≠ `elipse` (`limits.queue_timeout_s`, E14); `elipse` espera como siempre.

### Qué cambió en archivos que ya existían (para no sorprenderse al leer el diff)

`db.py` (migración v1, poda por perfil) · `auth.py` (`Principal`, `require_principal`, `require_admin_key`, `require_principal_ws`, `create_key(profile, caps)`) · `main.py` (`/v1/chat` y compañía con `require_principal`, `session`/`actor` en `ChatRequest`, `profiles_router`) · `core.py` · `tasks.py` · `safety.py` · `tools.py` (todo acepta `profile`; `execute_tool(name, args, profile, actor, session)`) · `personality.py` · `memory.py` · `voice/` (`store.py`, `__init__.py`, `router.py`) · `manage_keys.py` (`--profile`, `--caps`) · `launcher/settings_window.py` y `launcher/process_utils.py` (fila **Perfiles** → **Administrar**).

### Archivos nuevos

`app/core/profiles.py` (modelo, spec/caps, `Capabilities`, gestión, callback config) · `app/core/profiles_api.py` (router `/v1/profiles`) · `app/core/callback.py` (firma, verificación y despacho HMAC) · `app/manage_profiles.py` (CLI) · `launcher/profiles_dialog.py` (crear/editar perfiles y llaves, callback; la llave y el secreto se muestran una sola vez) · `callback_demo.py` (raíz; anfitrión de juguete para probar sin Django) · tests (abajo).

### Tests (74 pasan; corre `pytest tests -v` **desde la raíz del proyecto**, `~/Elipse-engine/elipse`)

`test_golden_default.py` (6: `elipse` no cambia) · `test_migration.py` (16: migración, llaves, 403, poda) · `test_profiles_phase2.py` (14) · `test_profiles_phase3.py` (19: aislamiento de memoria, historial, prompt, tareas ajenas, adjuntos) · `test_profiles_phase4.py` (14: firma, timeout, redirects, respuesta enorme, acciones pendientes con `actor`/`session`) · `test_profiles_prompt.py` (5: prompt sin creador y con fecha). Los de memoria usan un **Chroma falso en memoria** (no cargan el modelo de embeddings): la memoria con Chroma real **no está probada por tests**, solo a mano.

### Prueba de extremo a extremo con el modelo real (sin Django)

3 terminales, todas con `cd ~/Elipse-engine/elipse && source venv/bin/activate`:
(A) `uvicorn app.main:app --host 127.0.0.1 --port 8000` · (C) `python callback_demo.py <SECRETO>` (≥16 caracteres) · (B) crear perfil `rbe` (`create rbe --spec rbe.json`), `set-callback rbe --url http://127.0.0.1:8765/elipse/tools --secret <SECRETO>`, `manage_keys create ... --profile rbe --caps caps_rbe.json` (techo con `"host_tools": true`), y `curl POST /v1/chat` con `{"message":"¿Qué viajes hay hoy?","session":"s2","actor":"admin:42"}` + `GET /v1/task/<id>`. Debe verse en C `[callback] viajes_del_dia args={'fecha': '<hoy>'} actor='admin:42' session='s2'` y la respuesta con el viaje 101. Al terminar: `clear-callback rbe` y revocar la llave de prueba.

### Cómo se aplicó (historial; **ya aplicado, no repetir**)

Parches de un solo uso, en este orden: `patch_tools_fase2.py` → `patch_fase3.py` → `patch_fase3b.py` (+ `memory.py` nuevo) → `patch_fase4.py` (+ `callback.py`) → `patch_fase4b.py`. Cada uno hace reemplazos exactos, respeta CRLF/LF y **aborta sin escribir ese archivo** si un ancla no coincide exactamente 1 vez (por eso volver a correrlos da error y es seguro). Quien siga puede editar directo o con el mismo estilo de parche.

### Trampas conocidas (aprendidas por las malas)

- **Fin de línea:** `core.py`, `tools.py`, `personality.py`, `safety.py` y `config.py` son **CRLF**; `profiles.py`, `tasks.py`, `auth.py`, `main.py`, `memory.py` son **LF**. Respétalo (verifícalo con Python contando `b"\r\n"`; `grep -c $'\r'` no es fiable en todos los shells).
- Un parche de Fase 3 llegó **a medias** (usaba `PROFILE_AWARE_TOOLS`, `memory_settings`, `Capabilities.memory()`, `exec_profile`, `confirm_for` y `memory.py` con `profile=`, que no existían). Moraleja: tras cualquier parche, **importar `app.main` y correr toda la suite**, no solo compilar.
- Un test de Fase 2 (`test_execute_tool_gate_and_r13`) usaba la base real y se rompió cuando existía un perfil `rbe` con callback; ahora usa `mock.patch.object(profiles, "get_callback", ...)`. **Los tests no deben depender de `elipse.db`.**
- Un `403`/`firma inválida` en la prueba casi siempre es **secreto distinto** entre `set-callback` y el anfitrión.
- Con **historial en la sesión**, el modelo local (Qwen3-4B) puede contestar con un dato viejo en vez de volver a llamar la tool, y las preguntas compuestas a veces encadenan solo una tool. No es un fallo del callback.

### Lo que falta (en orden recomendado)

1. **Prueba de humo con Chroma real** de `remember`/`recall` con un perfil (los tests usan uno falso).
2. **Fase 5 — SDK y RBE** (`PLAN §4.9–4.10`). Necesita de RBE: `taquilla/elipse_views.py`, la plantilla `elipse.html` y el archivo donde viven los intents.
   - Paquete **`elipse_sdk`**: `@elipse.tool` (esquema JSON desde type hints), `sync()` (PUT del perfil), `chat()` con streaming, `verify_request()` (copia exacta de `callback.verify_request`).
   - **Helper de Django**: vista de chat y vista de callback que verifica la firma.
   - Reemplazar `elipse_chat` de RBE (hoy intents por regex + Groq) y **migrar ~40 intents a tools deterministas**, con un test que compare la salida vieja contra la nueva.
   - Tool opcional de **SQL de solo lectura**, apagada por defecto; pasar `INFO_RBE` y el esquema a la memoria del perfil `rbe`.
3. **Resumen "¿cómo vas en RBE?" (§4.8)**: `profile_brief()` hecho solo con SQL (≤600 caracteres), disparador en `core._run` solo para `elipse`, sanitizar textos de usuarios de RBE, `GET /v1/profiles/{name}/brief`. No depende de nada: se puede hacer en cualquier momento.
4. **E8 exacto**: el prompt de perfiles es propio y corto; el plan pedía `TOOL_RULE_SNIPPETS` con test de reconstrucción byte a byte contra el de `elipse`.
5. **Abrir más tools propias a perfiles** de a una (`read_file`, `search_web`, `research_topic`…): cada una debe recibir `profile` por código, usar su propio espacio, y entrar a `PROFILE_SAFE_BUILTINS` + `PROFILE_AWARE_TOOLS` con test de aislamiento.
6. **E6**: defensas internas en `pipeline`, `docpipeline`, `docedit`, `gallery`, `vision` y `codegen` (hoy basta con que `core.py` no las llame para un perfil que no las tiene).
7. **E16**: `/v1/voice/say` y `/v1/voice/{task}/stream` con la voz del perfil siguen **solo admin** (no se abrieron a llaves de anfitrión; verifica `voice/api.py`).
8. **Borrar un perfil** no borra su colección de memoria: enganchar `memory.reset_memory(perfil)` (decidir si es lo deseado o se conserva por retención).
9. **Regla en el prompt de perfiles** "los datos de las tools cambian: si piden información actual, vuelve a llamar la tool aunque ya la hayas consultado" (cambio chico en `personality._build_profile_prompt`; no toca a `elipse`).
10. **Launcher**: campos de `memory.max_items` / `memory.max_distance` por perfil (hoy se editan en el JSON del spec). `memory.max_distance` **no debe inventarse**: calibrarlo con datos reales de RBE.
11. **Decisión abierta de seguridad**: hoy una llave de anfitrión puede poner `"write": "never"` en su propio `spec.confirm` y saltarse la confirmación humana de sus tools de escritura (es lo que dice el plan). Si se quiere endurecer, que solo el admin pueda bajarla.
12. **Callback**: acepta `http` (útil en local); en producción conviene exigir `https`. No hay bloqueo de IPs privadas a propósito (RBE vive en la misma red); si el motor se expone, revisarlo.

### Reglas de trabajo para quien siga

Una fase por vez · para `elipse`, **ni un byte** de diferencia (los 6 tests dorados deben seguir verdes) · el perfil, `actor` y `session` **solo por código**, nunca por `arguments` · todo falla cerrado · comentarios y textos en español · antes de editar un archivo que no tengas, pídelo · tras cada cambio: `pytest tests -v` completo desde la raíz y actualizar este apartado.

---

