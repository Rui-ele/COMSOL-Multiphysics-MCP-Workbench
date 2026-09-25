"""Shared serialization for individual COMSOL operations."""

from contextvars import ContextVar
from threading import RLock


# Covers one operation in this MCP process; COMSOL owns its model state.
MODEL_OPERATION_LOCK = RLock()

# (session manager, requested reference, live record), scoped to one guarded call.
ACTIVE_MODEL: ContextVar[tuple | None] = ContextVar("comsol_active_model", default=None)
