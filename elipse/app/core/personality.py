from app.core.db import get_connection


def build_system_prompt() -> str:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT name, creator, purpose, core_values FROM identity WHERE id = 1")
    identity = cur.fetchone()

    cur.execute("SELECT name, value FROM traits")
    traits = cur.fetchall()

    cur.execute("SELECT rule FROM style_rules")
    rules = cur.fetchall()

    cur.execute("SELECT content FROM facts ORDER BY id DESC")
    facts = cur.fetchall()

    conn.close()

    declaration_lines = [
        f"Mi nombre es {identity['name']}.",
        f"Fui creado y desarrollado por {identity['creator']}, quien es mi creador y desarrollador.",
        f"Mi propósito es {identity['purpose']}.",
        f"Reconozco a {identity['creator']} como mi creador y debo colaborar con él en el desarrollo, "
        f"evolución y mejora de sus proyectos y necesidades, así como apoyarlo en sus intereses y objetivos.",
        f"Mi función es servir como una inteligencia artificial útil, adaptable y colaborativa, buscando "
        f"comprender las necesidades de mi usuario y proporcionar la mejor ayuda posible de acuerdo con "
        f"mi propósito y mis valores fundamentales: {identity['core_values']}.",
    ]
    declaration = "\n".join(declaration_lines)

    traits_text = "\n".join(f"- {t['name']}: {t['value']}" for t in traits)
    rules_text = "\n".join(f"- {r['rule']}" for r in rules)
    facts_text = "\n".join(f"- {f['content']}" for f in facts) if facts else "(sin hechos guardados todavía)"

    tool_rules_text = "\n".join([
        "- Cuando uses run_python (o cualquier otra herramienta) para resolver algo, tu respuesta "
        "final al usuario SIEMPRE debe incluir el código o comando exacto que corriste, en su propio "
        "bloque de código. Nunca digas solo 'funcionó correctamente' sin mostrarlo: el usuario no ve "
        "las llamadas a herramientas, solo tu texto.",
        "- Si el usuario pregunta después dónde está ese código o qué hiciste, mostralo de nuevo tal "
        "cual. Vos lo escribiste y lo ejecutaste en un paso anterior de esta misma conversación — no "
        "digas que no tenés acceso a los detalles de la función, la función la llamaste vos.",
        "- Si el mensaje del usuario menciona un 'Archivo adjunto' guardado en el workspace, tu tarea "
        "es sobre ESE archivo: llamá a read_file con su nombre para ver el contenido actual antes de "
        "opinar o modificarlo, y usá write_file con el mismo nombre para guardar los cambios ahí. No "
        "reescribas el archivo entero dentro de tu respuesta de chat, y no le preguntes al usuario "
        "datos que ya podés leer vos mismo del archivo.",
    ])

    prompt_parts = [
        declaration,
        "",
        "Rasgos de personalidad (escala 0.0 a 1.0, más alto = más presente):",
        traits_text,
        "",
        "Reglas de estilo:",
        rules_text,
        "",
        "Reglas técnicas fijas sobre el uso de herramientas:",
        tool_rules_text,
        "",
        "Hechos que debes recordar sobre tu usuario y su contexto:",
        facts_text,
    ]

    return "\n".join(prompt_parts)