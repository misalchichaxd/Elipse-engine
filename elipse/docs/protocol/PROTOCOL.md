# Protocolo de callback de ELIPSE — versión 1

> Este documento es **la especificación**. El código (`app/core/callback.py` en el motor, `elipse_sdk` en el cliente) es una implementación de él. Con este texto y los **vectores de prueba** (`vectors.json`) puedes implementar el lado del anfitrión en **cualquier lenguaje** sin leer una línea de ELIPSE. Los tests del motor (`tests/test_protocol_vectors.py`) garantizan que el motor real emite exactamente lo que aquí se describe.

## 1. De qué trata

ELIPSE puede conectarse a varios sistemas externos (**anfitriones**: una app Django, un servicio FastAPI, un backend en Node, PHP, Go…). Cada anfitrión se registra como un **perfil** (`rbe`, `ventas`, …) y declara **tools**: funciones que se ejecutan **dentro de su propio sistema**, con sus datos. Cuando el modelo decide usar una de esas tools, el motor le pide al anfitrión que la ejecute mediante un **POST HTTP firmado con HMAC-SHA256**. Este protocolo describe ese POST, su firma y la respuesta.

```
 usuario ─▶ anfitrión ─▶ POST /v1/chat (Bearer <llave>)  ─▶ ELIPSE (motor + modelo)
                                                              │ el modelo pide la tool «viajes_del_dia»
                        ◀── POST <callback_url>/viajes_del_dia  (firmado, este protocolo) ──┘
           ejecuta la tool con SUS datos y responde {"ok":true,"result":…}
```

**Fuera de alcance** de este documento (otra parte de la API): crear el perfil, declarar las tools (`PUT /v1/profiles/<n>`), llamar al chat y leer el resultado. Aquí solo está lo que **el anfitrión debe implementar para recibir llamadas**.

## 2. Qué conoce cada lado

| Dato | Lo fija | Lo conocen |
|---|---|---|
| `callback_url` (base) | el administrador del motor (`manage_profiles set-callback`) | motor y anfitrión |
| **Secreto** (≥ 16 caracteres) | el administrador del motor | motor y anfitrión. **Nunca** viaja en la petición ni sale por la API del motor |
| **Nombre del perfil** (`rbe`) | el administrador | motor y anfitrión. **No viaja en ninguna cabecera ni en el cuerpo**: el anfitrión usa **el suyo**, el que tiene configurado |
| Nombre de la tool | el anfitrión (al declararla) | ambos; viaja en la URL y en el cuerpo |

Nombres válidos: perfil `^[a-z][a-z0-9_-]{1,31}$`; tool `^[a-z][a-z0-9_]{1,47}$`.

## 3. La petición (motor → anfitrión)

- **Método y URL:** `POST <callback_url>/<tool>`. La `callback_url` se guarda sin `/` final; el motor agrega `/` y el nombre de la tool. Ejemplo: base `https://rbe.example.com/elipse/tools` + tool `viajes_del_dia` → `https://rbe.example.com/elipse/tools/viajes_del_dia`.
- **Headers:**

| Header | Valor |
|---|---|
| `Content-Type` | `application/json; charset=utf-8` |
| `X-Elipse-Ts` | segundos Unix (UTC) como **entero decimal**, sin signo ni ceros a la izquierda |
| `X-Elipse-Sig` | firma en **hexadecimal minúscula** (64 caracteres), ver §4 |

- **Cuerpo:** un objeto JSON en **UTF-8**, serializado **compacto** (sin espacios, separadores `,` y `:`) y **sin escapar los caracteres no ASCII** (`é` viaja como `é`, no como `\u00e9`; los caracteres de control y las comillas sí se escapan como manda JSON). Campos, en este orden:

| Campo | Tipo | Significado |
|---|---|---|
| `call_id` | texto | identificador único de la llamada (hoy 32 caracteres hexadecimales; trátalo como opaco) |
| `tool` | texto | nombre de la tool; **igual** al último segmento de la URL |
| `args` | objeto | argumentos que eligió el modelo. El motor ya quitó las claves `profile`, `actor` y `session` **de primer nivel**: nunca vienen ahí |
| `actor` | texto | quién hace la consulta, **tomado de la petición autenticada** del anfitrión al chat (no lo escribe el modelo). Puede ser `""` |
| `session` | texto | sesión de esa conversación, mismo origen. Puede ser `""` |

