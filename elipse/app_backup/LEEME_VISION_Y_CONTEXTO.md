# ELIPSE Engine — parche acumulado: contexto (RTX 2050) + visión

Este paquete incluye TODO lo del parche anterior (contexto) más la visión.
Copia los 9 archivos encima de los de tu proyecto, misma ruta:
`config.py`, `main.py`, `core/core.py`, `core/providers.py`, `core/personality.py`,
`core/tools.py`, `core/pipeline.py`, `core/tasks.py` y `core/vision.py` (este último es nuevo).
`cambios.diff` muestra todo por si quieres revisar antes de pisar.

## Cómo funciona la visión
Un cuarto rol, `vision`, con su propio modelo (como code y research). El modelo principal
sigue siendo Qwen3-4B y no ve imágenes: cuando adjuntas una, el CÓDIGO se la pasa al modelo de
visión, que la describe en texto, y el principal razona sobre esa descripción.

- Con 4 GB no caben a la vez el principal (~3.3 GB) y el de visión; Ollama los cambia solo
  (MAX_LOADED_MODELS=1). Ese cambio se paga SOLO cuando hay una imagen: espera unos segundos extra.
- La imagen se reduce a 768 px de lado mayor antes de mirarla, para no gastar el contexto.
- Si el modelo de visión falla o no está, el modelo principal recibe un aviso y se lo dice al
  usuario, en vez de inventar lo que hay en la imagen.
- `describe_image(path, question)` es una tool nueva para preguntas posteriores sobre una imagen
  que ya está en el workspace.
- Con una imagen adjunta se salta el pipeline por plan y no se ofrece `search_images`.

## Instalación (una vez)
```
pip install pillow
ollama pull qwen3-vl:2b
```
Pillow reduce las imágenes; sin él solo se aceptan imágenes de menos de 2 MB.
El `.env` ya no necesita nada (los valores por defecto son estos). Para cambiarlo:
```
VISION_PROVIDER=ollama            # vacío = desactiva la visión; también anthropic / openai / gemini
VISION_OLLAMA_MODEL=qwen3-vl:2b
```
Los proveedores de nube también soportan imágenes (VISION_PROVIDER=anthropic + VISION_ANTHROPIC_API_KEY
y VISION_ANTHROPIC_MODEL, etc.), útil si algún día quieres más calidad que un modelo de 2B.

## Probar
Con el servidor corriendo:
```
python probar_vision.py foto.jpg "¿qué ves en esta foto?" elp_TU_API_KEY
```
En el log busca `ollama ctx: prompt=... de 4096` (WARNING si pasa del 85%) y comprueba con
`ollama ps` que `qwen3-vl:2b` queda en `100% GPU`.

## Para tu cliente (app/web)
`/v1/chat` ya acepta imágenes en `attachments`: `{"filename": "x.png", "content": "<base64>"}`
(vale también con prefijo `data:image/png;base64,`). Formatos: png, jpg, jpeg, webp, gif, bmp; máx. 10 MB
(`VISION_MAX_UPLOAD_MB`). El servidor comprueba por los bytes que de verdad sea una imagen.
Si tu cliente solo permite adjuntar texto, hay que cambiar el selector de archivos para leer imágenes como base64.

## Límites a tener en cuenta
- Un modelo de 2B es más flojo que uno grande: puede equivocarse con detalles, texto pequeño o
  fotos muy cargadas. Para OCR de documentos serios conviene un modelo más grande o uno de nube.
- Qwen3-VL en tamaños 2b/4b a veces es irregular según la guía de PhotoPrism; si ves descripciones
  raras, prueba otra variante o sube `VISION_MAX_SIDE` a 1024 si te sobra VRAM.
- La descripción se guarda en el historial (máx. ~900 caracteres) para que los seguimientos la tengan;
  el historial local recorta mensajes viejos.
