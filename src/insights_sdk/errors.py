"""The platform's failure vocabulary.

Every one of these is a *refusal*, not a crash. They exist so that a tenant reading a
stack trace learns which rule they hit and where it is written down, rather than
discovering that something silently returned fewer rows.

Design rule: the SDK fails closed. When the SDK cannot establish that an action is
permitted, it raises - it never degrades to "probably fine".
"""


class InsightsError(Exception):
    """Base class. Catch this to catch anything the platform refused."""

    adr: str | None = None

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = super().__str__()
        return f"{base}  [see {self.adr}]" if self.adr else base


class ManifestError(InsightsError):
    """app.yaml is missing, malformed, or declares something a tenant may not declare."""

    adr = "ADR-002 / ADR-004"


class ConfigError(InsightsError):
    """The platform's own configuration is missing or unreadable. A platform bug, not a tenant one."""


class UnknownDatasetError(InsightsError):
    """The dataset is not in the platform catalog. Nobody can read it, entitled or not."""

    adr = "ADR-002"


class EntitlementError(InsightsError):
    """The app is not entitled to this dataset.

    Two distinct causes, both landing here on purpose:
      - the dataset is not declared in app.yaml            (no intent)
      - the dataset is restricted and has no active grant  (no approval)
    """

    adr = "ADR-002 s4"


class IdentityError(InsightsError):
    """There is no trusted caller. Raised when an app runs outside the platform edge.

    This is the fail-closed case that matters most: an app started by hand, with no
    edge in front of it, must not be able to read anything.
    """

    adr = "ADR-004"


class AuthzError(InsightsError):
    """The caller is trusted but lacks the role this action requires."""

    adr = "ADR-004"


class RedactionError(InsightsError):
    """A telemetry record tried to carry a payload, or a restricted field name.

    Raised at the emit point rather than scrubbed at the sink, because scrubbing at
    the sink fails open: anything the scrubber does not recognise has already left.
    """

    adr = "ADR-003"
