"""Unit tests for the `slack` notifier — webhook POST via the shared RestClient.

The slack notifier delivers an `Alert` to a Slack incoming webhook (an alert sink, NOT
an observed system). It POSTs through the shared `core.rest.RestClient`, so httpx is
mocked with `respx` (the RestClient's default client is intercepted globally).

Covers (spec § New notifier adapters / plan Phase 2):
- `notify` POSTs the configured webhook URL with a JSON body carrying the alert;
- a 4xx/5xx surfaces the upstream response BODY via `_format_failure` (not just the
  URL + status code) — so a rejected webhook is diagnosable in one cycle;
- a missing `webhook_url` fails fast at construction.
"""

import httpx
import pytest
import respx
from core.errors import PanoptesError
from core.model import Alert
from core.notifiers.slack import SlackNotifier
from core.registry import NOTIFIERS

_WEBHOOK_URL = "https://hooks.slack.test/services/T000/B000/XXXX"


def _alert() -> Alert:
    return Alert(
        name="HighErrorRate",
        severity="critical",
        message="error rate 0.42 exceeded threshold 0.10",
        labels={"env": "prod", "service": "api"},
    )


def _notifier() -> SlackNotifier:
    return SlackNotifier({"webhook_url": _WEBHOOK_URL})


def test_notifier_is_registered() -> None:
    """Importing the module self-registers the `slack` adapter on NOTIFIERS."""
    assert "slack" in NOTIFIERS.available()


def test_notify_posts_the_webhook_with_a_json_body() -> None:
    """`notify` POSTs the configured webhook URL; the JSON body carries the alert text."""
    with respx.mock:
        route = respx.post(_WEBHOOK_URL).mock(return_value=httpx.Response(200, text="ok"))
        _notifier().notify(_alert())
        assert route.called, "the slack webhook must be POSTed"
        request = route.calls.last.request
        # The body is JSON (Slack incoming-webhook contract); it carries the alert message.
        body = request.content.decode("utf-8")
        assert "HighErrorRate" in body
        assert "error rate 0.42 exceeded threshold 0.10" in body


def test_notify_surfaces_response_body_on_http_error() -> None:
    """A 4xx/5xx surfaces the upstream response BODY in the raised error (not just status).

    `str(httpx.HTTPStatusError)` carries only URL + status; the actionable detail (Slack's
    rejection reason) lives in the body. `_format_failure` appends it, so the body text
    must appear in the raised PanoptesError.
    """
    with respx.mock:
        respx.post(_WEBHOOK_URL).mock(
            return_value=httpx.Response(400, text="invalid_payload: no_text")
        )
        with pytest.raises(PanoptesError) as excinfo:
            _notifier().notify(_alert())
    message = str(excinfo.value)
    # The upstream body (the rejection reason) is surfaced, not just the status code.
    assert "invalid_payload" in message


def test_notify_surfaces_transport_error() -> None:
    """A connection/transport error surfaces as a PanoptesError (never a raw httpx error)."""
    with respx.mock:
        respx.post(_WEBHOOK_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(PanoptesError):
            _notifier().notify(_alert())


def test_missing_webhook_url_fails_fast() -> None:
    """A config without `webhook_url` fails fast at construction (registry contract)."""
    with pytest.raises(PanoptesError):
        SlackNotifier({})
