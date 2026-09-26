"""What a job produces.

    output("weekly-equity-summary", rows)            # csv, the default
    output("weekly-equity-summary", rows, "json")

The tenant names the artefact; the platform decides where it physically lives and
when it expires. Locally a directory; on AWS an S3 prefix scoped to the app, with a
lifecycle rule from the platform's default retention.

THERE USED TO BE AN `outputs:` BLOCK in app.yaml declaring each artefact's name,
kind, format and retention, and `output()` refused anything not listed. It was
removed. The declaration bought one thing - a per-artefact retention - and cost a
block of ceremony in every job manifest plus a failure mode where the job runs,
computes, and then throws away the result because a name did not match. A platform
default that a team can override when they actually have a retention requirement is
the better trade at this size.
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


#: How long the platform keeps an artefact unless a team says otherwise. Ninety days
#: because that is one quarter plus a margin - long enough for "last quarter's report"
#: and short enough that nobody treats this as a data store.
DEFAULT_RETENTION = "90d"


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


def output(name: str, rows: Sequence[dict], fmt: str = "csv") -> str:
    """Write an artefact. Returns the location, for the log - not for reuse.

    Tenant code never names a destination, which is what keeps it identical in local,
    dev and prod.
    """
    if fmt not in ("csv", "json"):
        raise ManifestError(f"output format must be csv or json, got {fmt!r}")
    manifest = config.manifest()
    payload = _serialise(rows, fmt)

    root = Path(os.environ.get("INSIGHTS_OUTPUT_DIR", "outputs"))
    destination = root / manifest.app / f"{name}.{fmt}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)

    # Row count and size, never content - the same rule as every other log record.
    telemetry.get_logger().info(
        "output_written", output=name, format=fmt, rows=len(rows),
        bytes=len(payload), retention=DEFAULT_RETENTION,
    )
    return str(destination)