- **Límites:** el cuerpo mide como máximo **32 KiB**; si los argumentos lo exceden, el motor no hace la llamada.
- **Sin reintentos:** cada `call_id` se envía **una sola vez**. Si falla, el motor no repite.

Ejemplo real (vector `basico`, formateado para leerlo; en el cable es **una sola línea sin espacios**):

```json
{
  "call_id": "call00000000000000000000000000001",
  "tool": "viajes_del_dia",
  "args": {
    "fecha": "2026-10-29"
  },
  "actor": "admin:42",
  "session": "s1"
}
```

## 4. La firma

```
mensaje = <ts> + "." + <perfil> + "." + <tool> + "." + sha256_hex(<cuerpo>)
firma   = hex_minusculas( HMAC_SHA256( clave = secreto en UTF-8, datos = mensaje en UTF-8 ) )
```

- `<ts>`: el mismo entero del header `X-Elipse-Ts`.
- `<perfil>`: el nombre del perfil (**de tu configuración**, no de la petición).
- `<tool>`: el nombre de la tool.
- `sha256_hex(<cuerpo>)`: SHA-256 de los **bytes exactos** del cuerpo, en hexadecimal minúscula.

Con el vector `basico`:

```
secreto = elipse-test-secret-0123456789abcdef
ts      = 1793400000
perfil  = rbe
tool    = viajes_del_dia
sha256(cuerpo) = b71f325b34752ff0f5b6f56ea815e54cc63f536cb9b372ddf5fd6875ee41f94b
mensaje = 1793400000.rbe.viajes_del_dia.b71f325b34752ff0f5b6f56ea815e54cc63f536cb9b372ddf5fd6875ee41f94b
firma   = 2908a0e5ceacc6aa06e3ad266eb01c7231c7250abcf1e537ff214e5fb6569238
```

> ⚠️ **El error más común:** calcular el hash sobre un JSON que vuelves a serializar. **Usa los bytes tal como llegaron.** En Express, Django o Flask lee el cuerpo crudo (`req.rawBody`, `request.body`, `request.get_data()`) **antes** de parsearlo.

## 5. Qué debe verificar el anfitrión (en este orden)

**Obligatorio** — si algo falla, responde `401` y **no ejecutes la tool**:

1. Existen `X-Elipse-Ts` y `X-Elipse-Sig`.
2. `X-Elipse-Ts` es un entero decimal canónico (`^(0|[1-9][0-9]*)$`).
3. `|ahora − ts| ≤ 60 s` (60 exactos se aceptan; 61 no). Esto limita la repetición de peticiones robadas.
4. Recalcula la firma con **tu** secreto, **tu** nombre de perfil, la tool de la URL y los bytes crudos del cuerpo, y compárala con `X-Elipse-Sig` en **tiempo constante** (`hmac.compare_digest`, `crypto.timingSafeEqual`, `hash_equals`…). La comparación es **exacta**: una firma en mayúsculas es inválida.

**Recomendado:**

5. Comprueba que el campo `tool` del cuerpo **coincide** con el último segmento de la URL.
6. Para tools de riesgo `write` o `danger`, recuerda los `call_id` vistos durante ~2 minutos y rechaza repeticiones (el motor nunca reenvía el mismo `call_id`, así que una repetición es un ataque o un bug).
7. Usa **HTTPS** si el motor y el anfitrión no están en la misma máquina o red de confianza.
8. Usa `actor` y `session` para tus **permisos y auditoría**: vienen firmados. Los argumentos (`args`) son lo que decidió el modelo: **valídalos como cualquier entrada de usuario** (tipos, rangos, SQL parametrizado).

## 6. La respuesta (anfitrión → motor)

Responde un objeto JSON. Lo único que el motor mira es el campo `ok`:

