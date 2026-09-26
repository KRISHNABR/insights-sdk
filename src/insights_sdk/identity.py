"""Who is calling, and whether we believe them.

The trust model in one line: **an app trusts identity only when the platform edge put it
there.** Anything a client sent is discarded, not merely distrusted.

Running an app outside the edge is therefore not "an app with no user" - it is an app
whose every authorization check returns no, by construction.
"""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Mapping

from .errors import AuthzError, IdentityError

HEADER_USER = "x-auth-user"
HEADER_GROUPS = "x-auth-groups"
HEADER_REQUEST_ID = "x-auth-request-id"
HEADER_EDGE_TOKEN = "x-auth-edge-token"

#: Every header the edge is responsible for. The edge STRIPS all of these from the
#: inbound request before re-injecting its own validated values, so a client cannot
#: assert its own identity. Kept here so the edge and the SDK cannot drift apart.
EDGE_OWNED_HEADERS = (HEADER_USER, HEADER_GROUPS, HEADER_REQUEST_ID, HEADER_EDGE_TOKEN)


@dataclass(frozen=True)
class Caller:
    subject: str
    request_id: str
    trusted: bool
    _groups: tuple[str, ...] = field(default=(), repr=False)
    is_service: bool = False

    @property
    def groups(self) -> tuple[str, ...]:
        """Group membership - empty unless the identity came from the edge.

        This is a property rather than a plain field on purpose. Every authorization
        decision in the SDK goes through `.groups`, so an untrusted caller fails every
        check *structurally*, with no code anywhere needing to remember to test
        `.trusted` first. Fail-closed as a data structure, not as a discipline.
        """
        return self._groups if self.trusted else ()

    def has_role(self, role: str) -> bool:
        """Deprecated. Checks a raw corporate group name.

        It predates the three-tier model: a caller's groups are now compared against
        `access.manage`, so a check against a bare group name bypasses the tier
        hierarchy - an owner would fail `has_role("MG-X-READERS")` even though they
        satisfy every reader check.

        Kept working for a full major. Every call emits a telemetry record naming
        this app and this symbol, so the migration can be watched rather than
        assumed - see deprecation.py.
        """
        from .deprecation import deprecated

        return deprecated(
            since="0.2.0", removed_in="1.0.0", instead='require_role("reader")'
        )(lambda: role in self.groups)()

    # `is_service` is a field set by whoever CONSTRUCTS the caller, not something
    # inferred from the subject string. It used to be `subject.startswith("svc:")`,
    # which quietly made the subject format load-bearing: rename the prefix and
    # masking silently changes behaviour. Only the scheduler sets it, and only
    # `Caller.service()` can.
    #
    # It matters at exactly one place - field masking - because a job has no human
    # whose corporate groups could answer "may this caller see salaries?".
    # See `broker._effective_roles`.

    @classmethod
    def anonymous(cls) -> "Caller":
        return cls(subject="anonymous", request_id="-", trusted=False)

    @classmethod
    def service(cls, subject: str, groups: tuple[str, ...], request_id: str) -> "Caller":
        """The identity a scheduled run acts as.

        A 06:00 batch job has no interactive user, so it acts as itself with the app's
        owner groups. Trusted because the platform scheduler - not a network client -
        constructed it. Naming this explicitly matters: "who is the caller for a job?"
        is otherwise answered by accident.
        """
        return cls(
            subject=subject, request_id=request_id, trusted=True, _groups=groups, is_service=True
        )


_current: contextvars.ContextVar[Caller] = contextvars.ContextVar(
    "insights_caller", default=Caller.anonymous()
)


def current_user() -> Caller:
    """The caller for this request or run. Never raises - always returns *something*,
    so telemetry can always stamp a subject. Authorization is what raises."""
    return _current.get()


@contextmanager
def as_caller(caller: Caller) -> Iterator[Caller]:
    token = _current.set(caller)
    try:
        yield caller
    finally:
        _current.reset(token)


def from_headers(headers: Mapping[str, str]) -> Caller:
    """Build a caller from edge-injected headers, or refuse to.

    The edge proves it is the edge with a token both it and the app receive from the
    platform's secret store. In a real deployment this would be mTLS or a signed
    assertion; the shape of the check is the same, and the stub keeps the seam visible.
    """
    lowered = {str(k).lower(): v for k, v in headers.items()}
    expected = os.environ.get("INSIGHTS_EDGE_TOKEN")
    presented = lowered.get(HEADER_EDGE_TOKEN)

    if not expected or not presented or presented != expected:
        # Note what we do NOT do: we do not construct an untrusted Caller carrying the
        # client's claimed subject and groups. Values we will never honour are values
        # we should not keep - holding them invites some future line of code to use one.
        return Caller.anonymous()

    subject = lowered.get(HEADER_USER)
    if not subject:
        return Caller.anonymous()

    raw_groups = lowered.get(HEADER_GROUPS, "")
    groups = tuple(g.strip() for g in raw_groups.split(",") if g.strip())
    return Caller(
        subject=subject,
        request_id=lowered.get(HEADER_REQUEST_ID, "-"),
        trusted=True,
        _groups=groups,
    )


def require_trusted() -> Caller:
    """Step 3 of the data broker. No trusted caller, no data."""
    caller = current_user()
    if not caller.trusted:
        raise IdentityError(
            "No trusted caller. This app is running without the platform edge in front "
            "of it, so no identity can be established and nothing may be read. "
            "Run it through `insights run` or the platform runtime."
        )
    return caller


def require_role(tier: str) -> Caller:
    """Authorize the caller for one of this app's three access tiers.

        require_role("reader")        # anyone the app lists, at any tier
        require_role("contributor")   # contributors and owners
        require_role("owner")         # owners only

    It used to take an app-defined role name from `access.roles`. That block is gone:
    two authorization vocabularies in one manifest meant every reader had to work out
    which one a given check used, and in practice apps' roles restated these three.

    The tiers NEST - an owner satisfies `require_role("reader")` - because the
    alternative is every team remembering to list their owners in three places, and
    forgetting once is a lockout that looks like a platform bug.
    """
    from . import config

    caller = require_trusted()
    allowed = set(config.manifest().manage.groups_for(tier))
    if not (allowed & set(caller.groups)):
        raise AuthzError(
            f"{caller.subject} is not a {tier} of this app. "
            f"Ask an owner to add your group to access.manage in app.yaml."
        )
    return caller
