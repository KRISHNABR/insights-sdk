# Changelog

Semantic versioning. Tenants declare a floor (`>=0.1,<1`), so anything that is not a
major reaches every app on its next build without them doing anything — which is why
the "Changed" section of a minor release may never contain a breaking change.

## 0.1.0 — 2026-09-26

First release. The tenant surface is `web_app`, `job` / `run_job`, `query`, `fetch`,
`get_logger`, `current_user`, `require_role`, `Caller`.

**Added**
- `data.query()` / `data.fetch()` — the broker. Two verbs, two engines, one enforcement path
- `identity` — the trusted-edge model; `Caller.groups` is empty unless the edge asserted it
- `obs` — structured logging with a redaction boundary that raises at emit, plus an audit stream
- `app.web_app()` / `app.run_job()` — the two archetypes, with a health check that resolves
  every declared dataset and SIGTERM draining for jobs
- `deprecation.deprecated()` — deprecation telemetry, once per symbol per process
- the `insights` CLI: `new-app`, `doctor`, `datasets`, `status`, `access`, `up`, `run`,
  `compliance-report`, `upgrade-scaffold`

**Support window:** 0.1.x. The platform supports the current major and the two before it.

---

### How a future release will read

A worked example of the contract, so teams know what to expect:

> ## 0.2.0
>
> **Added**
> - `query(..., timeout=)`.
>
> **Deprecated**
> - `run_sql()` — use `query()`. It still works and now emits `sdk_deprecated_use`, so we
>   can see who is affected. Scheduled for removal in 1.0.
>
> **Changed**
> - Nothing that requires you to act. If it did, this would be 1.0.

The rule this illustrates: **a deprecation is not a request, it is an instrument.** We do
not cut a major until the telemetry says nobody is still on the removed path.