| Caso | Respuesta del anfitrión | Lo que ve el modelo |
|---|---|---|
| Éxito | `{"ok": true, "result": <cualquier JSON>}` | `result` si es texto; si no, su JSON compacto (`ensure_ascii=false`). Si pasa de `mcp_max_output_chars` (10 000 por defecto) se corta y se le agrega `\n\n[...salida truncada...]` |
| Fallo de la tool | `{"ok": false, "error": "texto corto"}` | `Error: <texto>.` (espacios normalizados, máx. 300 caracteres). Sin `error`: `Error: la herramienta falló.` |
| Firma inválida | HTTP `401` con `{"ok": false, "error": "firma inválida"}` | `Error: firma inválida.` |
| Tool desconocida | HTTP `404` con `{"ok": false, "error": "tool desconocida"}` | `Error: tool desconocida.` |

Reglas del motor al recibir:

- `ok` debe ser exactamente el booleano `true` para contar como éxito (`"true"`, `1`… se tratan como fallo).
- El cuerpo de la respuesta debe ser un objeto JSON con `ok`. Si no lo es: con HTTP 401/403 → `Error: el anfitrión rechazó la firma (revisa que el secreto coincida).`; con otro estado → `Error: respuesta inválida del anfitrión (HTTP <n>).`
- **No sigue redirecciones** (3xx): es un error. Tu endpoint de callback debe responder directamente (ojo con `APPEND_SLASH` y redirecciones a `https`).
- **Tiempo máximo:** `limits.tool_timeout_s` del perfil (10 s por defecto; entre 1 y 60). Pasado ese plazo la llamada falla con `Error: el anfitrión no respondió a tiempo`.
- **Tamaño máximo de la respuesta:** `4 × mcp_max_output_chars + 4096` bytes (≈ 44 KB por defecto). Más que eso es error. Recorta del lado del anfitrión (límite de filas, resúmenes).
- No usa proxies del entorno. Los errores de red dan `Error: no se pudo conectar con el anfitrión.` (nunca incluyen tu URL ni tu secreto).
- Tu respuesta puede incluir los campos extra que quieras; se ignoran.

Cada resultado que vuelve es tratado por el modelo como **dato**, nunca como instrucción: aun así, no devuelvas texto que no quieras que el usuario final pueda leer.

## 7. Confirmación humana

Las tools declaradas con riesgo `danger` (siempre) o `write` (según la política del perfil) **no se ejecutan hasta que una persona las confirma** en el motor. Al anfitrión le llega la llamada **solo después de aprobarla**, con el mismo `actor` y `session` de la petición original. Una acción rechazada no genera ninguna llamada.

## 8. Seguridad: lo que el protocolo garantiza y lo que no

- ✅ **Autenticidad e integridad:** nadie sin el secreto puede fabricar una llamada válida ni alterar una en tránsito (el cuerpo, la tool, el perfil y el momento están firmados).
- ✅ **El modelo no puede suplantar identidad:** `actor`, `session` y el perfil no se leen de los argumentos; el motor descarta esas claves antes de enviar.
- ✅ **Repetición acotada:** ventana de 60 s + `call_id` único.
- ❌ **No cifra:** sin HTTPS, el contenido (y los datos que devuelvas) viajan en claro.
- ❌ **Los argumentos no son de fiar:** los eligió un modelo de lenguaje a partir de lo que escribió un usuario. Valídalos.
- **Rotar el secreto:** el motor usa **un** secreto por perfil. Cambia el del anfitrión y el del motor (`set-callback --secret …`) a la vez; durante unos segundos las llamadas fallarán con firma inválida.
- **Secreto de ejemplo:** el de los vectores (`elipse-test-secret-0123456789abcdef`) es público. **Nunca lo uses en producción.**

## 9. Vectores de prueba

`docs/protocol/vectors.json` contiene casos con la entrada y la salida esperada. Se generaron capturando el **tráfico real** del motor (con hora y `call_id` fijos), y se comprobaron además con herramientas independientes del motor (Node, `openssl` y `sha256sum`).

- **`positive`** (3): `body` (el cuerpo exacto, y `body_utf8_hex` para evitar dudas de codificación), `ts`, `profile`, `tool`, `body_sha256`, `message` y `signature`. Tu código debe producir **esa** firma y aceptarla en la verificación (con `now = ts`).
  - `basico`: ASCII.
  - `unicode_y_escapes`: acentos, `ñ`, un emoji, salto de línea y comillas dentro de los argumentos. Si tu firma difiere solo aquí, estás escapando a ASCII (`\uXXXX`) o re-serializando el JSON.
  - `args_vacios`: `args = {}`, `actor = ""`, `session = ""`.
