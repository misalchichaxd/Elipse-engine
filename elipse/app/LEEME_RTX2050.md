# ELIPSE Engine — ajuste de contexto para RTX 2050 (4 GB VRAM / 8 GB RAM)

## 1. Cómo aplicar
Copia estos 6 archivos encima de los de tu proyecto (misma ruta):
`config.py`, `core/core.py`, `core/providers.py`, `core/personality.py`, `core/tools.py`, `core/pipeline.py`.
`cambios.diff` tiene todos los cambios por si prefieres revisarlos antes.

## 2. Variables de Ollama (Windows, una sola vez, luego reinicia Ollama)
```
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_KV_CACHE_TYPE q8_0
setx OLLAMA_MAX_LOADED_MODELS 1
setx OLLAMA_NUM_PARALLEL 1
setx OLLAMA_KEEP_ALIVE 10m
```
- Flash attention + KV q8_0: el cache de contexto pesa la mitad (~72 KB/token en Qwen3-4B).
- MAX_LOADED_MODELS=1 y NUM_PARALLEL=1: con 4 GB nunca caben dos modelos ni dos contextos a la vez.

## 3. Presupuesto de VRAM aproximado (Qwen3-4B Q4_K_M)
| Pieza | VRAM |
|---|---|
| Pesos del modelo | ~2.5 GB |
| KV cache a 4096 (q8_0) | ~0.3 GB |
| Buffers de cómputo | ~0.3 GB |
| Pantalla de Windows | ~0.3-0.5 GB |
| **Total** | **~3.4-3.6 GB** (entra) |

A 8192 el KV sube a ~0.6 GB y se acerca al límite: úsalo solo si `ollama ps` sigue mostrando `100% GPU`.
Verifica siempre con `ollama ps`: si dice algo como `30%/70% CPU/GPU`, baja `ollama_num_ctx`.

## 4. Un solo modelo local para todos los roles (recomendado)
Hoy usas 3 modelos distintos (Qwen3-4B, qwen2.5-coder:3b, phi3:mini). En 4 GB solo cabe uno
a la vez, así que cada cambio de rol descarga y recarga un modelo desde disco (con 8 GB de RAM
tampoco queda en caché). En tu `.env`:
```
CODE_OLLAMA_MODEL=hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
RESEARCH_OLLAMA_MODEL=hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
```
Si prefieres mantener el coder, pruébalo y compara los tiempos; el costo es el swap en cada turno
que mezcla investigación + código.

## 5. Qué hace el parche
1. Log `ollama ctx: prompt=N salida=M de 4096 (X%)` en cada llamada (WARNING desde 85%).
2. `ollama_num_ctx` 2048 -> 4096.
3. Solo para Ollama: catálogo de tools filtrado por el pedido (6-8 en vez de 14) y descripciones recortadas.
4. Solo para Ollama: system prompt compacto (~10.500 -> ~2.300 caracteres) con las mismas reglas críticas.
5. Historial local de 6 mensajes recortados, memoria semántica de 2 recuerdos recortados,
   y `_fit_to_context` que recorta antes de llamar al modelo (nunca el system ni tu mensaje).
6. El planificador usa `num_predict=200` en vez de 1536.

Los proveedores de nube (Anthropic/OpenAI/Gemini) siguen con el prompt y las tools completos.
Se desactiva todo con `OLLAMA_COMPACT_PROMPT=false`.
