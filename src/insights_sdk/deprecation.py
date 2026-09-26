"""Deprecation telemetry - the keystone of the upgrade story (ADR-001).

The hard part of shipping a library to twelve teams is not writing the new version. It
is knowing **who is still on the old one, and which parts of it they actually use**.
Without that, every deprecation is a broadcast email and a hope, and the platform team
either breaks someone or supports the old path forever.

So a deprecated API does not just warn the caller. It emits a telemetry event naming the
app, the version and the specific symbol - which turns "please migrate" into a list of
four teams and the two functions between them and the next major release.

    @deprecated(since="0.2", removed_in="1.0", instead="query()")
    def run_sql(...): ...

What this buys, concretely:
  * the platform team can see a migration finish, rather than assuming it did
  * a major release is cut when the list is empty, not on a date
  * a team that is quietly stuck shows up as usage that never declines, which is a
    support conversation rather than a surprise outage
"""

from __future__ import annotations

import functools
import inspect
import warnings
from typing import Any, Callable, TypeVar

from .obs import get_logger

F = TypeVar("F", bound=Callable[..., Any])

#: Emit once per symbol per process. A job that calls a deprecated function in a loop
#: should produce one signal, not a million - otherwise the telemetry that was supposed
#: to make upgrades tractable becomes the reason nobody reads the logs.
_seen: set[str] = set()


def deprecated(*, since: str, removed_in: str, instead: str) -> Callable[[F], F]:
    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            symbol = f"{fn.__module__}.{fn.__qualname__}"
            if symbol not in _seen:
                _seen.add(symbol)
                get_logger().warn(
                    "sdk_deprecated_use",
                    symbol=symbol,
                    since=since,
                    removed_in=removed_in,
                    instead=instead,
                )
                warnings.warn(
                    f"{symbol} is deprecated since {since} and will be removed in "
                    f"{removed_in}. Use {instead}.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return fn(*args, **kwargs)

        # Preserve the signature so introspection and IDEs still work through the wrapper.
        wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
        wrapper.__deprecated__ = {"since": since, "removed_in": removed_in, "instead": instead}  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorate
