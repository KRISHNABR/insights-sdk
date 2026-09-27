"""Telemetry, and the boundary that keeps tenant data out of it.

One stream, `events`: what the app did. Structured, tenant-authored, and freely
readable by the platform team - which is exactly why it must never contain rows.

There used to be a second stream, `audit`, written by the data broker: what was read,
with the dataset, its classification and its owner. It went when the broker went
(ADR-002). The platform no longer sits in the data path, so it has nothing to put in
those fields, and a stream nobody writes is worse than no stream - it reads as "no
reads happened" rather than "nothing records this". `STREAMS` below is the single
source of truth, and `insights logs` takes its choices from it.

The important decision here is *where* redaction happens. Scrubbing in the sink fails
open: anything the scrubber does not recognise has already left the process. Raising at
the emit point fails closed - the record never exists, and the author finds out at their
desk rather than a reviewer finding out in a log search. See ADR-003.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import __version__ as _sdk_version_fallback
from .errors import RedactionError
from .identity import current_user

_SCALARS = (str, int, float, bool, type(None))

#: A string this long is a payload wearing a coat.
_MAX_STRING = 512

#: Test/CLI hook. When a list is installed here, every record is also appended to it.
_capture: list[dict] | None = None




def _reject_payloads(fields: dict[str, Any]) -> None:
    """A telemetry record carries facts ABOUT a run, never data FROM it."""
    for key, value in fields.items():
        if not isinstance(value, _SCALARS):
            raise RedactionError(
                f"telemetry field {key!r} is a {type(value).__name__}, and log records may only "
                f"carry scalars. If you want to record a result set, record its shape: "
                f"log.info('read', connection=..., rows=len(rows))."
            )
        if isinstance(value, str) and len(value) > _MAX_STRING:
            raise RedactionError(
                f"telemetry field {key!r} is {len(value)} characters. Anything this large is a "
                f"payload in string form; log an identifier or a length instead."
            )


# Every stream that has a writer. `insights logs --stream` reads its choices from
# here, so the CLI cannot offer a stream nothing produces.
STREAMS = ("events",)


def _base_record(stream: str, level: str, event: str) -> dict[str, Any]:
    from . import config  # local import: telemetry loads before a manifest may exist

    caller = current_user()
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stream": stream,
        "level": level,
        "event": event,
        "env": config.env(),
        "caller": caller.subject,
        "request_id": caller.request_id,
        "sdk": _sdk_version_fallback,
    }
    try:
        m = config.manifest()
        record["app"] = m.app
        record["team"] = m.team
    except Exception:
        # Telemetry must survive a missing manifest, or a config error becomes invisible.
        record["app"] = os.environ.get("INSIGHTS_APP", "unknown")
        record["team"] = "unknown"
    return record


def _write(record: dict[str, Any]) -> None:
    line = json.dumps(record, default=str, sort_keys=False)
    print(line, file=sys.stdout, flush=True)

    sink_dir = os.environ.get("INSIGHTS_SINK_DIR")
    if sink_dir:
        path = Path(sink_dir) / f"{record['stream']}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:          # append-only. Evidence you can edit is not evidence.
            fh.write(line + "\n")

    if _capture is not None:
        _capture.append(record)


class Logger:
    """The only logger a tenant app uses. There is no `logging.getLogger` escape hatch
    in the templates, because a second logger is a second, unenforced boundary."""

    def _emit(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        _reject_payloads(fields)
        record = _base_record("events", level, event)
        record.update(fields)
        _write(record)
        return record

    def info(self, event: str, **fields: Any) -> dict[str, Any]:
        return self._emit("info", event, **fields)

    def warn(self, event: str, **fields: Any) -> dict[str, Any]:
        return self._emit("warn", event, **fields)

    def error(self, event: str, **fields: Any) -> dict[str, Any]:
        return self._emit("error", event, **fields)


_logger = Logger()


def get_logger() -> Logger:
    return _logger


@contextmanager
def capture() -> Iterator[list[dict]]:
    """Collect emitted records. For tests and for `insights status`."""
    global _capture
    previous, _capture = _capture, []
    try:
        yield _capture
    finally:
        _capture = previous
