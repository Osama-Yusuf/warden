"""Per-engine adapters. Import a module here and its @register call wires it
into the registry, so get_adapter() can hand back the right one."""

from .base import (
    EngineAdapter,
    EngineError,
    Mutation,
    NotSupported,
    Target,
    get_adapter,
)

# Importing each module runs its @register, which is the whole point: none of
# these names are used directly, they just have to load so the registry fills up.
from . import postgres  # noqa: F401
from . import mysql  # noqa: F401
from . import mongo  # noqa: F401
from . import elasticsearch  # noqa: F401
from . import redis  # noqa: F401
from . import sqlite  # noqa: F401

__all__ = ["EngineAdapter", "EngineError", "Mutation", "NotSupported", "Target", "get_adapter"]
