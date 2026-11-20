"""norns.tool, as the type checker should see it.

The SDK declares `tool` as returning `ToolDef | Callable` whichever way it is
called, so `@tool(side_effect=True)` reads as calling a ToolDef. At runtime
this is the SDK's own decorator, untouched; only the overloads are ours.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, overload

from norns.agent import ToolDef

if TYPE_CHECKING:

    @overload
    def tool(
        fn: Callable, /, *, name: str | None = None, description: str | None = None,
        side_effect: bool = False,
    ) -> ToolDef: ...

    @overload
    def tool(
        *, name: str | None = None, description: str | None = None, side_effect: bool = False
    ) -> Callable[[Callable], ToolDef]: ...

    def tool(
        fn: Callable | None = None, /, *, name: str | None = None,
        description: str | None = None, side_effect: bool = False,
    ) -> Any: ...

else:
    from norns import tool

__all__ = ["ToolDef", "tool"]
