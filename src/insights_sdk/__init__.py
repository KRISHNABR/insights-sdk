"""Insights Hub SDK - everything a tenant app inherits from the platform.

A tenant's whole surface area is here:

    from insights_sdk import web_app, job, query, fetch, get_logger, current_user, require_role

Anything not exported from this module is platform internals and may change in a minor
release. Anything exported is covered by the support window in ADR-001.
"""

__version__ = "0.1.0"

#: Versions still supported. The platform runs an N-2 window (ADR-001), so this is the
#: list CI checks a tenant's declared floor against, and the list the deprecation
#: telemetry is read against.
SUPPORTED_VERSIONS = ("0.1.0",)

from .entrypoints import job, run_job, web_app  # noqa: E402
from .broker import fetch, query  # noqa: E402
from .identity import Caller, current_user, require_role, require_trusted  # noqa: E402
from .telemetry import get_logger  # noqa: E402
from .outputs import output  # noqa: E402
from .errors import (  # noqa: E402
    AuthzError,
    EntitlementError,
    IdentityError,
    InsightsError,
    ManifestError,
    RedactionError,
    UnknownDatasetError,
)

__all__ = [
    "__version__",
    # the entire tenant-facing surface
    "web_app",
    "job",
    "run_job",
    "query",
    "fetch",
    "get_logger",
    "output",
    "current_user",
    "require_role",
    "require_trusted",
    "Caller",
    "SUPPORTED_VERSIONS",
    "InsightsError",
    "ManifestError",
    "EntitlementError",
    "IdentityError",
    "AuthzError",
    "RedactionError",
    "UnknownDatasetError",
]
