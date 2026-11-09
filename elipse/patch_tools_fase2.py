import sys
p = sys.argv[1]
s = open(p, "rb").read().decode("utf-8")
assert "\r\n" in s
def crlf(t): return t.replace("\r\n", "\n").replace("\n", "\r\n")
def rep(old, new):
    global s
    old, new = crlf(old), crlf(new)
    assert s.count(old) == 1, old[:60]
    s = s.replace(old, new)

rep("from app.core import gallery, vision\n",
    "from app.core import gallery, vision\nfrom app.core.profiles import DEFAULT, PROFILE_ARG_KEYS, Capabilities\n")

rep('''def get_tools_schema() -> list:
    """
    Catálogo COMPLETO que ve el modelo: herramientas propias + las que aporten los
    servidores MCP conectados (ver mcp_client.py). Se recalcula en cada uso porque
    los servidores MCP se conectan después de importar este módulo.
    """
    return TOOLS_SCHEMA + mcp_client.manager.tool_schemas()
''', '''def get_tools_schema(profile=DEFAULT) -> list:
    """
    Catálogo COMPLETO que ve el modelo: herramientas propias + las que aporten los
    servidores MCP conectados (ver mcp_client.py). Se recalcula en cada uso porque
    los servidores MCP se conectan después de importar este módulo.
    Con un perfil ≠ 'elipse' (E1): solo lo permitido (spec ∩ caps) + las tools del anfitrión.
    """
    schema = TOOLS_SCHEMA + mcp_client.manager.tool_schemas()
    return schema if profile.is_default else Capabilities(profile).filter_schema(schema)
''')

rep('''def select_tools_for_message(message: str, schema: list, max_tools: int = 8,
                             extra: tuple = (), history_has_code: bool = False) -> list:''',
'''def select_tools_for_message(message: str, schema: list, max_tools: int = 8,
                             extra: tuple = (), history_has_code: bool = False, profile=DEFAULT) -> list:''')

rep('''    low = (message or "").lower()
    wanted = set(_CORE_TOOLS) | set(extra)''', '''    caps = Capabilities(profile)
    if not caps.is_default:
        schema = caps.filter_schema(schema)   # E2: filtrar ANTES de recortar a max_tools
    low = (message or "").lower()
    wanted = set(_CORE_TOOLS) | set(extra)''')

rep('''            if name in wanted:
                chosen.append(tool)
        else:
            mcp.append((name, tool))''', '''            if name in wanted:
                chosen.append(tool)
        elif caps.is_host_tool(name):
            chosen.append(tool)               # las del anfitrión (ya filtradas) entran siempre
        else:
            mcp.append((name, tool))''')

rep('''def get_tools_schema_compact(message: str, max_tools: int = 8, extra: tuple = (),
                             history_has_code: bool = False) -> list:
    chosen = select_tools_for_message(message, get_tools_schema(), max_tools, tuple(extra), history_has_code)''',
'''def get_tools_schema_compact(message: str, max_tools: int = 8, extra: tuple = (),
                             history_has_code: bool = False, profile=DEFAULT) -> list:
    chosen = select_tools_for_message(message, get_tools_schema(profile), max_tools, tuple(extra),
                                      history_has_code, profile)''')

rep('''def execute_tool(name: str, arguments: dict):
    if name in TOOL_FUNCTIONS:''', '''def execute_tool(name: str, arguments: dict, profile=DEFAULT):
    caps = Capabilities(profile)
    if not caps.is_default:
        # E3: compuerta final. El perfil llega por código (R13): cualquier 'profile/actor/session'
        # que el modelo mande en los argumentos se descarta.
        if not caps.tool_allowed(name):
            return f"Error: herramienta '{name}' no habilitada en este perfil."
        arguments = {k: v for k, v in (arguments or {}).items() if k not in PROFILE_ARG_KEYS}
        if caps.is_host_tool(name):
            return "Error: las herramientas del anfitrión aún no están disponibles (llegan con el callback)."
    if name in TOOL_FUNCTIONS:''')
open(p, "wb").write(s.encode("utf-8"))
print("tools.py parcheado")
