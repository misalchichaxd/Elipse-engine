from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "ELIPSE"
    ollama_model: str = "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M"
    # RTX 2050 (4 GB): Qwen3-4B Q4_K_M pesa ~2.5 GB y su KV cache cuesta ~144 KB/token (fp16)
    # o ~72 KB/token con OLLAMA_KV_CACHE_TYPE=q8_0. A 4096 de contexto son ~0.3 GB: entra
    # con holgura. 8192 ya roza el límite de 4 GB y se va a RAM/CPU (muy lento con 8 GB de RAM).
    ollama_num_ctx: int = 4096
    ollama_num_predict: int = 1536  # tope de tokens de salida: sin esto, si el modelo entra en loop no para hasta el timeout
    ollama_timeout_seconds: int = 120  # si Ollama se cae o se cuelga, fallar rápido en vez de esperar para siempre
    llm_keep_alive: str = "10m"  # cuánto queda el modelo cargado en la GPU tras cada llamada ("" = default de Ollama)
    workspace_dir: str = "workspace"

    # --- Modo compacto para modelos locales (Ollama) ---
    # Con contexto chico, el prompt completo (system ~3000 tokens + 14 tools ~3600) no cabe.
    # En modo compacto: system prompt resumido, solo las tools que el pedido necesita con
    # descripciones recortadas, historial y memoria recortados, y un ajuste final al presupuesto.
    ollama_compact_prompt: bool = True
    ollama_history_limit: int = 6          # mensajes de historial que ve el Agent Loop local
    ollama_history_msg_chars: int = 700    # tope por mensaje viejo (los 2 últimos admiten el doble)
    ollama_memory_k: int = 2               # recuerdos semánticos inyectados (vs memory_retrieval_k)
    ollama_memory_hit_chars: int = 350     # tope por recuerdo inyectado
    ollama_max_tools: int = 8              # tools máximas (las de MCP solo entran si sobra lugar)
    ollama_planner_num_predict: int = 200  # el plan JSON es chico: no necesita 1536 tokens de salida
    ollama_context_budget: float = 0.75    # fracción de num_ctx para el PROMPT; el resto queda para generar

    # --- Rol "vision": el modelo que MIRA imágenes y las describe en texto ---
    # A diferencia de code/research, NO hereda del general (Qwen3-4B-Instruct no tiene visión).
    # Con 4 GB de VRAM: qwen3-vl:2b (~1.9 GB). El 4b (~3.3 GB) no entra completo en la GPU.
    # VISION_PROVIDER vacío desactiva la visión. También puede ser anthropic/openai/gemini.
    vision_provider: str = "ollama"
    vision_ollama_model: str = "qwen3-vl:2b"
    vision_anthropic_api_key: str = ""
    vision_anthropic_model: str = ""
    vision_openai_api_key: str = ""
    vision_openai_base_url: str = ""
    vision_openai_model: str = ""
    vision_gemini_api_key: str = ""
    vision_gemini_model: str = ""
    vision_num_ctx: int = 4096       # una imagen de 768 px ocupa ~600-800 tokens de contexto
    vision_num_predict: int = 450    # tope de salida de la descripción
    vision_max_side: int = 768       # lado mayor al que se reduce la imagen antes de mirarla
    vision_max_chars: int = 900      # tope de la descripción que se le pasa al modelo principal
    vision_max_upload_mb: int = 10   # tope del adjunto de imagen que acepta /v1/chat
    vision_max_side_text: int = 1152  # capturas, documentos y gráficos se miran a más resolución (el texto chico
                                      # se pierde a 768 px). Si `ollama ps` muestra CPU/GPU repartido, bájalo a 960.
    vision_ocr_hint_chars: int = 600  # cuánto del texto del OCR se le muestra al modelo de visión como pista
    vision_cache_max: int = 300       # imágenes cuyo OCR/descripción se recuerdan (caché en SQLite)

    # --- OCR en CPU (ocr.py): lee el texto de las imágenes sin gastar VRAM ---
    # OCR_ENGINE: auto (rapidocr y si no tesseract) | rapidocr | tesseract | off
    # Instalar: pip install rapidocr-onnxruntime   (o, para Tesseract: pip install pytesseract + instalar
    # Tesseract con el idioma 'spa'; en Windows el instalador de UB-Mannheim).
    ocr_engine: str = "auto"
    ocr_langs: str = "spa+eng"        # solo Tesseract
    tesseract_cmd: str = ""           # ruta a tesseract.exe si no está en el PATH
    ocr_max_side: int = 2000          # las imágenes más grandes se reducen a este lado para el OCR (velocidad)
    ocr_min_confidence: float = 0.5   # líneas con menos confianza se descartan
    ocr_inline_chars: int = 1500      # texto del OCR que va directo al mensaje (el resto: read_image_text)
    ocr_page_chars: int = 2500        # tamaño de cada página de read_image_text
    ocr_history_chars: int = 300      # texto del OCR que se guarda en el historial del chat

    # --- Galería visual (gallery.py): personajes, mascotas, figuras u objetos que ELIPSE aprende ---
    gallery_dir: str = "personajes"   # subcarpeta del workspace con las imágenes de referencia
    gallery_max_refs: int = 4         # referencias por personaje (se descartan primero las de internet)
    gallery_web_refs: int = 2         # imágenes que baja learn_character(from_web=True)
    gallery_verify_max: int = 2       # candidatos que se verifican con el modelo de visión
    gallery_verify_refs: int = 2      # referencias que se le muestran junto a la imagen nueva
    gallery_match_distance: float = 0.8  # distancia coseno máxima (texto) para considerar candidato; más alto = más candidatos

    # --- Proveedores de modelos (capa de compatibilidad) ---
    # Opciones: "ollama" (local), "anthropic" (Claude), "openai" (OpenAI o cualquier
    # API compatible: OpenRouter, Mistral, Groq, LM Studio, vLLM... vía openai_base_url),
    # "gemini" (Google AI).
    default_provider: str = "ollama"
    cloud_timeout_seconds: int = 120

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    anthropic_max_tokens: int = 4096
    anthropic_base_url: str = "https://api.anthropic.com"

    openai_api_key: str = ""  # puede ir vacío con servidores locales compatibles
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = ""  # sin default a propósito: se elige explícitamente

    # --- Gemini (Google AI) ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_max_tokens: int = 8192
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    # --- Proveedor por rol: "code" (código) e "research" (investigación) pueden
    # pensar con un proveedor y modelo distintos al general. Si "{rol}_provider"
    # queda vacío, el rol HEREDA por completo la configuración general de arriba
    # (mismo comportamiento que antes de que existieran estos overrides).
    # Si se define un proveedor para el rol, cada campo "{rol}_{proveedor}_*"
    # vacío también hereda su equivalente general (p. ej. code_anthropic_model
    # vacío usa anthropic_model) — así no hay que repetir todo para cambiar
    # solo una cosa.
    code_provider: str = ""
    code_ollama_model: str = "qwen2.5-coder:3b"
    code_anthropic_api_key: str = ""
    code_anthropic_model: str = ""
    code_openai_api_key: str = ""
    code_openai_base_url: str = ""
    code_openai_model: str = ""
    code_gemini_api_key: str = ""
    code_gemini_model: str = ""

    research_provider: str = ""
    # sin capacidad de "thinking" por default; evita el bug de think=False en qwen3
    research_ollama_model: str = "phi3:mini"
    research_anthropic_api_key: str = ""
    research_anthropic_model: str = ""
    research_openai_api_key: str = ""
    research_openai_base_url: str = ""
    research_openai_model: str = ""
    research_gemini_api_key: str = ""
    research_gemini_model: str = ""

    # --- MCP (Model Context Protocol): conectar herramientas externas ---
    mcp_config_file: str = "mcp_servers.json"  # relativo a la raíz del proyecto; si no existe, MCP queda apagado
    mcp_connect_timeout_seconds: int = 30
    mcp_call_timeout_seconds: int = 60
    mcp_max_description_chars: int = 400  # recorta descripciones largas: cuidan el contexto del modelo
    mcp_max_output_chars: int = 10000

    # --- Fase 4: memoria semántica (Chroma) ---
    chroma_dir: str = "chroma_data"
    chroma_collection: str = "elipse_memory"
    memory_max_items: int = 500
    memory_prunable_types: list[str] = ["research"]  # tipos que SÍ se borran automáticamente al llenarse
    memory_retrieval_k: int = 4
    research_summary_max_chars: int = 800

    # --- Pipeline por plan (Fase 7): el orquestador planifica UNA vez y el código
    # ejecuta la cadena de tools (research / search_images / write_code), en vez de
    # que el modelo chico vaya razonando ronda a ronda. Pensado para que modelos
    # locales 4B-8B no se "cansen" a mitad del Agent Loop (ver diseño en el chat de
    # planificación). Si el plan sale vacío o inválido, cae al Agent Loop de siempre.
    pipeline_enabled: bool = True
    pipeline_step_timeout_seconds: int = 90  # tope por paso paralelo (research / search_images)
    # Si es True, se le pide al orquestador 1-2 frases de cierre en lenguaje natural;
    # si es False (default), el cierre es una frase por plantilla, sin gastar otra
    # llamada al modelo. Con modelos chicos, False es más confiable.
    pipeline_close_with_model: bool = False

    # --- Voz (core/voice): TTS opcional, por proveedores intercambiables ---
    # Lo que cambies desde la interfaz (activado, modo, proveedor, voice id, ganancia) se guarda
    # en SQLite y pisa a estos valores. Las claves van solo en .env.
    voice_enabled: bool = True
    voice_mode: str = "smart"            # off | manual | smart | always
    voice_routing: str = "failover"      # manual | failover | round_robin
    voice_providers: str = "fish,local"  # orden de prioridad, separado por comas
    voice_cloud_allowed: bool = True     # False = modo privado: solo proveedores locales
    voice_max_chars: int = 400
    voice_spoken_fallback: str = "Listo, te dejé el resultado en pantalla."
    voice_cooldown_seconds: int = 300
    voice_fail_threshold: int = 3        # fallos seguidos antes de apartar a un proveedor
    voice_timeout_seconds: int = 30      # lectura máxima entre pedazos de audio

    voice_fish_api_key: str = ""
    voice_fish_model: str = "s2.1-pro-free"
    voice_fish_voice_id: str = ""
    voice_fish_gain: float = 1.0
    # Calidad de la voz de Fish. latency: "normal" = mejor calidad, "balanced" = mas rapido pero puede sonar mas plano.
    voice_fish_latency: str = "balanced"
    voice_fish_temperature: float = 0.7     # 0.0-1.0: aleatoriedad (mas alto = mas expresivo, menos estable)
    voice_fish_top_p: float = 0.7           # 0.0-1.0

    voice_local_url: str = "http://127.0.0.1:8002/tts"
    voice_local_voice_id: str = ""
    voice_local_gain: float = 1.0
    voice_local_mime: str = "audio/mpeg"  # o audio/wav, según lo que devuelva tu servicio local

    class Config:
        env_file = ".env"

settings = Settings()
