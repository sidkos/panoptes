"""Phase 4 unit tests for the `logging` notifier (`core/notifiers/logging_notifier.py`).

Covers (spec `## Tests` → Notifier; playbook Phase 4 table):
- `notify` renders the `Alert` to a structured log line via an **f-string** — the
  rendered content (name / severity / message / labels) is asserted via `caplog`;
- a **no-`%s`/`%d` printf-form** content assertion on the notifier source itself —
  the f-string-only convention is load-bearing here (a `%s` in a `logger.info(...)`
  call would silently lazy-interpolate and is forbidden codebase-wide);
- an **unregistered notifier `type` fails fast** with `UnknownAdapterError` listing
  the available notifier types (built through the registry with an unknown type) —
  closes Open Question 6's "unknown notifier types still fail-fast" on the plane.
"""

import logging
import re
from pathlib import Path

import pytest
from core.errors import UnknownAdapterError
from core.model import Alert
from core.notifiers.logging_notifier import LoggingNotifier
from core.registry import NOTIFIERS


def _alert() -> Alert:
    return Alert(
        name="health-down",
        severity="critical",
        message="dev /health returned 503",
        labels={"env": "dev", "url": "https://dev.example/health"},
    )


def test_notify_renders_alert_fields_via_log_line(caplog: pytest.LogCaptureFixture) -> None:
    """The emitted log record carries every `Alert` field, rendered by an f-string."""
    notifier = LoggingNotifier({"type": "logging"})
    with caplog.at_level(logging.INFO, logger="core.notifiers.logging_notifier"):
        notifier.notify(_alert())
    rendered = caplog.text
    assert "health-down" in rendered
    assert "critical" in rendered
    assert "dev /health returned 503" in rendered
    # Labels (incl. the mandatory `env`) are part of the structured line.
    assert "env" in rendered and "dev" in rendered


def test_notify_log_record_has_no_lazy_printf_args(caplog: pytest.LogCaptureFixture) -> None:
    """The record is fully rendered (f-string), not a printf template + args.

    A `logger.info("... %s", value)` call leaves the message template unrendered with
    `record.args` populated. An f-string call renders eagerly with empty `args`, so
    asserting `record.args` is falsy proves the f-string-only convention held.
    """
    notifier = LoggingNotifier({"type": "logging"})
    with caplog.at_level(logging.INFO, logger="core.notifiers.logging_notifier"):
        notifier.notify(_alert())
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert not record.args
    # The fully-rendered message contains no `%s`/`%d` placeholders.
    assert "%s" not in record.getMessage()
    assert "%d" not in record.getMessage()


def test_notifier_source_has_no_printf_in_logger_calls() -> None:
    """Static guard: the notifier module uses f-strings, never `%s`/`%d` printf form.

    The f-string-only rule is load-bearing for the notifier (spec § Notifier). This
    scans the module source for any `%s`/`%d` token to fail fast if a future edit
    reintroduces a printf-style logger call.
    """
    source = Path("core/notifiers/logging_notifier.py").read_text()
    assert not re.search(r"%[sd]", source), "logging_notifier must use f-strings, not %s/%d"


def test_unregistered_notifier_type_fails_fast_listing_available() -> None:
    """Building an unknown notifier `type` raises `UnknownAdapterError` listing types.

    The `logging` adapter self-registers on import (above), so the registry knows it
    and surfaces it in the message; an unknown `pagerduty` fails fast.
    """
    with pytest.raises(UnknownAdapterError) as excinfo:
        NOTIFIERS.build("pagerduty", {"type": "pagerduty"})
    message = str(excinfo.value)
    assert "pagerduty" in message
    # The available-types list names the registered `logging` notifier.
    assert "logging" in message
    assert "logging" in NOTIFIERS.available()
