"""The `logging` notifier — renders an `Alert` to a structured log line.

The only v0.1 notifier (spec `## API Surface` → Notifier adapter); `sns`/`slack` are
deferred to v0.2. It proves the Notifier plane end to end: it self-registers on
`core.registry.NOTIFIERS` and renders every `Alert` field through a module logger via
an **f-string** — never a printf-percent template (the f-string-only convention is
load-bearing here; a printf-style logger call would lazy-interpolate and is forbidden
codebase-wide).
"""

import logging

from core.model import Alert
from core.registry import NOTIFIERS, ConfigBlock

# Module logger — the test asserts the rendered record via this logger name.
_LOGGER = logging.getLogger(__name__)


@NOTIFIERS.register("logging")
class LoggingNotifier:
    """Renders an `Alert` to a structured INFO log line via an f-string."""

    type = "logging"

    def __init__(self, config: ConfigBlock) -> None:
        # The logging notifier needs no destination endpoint, so `config` is accepted
        # only to satisfy the locked single-positional-`ConfigBlock` registry
        # construction convention every adapter shares — there is nothing to store.
        del config

    def notify(self, alert: Alert) -> None:
        """Render the alert to a single structured log line.

        Every `Alert` field is interpolated eagerly via an f-string so the emitted
        record is fully rendered (no lazy printf template/args) — the f-string-only
        rule the spec calls load-bearing for this notifier.
        """
        # Labels are rendered in sorted-key order for a deterministic, scannable line.
        labels = ", ".join(f"{key}={value}" for key, value in sorted(alert.labels.items()))
        _LOGGER.info(
            f"ALERT name={alert.name} severity={alert.severity} "
            f"message={alert.message!r} labels={{{labels}}}"
        )
