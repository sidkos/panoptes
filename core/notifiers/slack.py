"""The `slack` notifier — delivers an `Alert` to a Slack incoming webhook.

`notify(alert)` POSTs a JSON body to the configured Slack webhook URL (spec § New notifier
adapters). The webhook is an alert SINK, NOT an observed system — it carries no observed
credential and is read-only w.r.t. the systems Panoptes watches.

The transport runs through the shared `core.rest.RestClient`: its `send()` seam applies
`raise_for_status` + the shared two-branch failure surfacing, so a 4xx/5xx surfaces the
upstream response BODY (Slack's rejection reason — `invalid_payload`, `no_text`, …) via
`_format_failure`, not just the bare URL + status code. httpx is mocked with `respx` in
tests (the RestClient's default client is intercepted globally); the client is also an
explicit constructor seam for explicit test control.

A failed POST raises a typed `PanoptesError` (carrying the surfaced body) — a delivery
failure must stay visible to the caller, never be swallowed.
"""

import httpx

from core.model import Alert
from core.registry import NOTIFIERS, ConfigBlock
from core.rest import RestClient
from core.validation import require_str_field


@NOTIFIERS.register("slack")
class SlackNotifier:
    """POSTs an `Alert` to a Slack incoming webhook via the shared RestClient."""

    type = "slack"

    def __init__(
        self,
        config: ConfigBlock,
        client: httpx.Client | None = None,
    ) -> None:
        """Read the webhook URL from config; accept an injectable httpx client seam.

        The `client` seam mirrors the sentry/VM-store pattern: under `respx` the default
        `httpx.Client()` is intercepted globally, so production passes none and tests need
        not inject one — the seam exists for explicit control.
        """
        # `webhook_url` is mandatory — the Slack incoming-webhook endpoint to POST to.
        self._webhook_url = require_str_field(config, "webhook_url", self.type)
        self._rest = RestClient(client)

    def notify(self, alert: Alert) -> None:
        """POST the alert to the Slack webhook as a JSON body.

        The body uses Slack's `text` field (the incoming-webhook contract) carrying a
        fully-rendered f-string of every alert field. A 4xx/5xx or transport error raises a
        `PanoptesError` whose message carries the surfaced upstream body (via the shared
        `RestClient.send` + `_format_failure`) — the failure is visible, never swallowed.
        """
        # Labels rendered in sorted-key order for a deterministic, scannable line.
        labels = ", ".join(f"{key}={value}" for key, value in sorted(alert.labels.items()))
        text = f"*[{alert.severity}] {alert.name}*\n{alert.message}\nlabels: {{{labels}}}"
        payload = {"text": text}

        def _post(http: httpx.Client) -> httpx.Response:
            return http.post(self._webhook_url, json=payload)

        # `send` applies raise_for_status + the shared body-surfacing failure formatter, so
        # a rejected webhook is diagnosable in one cycle (the response body names the cause).
        self._rest.send(_post, prefix="slack webhook post failed", identifier=self._webhook_url)
