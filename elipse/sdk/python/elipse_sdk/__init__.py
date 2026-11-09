"""elipse_sdk — SDK de anfitrión para ELIPSE (núcleo, sin frameworks)."""

from .client import ChatResult, Elipse, ElipseError, ElipseTimeout
from .registry import Context, ToolError, ToolRegistry
from .schema import SchemaError
from .signing import MAX_SKEW_S, sign, verify_request

__all__ = ["Elipse", "ChatResult", "ElipseError", "ElipseTimeout", "Context", "ToolError",
           "ToolRegistry", "SchemaError", "sign", "verify_request", "MAX_SKEW_S"]
__version__ = "0.1.0"
