"""Per-engine adapters. Import a module here and its @register call wires it
into the registry, so get_adapter() can hand back the right one."""

from .base import EngineAdapter, EngineError, NotSupported, Target, get_adapter
from . import postgres  # noqa: F401  (import for the side effect of registering)

__all__ = ["EngineAdapter", "EngineError", "NotSupported", "Target", "get_adapter"]