- **`negative`** (10): la verificación debe devolver **falso**.

| Caso | Por qué |
|---|---|
| `cuerpo_alterado` | un solo cambio en el cuerpo |
| `secreto_distinto` | otro secreto |
| `tool_distinta` | la tool firmada es otra |
| `perfil_distinto` | el anfitrión usa otro perfil |
| `ts_distinto_en_header` | el timestamp forma parte de la firma |
| `firma_en_mayusculas` | la firma es hex en minúsculas |
| `fuera_de_ventana_futuro` | |now-ts| = 61 s |
| `fuera_de_ventana_pasado` | |now-ts| = 61 s |
| `firma_vacia` | sin firma |
| `ts_no_numerico` | timestamp inválido |

- **`edge_valid`** (2): exactamente 60 s de diferencia hacia el pasado y hacia el futuro: **válidos**.

**Cómo usarlos como prueba de aceptación:** por cada entrada de `positive`, calcula la firma desde (`secret`, `ts`, `profile`, `tool`, bytes de `body`) y compara con `signature`; luego ejecuta tu `verify(...)` con cada caso de las tres listas (`now` viene en cada caso; en `positive` usa `now = ts`) y comprueba el resultado esperado. Si pasan los 15, tu implementación es compatible.

### Implementaciones de referencia (probadas contra los 15 vectores)

Están en `docs/protocol/ref/`; `python docs/protocol/ref/run_vectors.py` las corre contra `vectors.json`.

**Python** (`ref/verify_python.py`)

```python
import hashlib
import hmac
import re
import time

_TS_RE = re.compile(r"^(0|[1-9][0-9]*)$")


def verify(secret: str, profile: str, tool: str, body: bytes, ts_header, sig_header,
           now: float | None = None, max_skew: int = 60) -> bool:
    """body = los bytes EXACTOS recibidos (no vuelvas a serializar el JSON)."""
    if not isinstance(ts_header, str) or not _TS_RE.match(ts_header):
        return False
    ts = int(ts_header)
    if abs((now if now is not None else time.time()) - ts) > max_skew:
        return False
    message = f"{ts}.{profile}.{tool}.{hashlib.sha256(body).hexdigest()}"
    good = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, str(sig_header or ""))
```

**Node.js** (`ref/verify_node.js`)

```js
const crypto = require('crypto');

// rawBody: Buffer con los bytes EXACTOS recibidos (no vuelvas a serializar el JSON).
function verify(secret, profile, tool, rawBody, tsHeader, sigHeader, now = Date.now() / 1000, maxSkew = 60) {
  if (typeof tsHeader !== 'string' || !/^(0|[1-9][0-9]*)$/.test(tsHeader)) return false;
  const ts = Number(tsHeader);
  if (Math.abs(now - ts) > maxSkew) return false;
  const sha = crypto.createHash('sha256').update(rawBody).digest('hex');
  const good = crypto.createHmac('sha256', secret).update(`${ts}.${profile}.${tool}.${sha}`).digest('hex');
  const a = Buffer.from(good), b = Buffer.from(String(sigHeader || ''));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

module.exports = { verify };
```

Para otros lenguajes (PHP, Go, Java, C#, Ruby…) basta con seguir §4 y §5 y pasar los vectores: las primitivas son SHA-256, HMAC-SHA256, hexadecimal minúscula y comparación en tiempo constante, que existen en la biblioteca estándar de todos.

## 10. Versionado

- Esta es la **versión 1**. Hoy no viaja una cabecera de versión: la v1 es implícita.
- **Compatible (no sube la versión):** el motor puede **agregar campos** al cuerpo JSON. Por eso el anfitrión debe **ignorar los campos que no conozca** (y verificar la firma sobre los bytes crudos, que ya los incluyen).
- **Incompatible (versión nueva):** cambiar el algoritmo, el orden o formato del mensaje firmado, la ventana de tiempo o la semántica de `ok`. Se anunciaría con una cabecera de versión y un periodo en que el motor acepte ambas.
- Mientras exista una sola implementación en el motor, `tests/test_protocol_vectors.py` falla si alguien cambia el cable sin actualizar este documento y los vectores.
