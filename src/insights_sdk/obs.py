"""Telemetry, and the boundary that keeps tenant data out of it.

Two streams, deliberately separate:

    events   what the app did. Structured, tenant-authored, freely readable by the
             platform team - which is exactly why it must never contain rows.
    audit    what was read. Platform-authored, append-only, the artefact a compliance
             reviewer is handed.

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

#: dataset -> field names that must never appear in telemetry. Populated by the data
#: broker when it resolves a restricted dataset, from the platform catalog - never from
#: tenant code, which is the point: the tenant cannot shorten this list.
_sensitive: dict[str, tuple[str, ...]] = {}

#: Test/CLI hook. When a list is installed here, every record is also appended to it.
_capture: list[dict] | None = None


def register_sensitive_fields(dataset: str, fields: tuple[str, ...]) -> None:
    _sensitive[dataset] = tuple(f.lower() for f in fields)


def clear_sensitive_fields() -> None:
    _sensitive.clear()


def _all_sensitive() -> set[str]:
    return {f for fields in _sensitive.values() for f in fields}


def _reject_payloads(fields: dict[str, Any]) -> None:
    """A telemetry record carries facts ABOUT a run, never data FROM it."""
    for key, value in fields.items():
        if not isinstance(value, _SCALARS):
            raise RedactionError(
                f"telemetry field {key!r} is a {type(value).__name__}, and log records may only "
                f"carry scalars. If you want to record a result set, record its shape: "
                f"log.info('read', dataset=..., rows=len(rows))."
            )
        if isinstance(value, str) and len(value) > _MAX_STRING:
            raise RedactionError(
                f"telemetry field {key!r} is {len(value)} characters. Anything this large is a "
                f"payload in string form; log an identifier or a length instead."
            )


def _reject_sensitive_names(event: str, fields: dict[str, Any]) -> None:
    """Additionally, for apps touching restricted data: no field NAME from that dataset
    may appear - as a key, in the event name, or inside any string value."""
    sensitive = _all_sensitive()
    if not sensitive:
        return
    haystacks = [event.lower()]
    for key, value in fields.items():
        haystacks.append(str(key).lower())
        if isinstance(value, str):
            haystacks.append(value.lower())
    for name in sensitive:
        for hay in haystacks:
            if name in hay:
                raise RedactionError(
                    f"telemetry mentions {name!r}, a field of a restricted dataset this app reads. "
                    f"Restricted field names do not go in logs, even empty ones - a field name in a "
                    f"log line tells a reader what the data contains."
                )


def _base_record(stream: str, level: str, event: str) -> dict[str, Any]:
    from . import config  # local import: obs is loaded before a manifest may exist

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
        _reject_sensitive_names(event, fields)
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


def audit_read(
    *,
    dataset: str,
    classification: str,
    owner: str,
    connection: str,
    rows: int,
    ms: int,
    masked_fields: int = 0,
    via_grant: bool = False,
    break_glass: str | None = None,
) -> dict[str, Any]:
    """Record one read. Written by the broker, never by tenant code.

    Note `masked_fields` is a COUNT, not a list of names: the audit stream is read by
    more people than the data is, so it must not become a description of the data's
    shape. Same reasoning as the field-name assertion above.
    """
    record = _base_record("audit", "info", "dataset_read")
    record.update(
        dataset=dataset,
        classification=classification,
        owner=owner,
        connection=connection,
        rows=rows,
        ms=ms,
        masked_fields=masked_fields,
        via_grant=via_grant,
    )
    if break_glass:
        record["break_glass"] = break_glass
    _reject_payloads({k: v for k, v in record.items()})
    _write(record)
    return record


@contextmanager
def capture() -> Iterator[list[dict]]:
    """Collect emitted records. For tests and for `insights status`."""
    global _capture
    previous, _capture = _capture, []
    try:
        yield _capture
    finally:
        _capture = previous
