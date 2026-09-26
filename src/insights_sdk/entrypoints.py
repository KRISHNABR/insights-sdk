"""The two front doors: `web_app()` for an interactive app, `job()` for a scheduled one.

A tenant writes handlers or a function. Everything a platform team would otherwise have
to ask twenty-five teams to remember - identity wiring, request ids, structured access
logs, a health check that actually checks something, graceful shutdown - is here, and
arrives with the next SDK release rather than in twenty-five pull requests (ADR-001).
"""

from __future__ import annotations

import os
import signal
import time
import uuid
from typing import Any, Callable

from . import config, identity, telemetry
from .errors import ConfigError, InsightsError

# --------------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------------


def _health(manifest: config.Manifest) -> dict[str, Any]:
    """A health check that exercises its dependencies.

    `{"status": "ok"}` tells you a process is running, which you already knew. This
    resolves every dataset the app declared and confirms the credential the platform
    was supposed to inject is actually present - so the common production failure
    (a deploy that starts fine and fails on first use) surfaces at the health check
    instead of at 06:00 in front of a user.
    """
    checks: dict[str, str] = {}
    healthy = True
    for request in manifest.datasets:
        try:
            resolved = config.catalog().resolve(request.dataset)
            settings = resolved.connection.environments[resolved.env]
            credential_var = settings.get("dsn_env") or settings.get("base_url_env")
            if credential_var and not os.environ.get(credential_var):
                raise ConfigError(f"{credential_var} not injected")
            checks[request.dataset] = "ok"
        except Exception as exc:                        # noqa: BLE001 - report, never crash the probe
            checks[request.dataset] = f"failed: {exc}"
            healthy = False
    return {
        "status": "ok" if healthy else "degraded",
        "app": manifest.app,
        "team": manifest.team,
        "sdk": config and __import__("insights_sdk").__version__,
        "datasets": checks,
    }


def web_app(**fastapi_kwargs: Any):
    """A FastAPI app with the platform's middleware already wired."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    manifest = config.manifest()
    if manifest.kind != "web":
        raise ConfigError(f"{manifest.app} is declared kind: {manifest.kind}, not web")

    application = FastAPI(title=manifest.app, **fastapi_kwargs)
    log = telemetry.get_logger()

    @application.middleware("http")
    async def _platform_context(request: Request, call_next):
        caller = identity.from_headers(request.headers)
        started = time.perf_counter()
        with identity.as_caller(caller):
            try:
                response = await call_next(request)
            except InsightsError as refusal:
                # A platform refusal is a 403 with a readable reason, not a 500. The tenant
                # did not crash; they were told no, and the reason names the ADR.
                log.warn("request_refused", path=request.url.path, reason=type(refusal).__name__)
                return JSONResponse({"error": str(refusal)}, status_code=403)
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=int((time.perf_counter() - started) * 1000),
            )
            response.headers["X-Request-Id"] = caller.request_id
            return response

    # `spa`: serve the tenant's own frontend from the SAME origin as its API.
    # Same-origin is the whole security argument - the browser holds a session
    # cookie it cannot read, and never a token an XSS bug could exfiltrate. The
    # platform SERVES the bundle; it does not build it. No Node in the image.
    #
    # Mounted at "/" on STARTUP rather than here, because a mount added now would
    # be registered before the tenant's own @app.get decorators have run and would
    # shadow every one of them. Appending at startup puts the catch-all last,
    # which is where a catch-all belongs.
    if manifest.web and manifest.web.type == "spa":
        static_dir = manifest.path.parent / "static"

        def _mount_frontend() -> None:
            from fastapi.staticfiles import StaticFiles
            from starlette.routing import Mount

            if static_dir.is_dir():
                application.router.routes.append(
                    Mount("/", app=StaticFiles(directory=static_dir, html=True), name="static")
                )
                log.info("frontend_mounted", path=str(static_dir.name))

        application.router.on_startup.append(_mount_frontend)

    @application.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return _health(manifest)

    return application


# --------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------


def run_job(fn: Callable[[], Any], *, run_id: str | None = None) -> int:
    """Run a scheduled job under the platform's contract, and return an exit code.

    The platform scheduler calls this; a tenant's `main.py` calls it too, so running a
    job by hand behaves identically to running it on schedule. Handles the four things
    every team otherwise reinvents: an identity, a run id, structured start/finish
    telemetry, and a SIGTERM that drains instead of dying mid-write.
    """
    manifest = config.manifest()
    if manifest.kind != "job":
        raise ConfigError(f"{manifest.app} is declared kind: {manifest.kind}, not job")

    run_id = run_id or os.environ.get("INSIGHTS_RUN_ID") or f"run-{uuid.uuid4().hex[:10]}"
    caller = identity.Caller.service(manifest.service_identity, manifest.owners, run_id)
    log = telemetry.get_logger()

    draining = {"stop": False}

    def _drain(signum, _frame):
        # Record the intent and let the job finish its current unit of work. A job killed
        # mid-write is the failure that costs a platform team a morning.
        draining["stop"] = True
        log.warn("sigterm_received", run_id=run_id, signal=signum)

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _drain)

    started = time.perf_counter()
    try:
        with identity.as_caller(caller):
            log.info("job_start", run_id=run_id, schedule=manifest.schedule or "-")
            fn()
            log.info("job_complete", run_id=run_id, ms=int((time.perf_counter() - started) * 1000),
                     drained=draining["stop"])
        return 0
    except InsightsError as refusal:
        with identity.as_caller(caller):
            log.error("job_refused", run_id=run_id, reason=type(refusal).__name__,
                      ms=int((time.perf_counter() - started) * 1000))
        print(f"{type(refusal).__name__}: {refusal}", flush=True)
        return 2                                        # 2 = the platform said no
    except Exception as exc:                            # noqa: BLE001
        with identity.as_caller(caller):
            log.error("job_failed", run_id=run_id, reason=type(exc).__name__,
                      ms=int((time.perf_counter() - started) * 1000))
        print(f"{type(exc).__name__}: {exc}", flush=True)
        return 1                                        # 1 = the job broke
    finally:
        signal.signal(signal.SIGTERM, previous)


def job(fn: Callable[[], Any]) -> Callable[[], int]:
    """Decorator form. `@job` on the entrypoint, then `raise SystemExit(main())`."""

    def wrapper() -> int:
        return run_job(fn)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper
