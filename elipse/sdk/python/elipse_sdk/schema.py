"""
Esquema JSON de una tool a partir de los type hints y el docstring de la función.

- descripción  = primera línea del docstring (el motor la corta a 300 caracteres)
- parámetros   = type hints (str, int, float, bool, list[X], dict, X | None, Literal[...], Enum)
- requeridos   = los que no tienen valor por defecto
- "Args:" del docstring (estilo Google) = descripción de cada parámetro (opcional)
- un parámetro llamado `ctx` o anotado como Context NO entra al esquema: lo inyecta el SDK
"""

import enum
import inspect
import json
import re
import types
import typing
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

MAX_PARAMS_JSON = 4000      # límite del motor (profiles._norm_host_tool)
MAX_DESCRIPTION = 300


class SchemaError(TypeError):
    """El tipo no se puede expresar como esquema JSON (se detecta al decorar, no en producción)."""


def first_line(doc: str | None) -> str:
    for line in (doc or "").strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _arg_docs(doc: str | None) -> dict[str, str]:
    """Lee la sección 'Args:' estilo Google: `nombre: texto` (o `nombre (tipo): texto`)."""
    out: dict[str, str] = {}
    if not doc:
        return out
    in_args = False
    for raw in inspect.cleandoc(doc).splitlines():
        line = raw.rstrip()
        if re.match(r"^(Args|Arguments|Parámetros|Parametros):\s*$", line):
            in_args = True
            continue
        if in_args:
            if line and not line.startswith((" ", "\t")):
                break
            m = re.match(r"^\s+(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip()
    return out


def type_to_schema(tp: Any) -> tuple[dict, bool]:
    """→ (esquema JSON, admite_null)."""
    origin = get_origin(tp)
    args = get_args(tp)

    if tp is Any or tp is inspect.Parameter.empty:
        raise SchemaError("falta el type hint (o es Any): declara un tipo concreto")
    if origin in (Union, getattr(types, "UnionType", object())):
        non_null = [a for a in args if a is not type(None)]
        nullable = len(non_null) != len(args)
        if len(non_null) != 1:
            raise SchemaError("solo se admite X | None, no uniones de varios tipos")
        sch, _ = type_to_schema(non_null[0])
        return sch, nullable
    if origin is Literal:
        vals = list(args)
        kinds = {type(v) for v in vals}
        if not vals or not kinds <= {str, int, bool}:
            raise SchemaError("Literal solo admite str, int o bool")
        t = "string" if kinds == {str} else "integer" if kinds == {int} else "boolean" if kinds == {bool} else None
        if t is None:
            raise SchemaError("Literal con tipos mezclados")
        return {"type": t, "enum": vals}, False
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        vals = [m.value for m in tp]
        if not all(isinstance(v, str) for v in vals):
            raise SchemaError("Enum solo admite valores de texto")
        return {"type": "string", "enum": vals}, False
    if tp is bool:               # antes que int: bool es subclase de int
        return {"type": "boolean"}, False
    if tp is int:
        return {"type": "integer"}, False
    if tp is float:
        return {"type": "number"}, False
    if tp is str:
        return {"type": "string"}, False
    if tp is dict or origin is dict:
        return {"type": "object"}, False
    if tp is list or origin is list:
        if args:
            item, _ = type_to_schema(args[0])
            return {"type": "array", "items": item}, False
        return {"type": "array"}, False
    raise SchemaError(f"tipo no soportado: {tp!r}")


def _enum_of(tp):
    """La clase Enum del parámetro (directa o dentro de X | None), para reconvertir el valor."""
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return tp
    for a in get_args(tp):
        if isinstance(a, type) and issubclass(a, enum.Enum):
            return a
    return None


def build_schema(fn, context_type: type | None = None) -> tuple[dict, dict[str, dict]]:
    """
    → (parameters, info) donde `parameters` es el esquema JSON {"type":"object",...} y
    `info[nombre] = {"schema", "required", "nullable"}` lo usa el validador de argumentos.
    """
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception as e:  # anotaciones que no se pueden resolver
        raise SchemaError(f"no se pudieron resolver los type hints de {fn.__name__}: {e}")
    docs = _arg_docs(fn.__doc__)
    props: dict[str, dict] = {}
    required: list[str] = []
    info: dict[str, dict] = {}

    for name, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            raise SchemaError(f"{fn.__name__}: no se admiten *args/**kwargs")
        ann = hints.get(name, inspect.Parameter.empty)
        if name == "ctx" or (context_type is not None and ann is context_type):
            continue
        try:
            sch, nullable = type_to_schema(ann)
        except SchemaError as e:
            raise SchemaError(f"{fn.__name__}({name}): {e}")
        sch = dict(sch)
        if name in docs:
            sch["description"] = docs[name]
        has_default = p.default is not inspect.Parameter.empty
        if has_default and p.default is not None:
            try:
                json.dumps(p.default)
                sch["default"] = p.default.value if isinstance(p.default, enum.Enum) else p.default
            except (TypeError, ValueError):
                pass
        props[name] = sch
        if not has_default:
            required.append(name)
        info[name] = {"schema": sch, "required": not has_default, "nullable": nullable or p.default is None,
                      "enum_cls": _enum_of(ann)}

    parameters: dict = {"type": "object", "properties": props}
    if required:
        parameters["required"] = required
    if len(json.dumps(parameters, ensure_ascii=False)) > MAX_PARAMS_JSON:
        raise SchemaError(f"{fn.__name__}: el esquema pasa de {MAX_PARAMS_JSON} caracteres (el motor lo rechazaría)")
    return parameters, info


# ----------------------------------------------------------------------------- validación

class ArgError(ValueError):
    """Argumento inválido (mensaje corto y seguro para devolver al modelo)."""


def coerce(value: Any, sch: dict, nullable: bool, path: str) -> Any:
    """Valida un valor que eligió el MODELO contra el esquema; convierte lo evidente ("5" → 5)."""
    if value is None:
        if nullable:
            return None
        raise ArgError(f"'{path}' no puede ser null")
    t = sch.get("type")
    if t == "boolean":
        if isinstance(value, bool):
            out = value
        elif isinstance(value, str) and value.lower() in ("true", "false"):
            out = value.lower() == "true"
        else:
            raise ArgError(f"'{path}' debe ser true o false")
    elif t == "integer":
        if isinstance(value, bool):
            raise ArgError(f"'{path}' debe ser un entero")
        if isinstance(value, int):
            out = value
        elif isinstance(value, float) and value.is_integer():
            out = int(value)
        elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
            out = int(value.strip())
        else:
            raise ArgError(f"'{path}' debe ser un entero")
    elif t == "number":
        if isinstance(value, bool):
            raise ArgError(f"'{path}' debe ser un número")
        if isinstance(value, (int, float)):
            out = value
        elif isinstance(value, str):
            try:
                out = float(value.strip())
            except ValueError:
                raise ArgError(f"'{path}' debe ser un número")
        else:
            raise ArgError(f"'{path}' debe ser un número")
    elif t == "string":
        if not isinstance(value, str):
            raise ArgError(f"'{path}' debe ser texto")
        out = value
    elif t == "array":
        if not isinstance(value, list):
            raise ArgError(f"'{path}' debe ser una lista")
        item = sch.get("items")
        out = [coerce(v, item, False, f"{path}[{i}]") for i, v in enumerate(value)] if item else value
    elif t == "object":
        if not isinstance(value, dict):
            raise ArgError(f"'{path}' debe ser un objeto")
        out = value
    else:
        out = value
    if "enum" in sch and out not in sch["enum"]:
        raise ArgError(f"'{path}' debe ser uno de: {', '.join(map(str, sch['enum']))}")
    return out
