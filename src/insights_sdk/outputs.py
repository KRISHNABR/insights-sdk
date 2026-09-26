"""What a job produces.

A job that only logs is a job nobody can use. But a job that writes wherever it
likes is a job the platform cannot govern - so outputs are declared in app.yaml
and addressed by name:

    output("weekly-equity-summary", rows)

The tenant names the artefact. The platform decides where it physically lives,
who can read it, and when it expires - the same "declare, don't wire" rule that
governs data access, applied to the other direction.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any, Sequence

from . import config, telemetry
from .errors import ManifestError


def _spec(name: str) -> dict:
    manifest = config.manifest()
    for declared in manifest.outputs:
        if declared.get("name") == name:
            return declared
    raise ManifestError(
        f"'{name}' is not a declared output. Add it to the `outputs:` block of app.yaml - "
        f"the platform needs to know its retention and who may read it before it will store it."
    )


def _serialise(rows: Sequence[dict], fmt: str) -> bytes:
    if fmt == "json":
        return json.dumps(list(rows), default=str).encode()
    buffer = io.StringIO()
    rows = list(rows)
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return buffer.getvalue().encode()


def output(name: str, rows: Sequence[dict]) -> str:
    """Write a declared output. Returns the location, for the log - not for reuse.

    Locally this is a directory; on AWS it is an S3 prefix scoped to the app with
    a lifecycle rule built from `retention`. Tenant code is identical either way,
    because it never names a destination.
    """
    spec = _spec(name)
    manifest = config.manifest()
    fmt = spec.get("format", "csv")
    payload = _serialise(rows, fmt)

    root = Path(os.environ.get("INSIGHTS_OUTPUT_DIR", "outputs"))
    destination = root / manifest.app / f"{name}.{fmt}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)

    # Row count and size, never content - the same rule as every other log record.
    telemetry.get_logger().info(
        "output_written", output=name, format=fmt, rows=len(rows),
        bytes=len(payload), retention=spec.get("retention", "-"),
    )
    return str(destination)
