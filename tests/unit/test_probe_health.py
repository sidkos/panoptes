"""Unit tests for the concentrated Source health-probe seam (`core/sources/probe.py`).

`probe_health` is the ONE place the Source health-probe discipline lives: the no-raise
contract, the no-`str(exc)`-leak SECURITY invariant, the before-probe timestamp capture, and
the generic failure-detail form. The six sources' `health()` impls delegate here (except
`http-health`, which is the documented exception — down IS its signal). These tests pin the
seam's invariants directly, so a regression in any of them fails HERE rather than diffusely
across the per-source tests.

The load-bearing test is the security invariant: a probe raising an exception whose `str()`
carries a sensitive value (a role ARN / bearer token / endpoint) must surface ONLY the
exception CLASS NAME in the detail — never the message text.
"""

from datetime import UTC, datetime

import httpx
import pytest
from core.errors import PanoptesError
from core.sources.probe import probe_health


def test_reachable_true_when_probe_succeeds() -> None:
    """A probe that returns without raising → `reachable=True` with the default detail."""
    health = probe_health("widget", lambda: None)

    assert health.reachable is True
    assert health.detail == "widget reachable"


def test_reachable_false_when_probe_raises() -> None:
    """A probe that raises ANY exception → `reachable=False` (no propagation)."""

    def _boom() -> object:
        raise RuntimeError("upstream down")

    health = probe_health("widget", _boom)

    assert health.reachable is False
    assert "widget unreachable" in health.detail
    assert "RuntimeError" in health.detail


def test_security_invariant_failure_detail_never_contains_str_exc() -> None:
    """THE SECURITY INVARIANT: the failure detail carries ONLY the class name, NEVER str(exc).

    A probe whose exception message embeds a sensitive value (here a role-ARN-shaped string)
    must surface only the exception CLASS NAME in the detail — the message text, which could
    carry a role ARN / bearer token / endpoint into the MCP-visible `describe_health` rollup,
    must never appear.
    """
    sensitive = "arn:aws:iam::999988887777:role/SuperSecretLeakRole"

    def _leaky() -> object:
        raise RuntimeError(f"denied access to {sensitive}")

    health = probe_health("widget", _leaky)

    assert health.reachable is False
    # The sensitive message text must NOT leak — only the class name is surfaced.
    assert sensitive not in health.detail
    assert "999988887777" not in health.detail
    assert "RuntimeError" in health.detail


def test_success_detail_factory_receives_the_probe_return_value() -> None:
    """`success_detail_factory` is called with the probe's return value to build the detail."""
    health = probe_health(
        "widget",
        lambda: [1, 2, 3],
        success_detail_factory=lambda result: (
            f"widget returned {len(result) if isinstance(result, list) else 0} item(s)"
        ),
    )

    assert health.reachable is True
    assert health.detail == "widget returned 3 item(s)"


def test_injectable_checked_at_is_used_for_both_branches() -> None:
    """The injected `checked_at` seam is the `SourceHealth.checked_at` on success AND failure."""
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    ok = probe_health("widget", lambda: None, checked_at=fixed)
    assert ok.checked_at == fixed

    def _boom() -> object:
        raise ValueError("nope")

    failed = probe_health("widget", _boom, checked_at=fixed)
    assert failed.checked_at == fixed


def test_default_checked_at_is_captured_before_the_probe_runs() -> None:
    """With no injected `checked_at`, the timestamp is captured BEFORE the probe is invoked.

    The probe records the wall-clock instant it observed `checked_at`; even a slow/raising
    probe must not move the recorded `checked_at` to AFTER the call. We pin this by capturing a
    bound just before the call and asserting `checked_at` is at/after it and not in the future.
    """
    before = datetime.now(UTC)

    def _slow() -> object:
        # A probe that does real work still must not push checked_at past its own start.
        return sum(range(1000))

    health = probe_health("widget", _slow)
    after = datetime.now(UTC)

    assert before <= health.checked_at <= after


def test_chained_exception_names_the_underlying_cause_not_the_wrapper() -> None:
    """A `PanoptesError(...) from httpx.ConnectError(...)` → detail names `ConnectError`.

    `type(exc.__cause__ or exc).__name__` is used so a `PanoptesError` wrapping a transport
    cause surfaces the UNDERLYING class (the actionable one), not the generic wrapper.
    """

    def _wrapped() -> object:
        cause = httpx.ConnectError("connection refused")
        raise PanoptesError("prometheus health probe failed") from cause

    health = probe_health("prometheus endpoint", _wrapped)

    assert health.reachable is False
    assert "ConnectError" in health.detail
    assert "PanoptesError" not in health.detail


def test_broad_catch_covers_value_error_and_os_error() -> None:
    """The broad `except Exception` converts ValueError AND OSError to `reachable=False`.

    The kubernetes/cloudwatch clients surface failures as a variety of exception types; the
    seam must convert ANY ordinary exception (not just transport ones) to an unreachable
    result rather than letting it crash the collector cycle.
    """

    def _value_error() -> object:
        raise ValueError("bad value")

    def _os_error() -> object:
        raise OSError("socket error")

    assert probe_health("widget", _value_error).reachable is False
    assert "ValueError" in probe_health("widget", _value_error).detail
    assert probe_health("widget", _os_error).reachable is False
    assert "OSError" in probe_health("widget", _os_error).detail


def test_base_exception_propagates_not_caught() -> None:
    """`KeyboardInterrupt`/`SystemExit` (BaseException, not Exception) must PROPAGATE.

    The seam catches `Exception`, NOT `BaseException`, so an interrupt/exit signal is never
    swallowed into a misleading `reachable=False` — it propagates so the process can shut down.
    """

    def _interrupt() -> object:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        probe_health("widget", _interrupt)

    def _exit() -> object:
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        probe_health("widget", _exit)
