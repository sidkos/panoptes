"""Unit tests for the `sns` notifier — boto3 publish via the injectable client seam.

The sns notifier delivers an `Alert` to a Panoptes-OWNED SNS topic (the alert channel,
NOT an observed system). boto3 is exercised through `botocore.stub.Stubber` against an
INJECTED sns client (the cloudwatch `sts_client`-seam pattern) for the TopicArn + error
paths, and through a small typed recording fake for the message-rendering assertion — so
no AWS call is ever made.

Covers (spec § New notifier adapters / plan Phase 2):
- `notify` calls `publish` with the configured `TopicArn` + a Subject + a rendered
  f-string `Message` carrying every alert field (and NO printf `%`-placeholder leak —
  the f-string-only convention is load-bearing);
- a `ClientError` from `publish` SURFACES (propagates) — a delivery failure must be
  visible, never swallowed;
- a missing `topic_arn` fails fast at construction (the registry construction contract).

Type-stub-only imports (`mypy_boto3_sns`) are guarded behind `if TYPE_CHECKING:` — they
are dev-only and must never run at slim-CI runtime; the client factories below are typed
against them so the injected-seam argument stays precisely typed under `mypy --strict`.
"""

from typing import TYPE_CHECKING, cast

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from core.errors import PanoptesError
from core.model import Alert
from core.notifiers.sns import SnsNotifier
from core.registry import NOTIFIERS

if TYPE_CHECKING:
    from mypy_boto3_sns import SNSClient

_REGION = "us-east-1"
# A Panoptes-owned topic ARN (the alert channel — never an observed resource).
_TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:panoptes-alerts"


def _sns_client() -> "SNSClient":
    """A real boto3 sns client a Stubber attaches to (no network is made)."""
    return boto3.client("sns", region_name=_REGION)


def _alert() -> Alert:
    return Alert(
        name="HighErrorRate",
        severity="critical",
        message="error rate 0.42 exceeded threshold 0.10",
        labels={"env": "prod", "service": "api"},
    )


def _notifier(sns_client: "SNSClient") -> SnsNotifier:
    return SnsNotifier({"topic_arn": _TOPIC_ARN}, sns_client=sns_client)


class _RecordingSnsClient:
    """A typed minimal fake recording the `publish` kwargs (no AWS call).

    Implements only the one method the notifier uses, so the message-rendering test can
    inspect the published `Subject`/`Message` without a Stubber's request-param plumbing.
    Cast to `SNSClient` at the injection site (it satisfies the seam structurally for the
    single call the notifier makes).
    """

    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    def publish(self, **kwargs: object) -> dict[str, str]:
        self.captured.update(kwargs)
        return {"MessageId": "recorded"}


def test_notifier_is_registered() -> None:
    """Importing the module self-registers the `sns` adapter on NOTIFIERS."""
    assert "sns" in NOTIFIERS.available()


def test_notify_publishes_to_configured_topic_arn() -> None:
    """`notify` calls `publish` with exactly the configured `TopicArn`."""
    sns = _sns_client()
    stub = Stubber(sns)
    # The stub asserts the request carries our TopicArn (a mismatch raises in the stub).
    stub.add_response(
        "publish",
        {"MessageId": "msg-1"},
        expected_params={"TopicArn": _TOPIC_ARN, "Subject": ANY, "Message": ANY},
    )
    stub.activate()
    _notifier(sns).notify(_alert())
    # No leftover queued responses → publish was called exactly once.
    stub.assert_no_pending_responses()


def test_notify_message_is_a_rendered_f_string_with_no_printf_placeholder() -> None:
    """The published `Message` carries every alert field, with NO `%s`/`%d` placeholder.

    The recorded request body must be a fully-rendered f-string (the f-string-only rule):
    a leaked printf placeholder would mean the message was lazily templated.
    """
    recording = _RecordingSnsClient()
    alert = _alert()
    # The recording fake satisfies the SNSClient seam for the single publish() call.
    _notifier(cast("SNSClient", recording)).notify(alert)

    message = recording.captured["Message"]
    assert isinstance(message, str)
    # Every alert field is rendered into the message.
    assert alert.name in message
    assert alert.severity in message
    assert alert.message in message
    assert "env=prod" in message
    # No printf-style placeholder leaked (the f-string-only convention).
    assert "%s" not in message
    assert "%d" not in message
    # The Subject is present + non-empty.
    subject = recording.captured["Subject"]
    assert isinstance(subject, str) and subject


def test_notify_surfaces_client_error_from_publish() -> None:
    """A `ClientError` raised by `publish` propagates — a delivery failure stays visible."""
    sns = _sns_client()
    stub = Stubber(sns)
    stub.add_client_error("publish", "AuthorizationError", "not authorized to publish")
    stub.activate()
    # The error must surface (not be swallowed). It propagates as the botocore ClientError
    # (or a PanoptesError wrapping it) — either way the failure is visible to the caller.
    with pytest.raises((ClientError, PanoptesError)):
        _notifier(sns).notify(_alert())


def test_missing_topic_arn_fails_fast() -> None:
    """A config without `topic_arn` fails fast at construction (registry contract)."""
    with pytest.raises(PanoptesError):
        SnsNotifier({})
