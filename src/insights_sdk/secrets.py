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
A `.env` file in the app's own repo, gitignored, read only when INSIGHTS_ENV=local
(CI refuses a committed one). An injected INSIGHTS_SECRET_<NAME> variable always wins,
which is how production delivers a value. The name and the scoping rule are identical
in both places, so code that works here works there - the only thing that changes is
who enforces the scoping: a file on a laptop, IAM in an account.
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


def _from_dotenv(name: str, app_dir: Path) -> str | None:
    """Read one key from the app's own `.env`. LOCAL ONLY.

    A team's credential belongs in the team's repo, next to the code that uses it -
    not in a directory inside the platform's checkout, which made a tenant depend on
    the platform's layout to run at all.

    `.env` is gitignored by the generator and the deploy gate refuses a committed one,
    because a credential in git is a credential in the history forever.
    """
    target = app_dir / ".env"
    if not target.is_file():
        return None
    for line in target.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return None


def resolve(app: str, name: str) -> Secret:
    """Fetch a secret for THIS app.

    An app can only ever ask for its own: the caller passes no path, and `app` comes
    from the manifest the platform registered, not from anything a tenant types.

    Three sources, in order, and the order is the design:

      1. an injected environment variable   how production delivers it
      2. the app's own .env                 LOCAL ONLY, and only for local
      3. nothing - a clear error naming the file and whose job it is

    In production only (1) exists. The runtime injects the value from Secrets Manager
    using the app's own identity, and `.env` is not consulted at all - which is
    enforced below rather than documented, because "remember not to ship a .env" is
    not a control.
    """
    # 1. An injected variable. In ECS or Kubernetes the platform populates this from
    #    the secret store using the app's identity; locally a developer can export it
    #    to point at a sandbox credential without editing any file.
    env_key = f"INSIGHTS_SECRET_{name.upper().replace('-', '_')}"
    if env_key in os.environ:
        return Secret(name, os.environ[env_key])

    # 2. The app's own .env. Refused outside local, so a .env that escapes into an
    #    image cannot quietly become the source of a production credential.
    environment = os.environ.get("INSIGHTS_ENV", "local")
    app_dir = Path(os.environ.get("INSIGHTS_APP_MANIFEST", "app.yaml")).resolve().parent
    if environment == "local":
        value = _from_dotenv(name, app_dir)
        if value:
            return Secret(name, value)
        raise SecretError(
            f"secret '{name}' is not set.\n"
            f"  add it to   {app_dir / '.env'}\n"
            f"      {name}=some-local-value\n"
            f"  .env is gitignored, and CI refuses a committed one.\n"
            f"  in dev and prod this comes from {path_for(app, name)}, which your\n"
            f"  team writes and the platform team cannot read."
        )

    raise SecretError(
        f"secret '{name}' was not injected into this {environment} environment.\n"
        f"  expected as  {env_key}\n"
        f"  from         {path_for(app, name)}\n"
        f"  who sets it  your team. The platform binds your app's identity to that\n"
        f"               path and cannot read or write the value itself."
    )
