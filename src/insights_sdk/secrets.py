"""Secret custody: the platform holds the SLOT, the team holds the VALUE.

THE PROBLEM THIS SOLVES
-----------------------
A team needs a credential to reach their own warehouse. Someone has to store it. If
the platform team stores it, three people can read every team's database password -
and "we promise not to look" is not a control, it is a sentence in a document.

THE SHAPE OF THE ANSWER
-----------------------
The platform never sees the value. It creates a namespace, binds the app's identity
to it, and gets out of the way:

    app.yaml            secret: hr-warehouse-token        <- a REFERENCE
    secret store        insights/comp-report/hr-warehouse-token
    who can WRITE it    the app's owning group (the team)
    who can READ it     sp-comp-report, the app's own identity, and nothing else
    who CANNOT read it  the platform team - by explicit IAM Deny, not by policy

That last line is the whole design. Custody is enforced by the thing issuing the
credentials, not by us choosing not to call GetSecretValue. In AWS the platform's
own role carries an explicit Deny on `insights/*` which no Allow can override, and
every read lands in CloudTrail whoever makes it - including ours, if we ever grant
ourselves an exception.

WHAT THE PLATFORM STILL DOES
----------------------------
Creates the slot at deploy time, binds the identity, rotates the *binding*, and
reports when a secret is missing or expired - with an error that says which
connection and which team to ask. It never logs a value, never returns one to
telemetry, and never puts one in an exception message. See `Secret.__repr__`.

LOCALLY
-------
A file per secret under `runtime/fakes/secrets/`, gitignored, seeded with obvious
fakes. The lookup path and the scoping rule are identical, so code that works here
works there - the only thing that changes is who enforces the scoping.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import InsightsError


class SecretError(InsightsError):
    """A secret could not be resolved. The message names the connection and the fix,
    and never the value or any part of it."""


@dataclass(frozen=True)
class Secret:
    """A resolved secret value that resists being written down by accident.

    The value is reachable only through `.reveal()`, which is deliberately awkward to
    type and easy to grep for in review. Everything that stringifies an object -
    f-strings, logging, repr in a traceback, json.dumps with default=str - gets the
    redaction instead. Defence in depth behind the telemetry layer's own rules,
    because the expensive leak is the one nobody wrote on purpose.
    """

    name: str
    _value: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"<Secret {self.name} REDACTED>"

    __str__ = __repr__


def path_for(app: str, name: str) -> str:
    """Where a secret lives. One rule, every environment.

    Scoped by app, so the read permission the platform grants can be scoped the same
    way. A flat namespace would mean any app's identity could read any other app's
    credential, and the isolation story would be a comment rather than a boundary.
    """
    return f"insights/{app}/{name}"


def resolve(app: str, name: str) -> Secret:
    """Fetch a secret for THIS app. Two backends, one contract.

    An app can only ever ask for its own: the caller passes no path, and `app` comes
    from the manifest the platform registered, not from anything a tenant types at
    call time.
    """
    # 1. An explicit environment override. This is how a job running in ECS or
    #    Kubernetes receives an injected secret, and how a developer points at
    #    their own sandbox credential without editing any file.
    env_key = f"INSIGHTS_SECRET_{name.upper().replace('-', '_')}"
    if env_key in os.environ:
        return Secret(name, os.environ[env_key])

    # 2. The local file store. In production this branch is a call to AWS Secrets
    #    Manager (or Vault) with the app's own identity - same path, same scoping,
    #    and the failure modes below map one-to-one onto its error codes.
    store = os.environ.get("INSIGHTS_SECRET_DIR")
    if not store:
        raise SecretError(
            f"no secret backend configured, so '{name}' cannot be resolved. "
            f"Locally, `insights up` and `insights run` set this for you."
        )

    target = Path(store) / app / name
    if not target.is_file():
        raise SecretError(
            f"secret '{name}' is not set for this app.\n"
            f"  expected at  {path_for(app, name)}\n"
            f"  who sets it  your team - the platform creates the slot and cannot "
            f"read or write the value\n"
            f"  locally      write it to {target}"
        )
    return Secret(name, target.read_text().strip())
