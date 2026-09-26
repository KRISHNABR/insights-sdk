# insights-sdk

The Insights Hub platform SDK. Everything a tenant app inherits, plus the `insights` CLI.

Install it and you have the whole platform surface:

```python
from insights_sdk import web_app, job, query, fetch, get_logger, current_user, require_role
```

**Read the platform docs first:** [`insights-platform`](https://github.com/KRISHNABR/insights-platform) —
[ONBOARDING.md](https://github.com/KRISHNABR/insights-platform/blob/main/ONBOARDING.md) for day one,
[docs/ARCHITECTURE.md](https://github.com/KRISHNABR/insights-platform/blob/main/docs/ARCHITECTURE.md) for how it works.

## Why the CLI lives in here

`insights new-app` generates a repository whose CI callers and project file must be
compatible with the library version they target. Shipped separately, you get
scaffold/library skew and no way to detect it. Shipped together, that is structurally
impossible — and `insights upgrade-scaffold` can re-render those files later precisely
because the SDK knows which ones it owns.

## Layout

```
src/insights_sdk/
├── errors.py         the platform's failure vocabulary. Every one is a refusal
├── config.py         app.yaml + the platform registry. Where "declare, don't wire" is enforced
├── identity.py       Caller, and the trusted-edge model. groups is () unless trusted
├── obs.py            the logger, the redaction boundary, the audit stream
├── engines.py        one adapter per connection, resolved per environment
├── data.py           the broker — the single path to data. Start reading here
├── app.py            web_app() and run_job()
├── deprecation.py    deprecation telemetry (ADR-001's keystone)
└── cli/              new-app · doctor · datasets · status · access · up · run
                      compliance-report · upgrade-scaffold
```

If you read one file, read [`data.py`](src/insights_sdk/data.py).

## Tests

```bash
uv run --with pytest --with pyyaml --with fastapi python -m pytest -q
```

31 tests, and they are not unit tests for their own sake — each one exists to turn an
ADR claim into evidence:

| Test file | Proves |
|---|---|
| `test_identity_fails_closed.py` | a client cannot assert its own identity; an app outside the edge reads nothing |
| `test_entitlement.py` | knowing a name is not access; declaring a restricted dataset is not enough; errors do not form a discovery oracle |
| `test_portability_and_masking.py` | the same SQL reads a different table per environment; masking follows the caller, and a job's unmask comes from the grant |
| `test_telemetry_boundary.py` | logs cannot carry rows, and cannot mention restricted field names |
| `test_no_escape_hatch.py` | there is no exported way round the broker — fails if anyone adds one |
| `test_deprecation_telemetry.py` | the upgrade story is driven by data, once per symbol |

## Versioning

Semantic. Tenants declare a **floor** (`>=0.1,<1`), never a pin — the manifest loader
rejects `==`. The platform supports the current major and two before it. See
[ADR-001](https://github.com/KRISHNABR/insights-platform/blob/main/docs/adr/0001-platform-shape-and-reuse-strategy.md).
