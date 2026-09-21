"""Holds the active Bot Framework TurnContext for the current request.

Lets the existing `teams_service` reply/card helpers send through the SDK when a turn is in
flight (SDK path) and fall back to raw REST otherwise — without threading TurnContext through
the ~1500-line `process_activity`. Kept in its own tiny module to avoid import cycles.
"""
from contextvars import ContextVar
from typing import Any, Optional

current_turn: ContextVar[Optional[Any]] = ContextVar("current_turn", default=None)
