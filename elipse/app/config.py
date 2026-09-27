from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "ELIPSE"
    ollama_model: str = "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M"
    ollama_num_ctx: int = 2048  # contexto fijo y chico a propósito: libera VRAM para meter más capas en GPU
    ollama_num_predict: int = 1536  # tope de tokens de salida: sin esto, si el modelo entra en loop no para hasta el timeout
    ollama_timeout_seconds: int = 120  # si Ollama se cae o se cuelga, fallar rápido en vez de esperar para siempre
    workspace_dir: str = "workspace"

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

    class Config:
        env_file = ".env"

settings = Settings()