"""The `sns` notifier — delivers an `Alert` to a Panoptes-OWNED SNS topic.

`notify(alert)` calls `sns.publish(TopicArn=…, Subject=…, Message=…)` against a
Panoptes-owned alert topic (spec § New notifier adapters). This is a legitimate write to
Panoptes' OWN alert channel — NOT an observed system — so it is the one place
`sns.publish` is allowed: the no-write guard's suppression allowlist is PATH-SCOPED to
this file (`core/notifiers/sns.py`), so a `publish` anywhere else (esp. `core/sources/`)
still red-bars. The guard's intent is "no writes to OBSERVED systems"; this channel is
Panoptes' own.

**AWS auth via an injectable seam (the cloudwatch `sts_client`-seam pattern).** The sns
client is obtained via an overridable constructor param (default `None`, so the registry's
single-positional `cls(config)` still works); a unit test injects a `botocore.stub.Stubber`
client so `Stubber` attaches to the exact instance the notifier uses. A real run leaves it
`None` and the client is built lazily on first use.

A `botocore` `ClientError` from `publish` is NOT swallowed: a delivery failure must be
visible to the caller (the collector surfaces it), unlike a source-side reachability error
which is converted to a health result.

`Message` is rendered as an **f-string** (NO printf `%s`/`%d`) — the f-string-only
convention the codebase enforces, mirroring the logging notifier.

Type-stub-only imports (`mypy_boto3_sns`) are guarded behind `if TYPE_CHECKING:` — they
are NOT installed in slim CI, so a bare runtime import would crash; the runtime `boto3`
import is unconditional (it is already a project dependency).
"""

from typing import TYPE_CHECKING

import boto3

from core.model import Alert
from core.registry import NOTIFIERS, ConfigBlock
from core.validation import require_str_field

if TYPE_CHECKING:
    # Type-stub-only import — present at type-check time (boto3-stubs[sns] is a dev dep)
    # but NOT installed in slim CI, so it must never run at import time.
    from mypy_boto3_sns import SNSClient


@NOTIFIERS.register("sns")
class SnsNotifier:
    """Publishes an `Alert` to a Panoptes-owned SNS topic via an injectable client seam."""

    type = "sns"

    def __init__(
        self,
        config: ConfigBlock,
        sns_client: "SNSClient | None" = None,
    ) -> None:
        """Read the topic ARN from config; accept an injectable boto3 sns client seam.

        The `sns_client` param defaults to `None`, so the registry's single-positional
        `cls(config)` still constructs the notifier; a test injects a stubbed client so
        `botocore.stub.Stubber` attaches to the exact instance. A real run leaves it `None`
        and the client is built lazily from `region` (optional) on first publish.
        """
        # `topic_arn` is mandatory — the Panoptes-owned alert topic to publish to.
        self._topic_arn = require_str_field(config, "topic_arn", self.type)
        # Optional region override (the ARN already encodes a region; an explicit `region`
        # config lets the client target it without relying on the default chain/profile).
        region = config.get("region")
        self._region = region if isinstance(region, str) and region else None
        # The injected seam (None in production; a stubbed client in tests).
        self._sns_client = sns_client

    def notify(self, alert: Alert) -> None:
        """Publish the alert to the configured SNS topic.

        The `Subject` is a short severity+name line (SNS caps Subject at 100 chars); the
        `Message` body is a fully-rendered f-string carrying every alert field. A
        `botocore` `ClientError` propagates — a delivery failure must stay visible.
        """
        # Labels rendered in sorted-key order for a deterministic, scannable body.
        labels = ", ".join(f"{key}={value}" for key, value in sorted(alert.labels.items()))
        # Subject: severity + name, trimmed to SNS's 100-char Subject limit.
        subject = f"[{alert.severity}] {alert.name}"[:100]
        message = (
            f"ALERT name={alert.name} severity={alert.severity} "
            f"message={alert.message} labels={{{labels}}}"
        )
        # Not wrapped in try/except: a ClientError must surface so the failure is visible.
        self._sns().publish(TopicArn=self._topic_arn, Subject=subject, Message=message)

    def _sns(self) -> "SNSClient":
        """Return the injected sns client, or lazily build one from the optional region."""
        if self._sns_client is not None:
            return self._sns_client
        if self._region is not None:
            self._sns_client = boto3.client("sns", region_name=self._region)
        else:
            self._sns_client = boto3.client("sns")
        return self._sns_client
