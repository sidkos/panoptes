"""Phase 3 unit tests for the `http-health` source.

Covers (spec `## Tests` → Sources, http-health bullet):
- **up** — a 200 response → `panoptes_health_up == 1.0`, latency recorded;
- **down** — a 5xx status, a connection-refused error, and a timeout all map to
  `panoptes_health_up == 0.0` with a latency still recorded;
- **latency value present** on both the up and down paths;
- the two emitted metrics carry the **exact** `{env, url}` label set;
- `capabilities() == {METRIC}`.

All httpx is mocked with `respx` (Risk R3). The probe is point-in-time, so the
asserted contract is structural (label sets, up-value, presence of a latency) rather
than a fixed latency number.
"""

import httpx
import pytest
import respx
from core.errors import PanoptesError
from core.model import CanonicalSignal, MetricSignal, SignalKind, TimeWindow
from core.sources.http_health import HttpHealthSource

_HEALTH_URL = "http://app.internal/health"
_ENV = "dev"
_WINDOW = TimeWindow.last(minutes=5)


def _source(client: httpx.Client | None = None) -> HttpHealthSource:
    return HttpHealthSource({"url": _HEALTH_URL, "env": _ENV}, client=client)


def _metrics_by_name(signals: list[CanonicalSignal]) -> dict[str, MetricSignal]:
    by_name: dict[str, MetricSignal] = {}
    for signal in signals:
        assert isinstance(signal, MetricSignal)
        by_name[signal.name] = signal
    return by_name


def test_capabilities_is_metric_only() -> None:
    assert _source().capabilities() == {SignalKind.METRIC}


def test_requires_url_and_env() -> None:
    with pytest.raises(PanoptesError):
        HttpHealthSource({"env": _ENV})
    with pytest.raises(PanoptesError):
        HttpHealthSource({"url": _HEALTH_URL})


@respx.mock
def test_up_200_emits_up_one_with_exact_labels() -> None:
    respx.get(_HEALTH_URL).mock(return_value=httpx.Response(200, text="ok"))

    signals = _source().fetch(_WINDOW)

    metrics = _metrics_by_name(signals)
    assert set(metrics) == {"panoptes_health_up", "panoptes_health_latency_ms"}
    up = metrics["panoptes_health_up"]
    latency = metrics["panoptes_health_latency_ms"]
    assert up.value == 1.0
    # Exact derived-metric label set on BOTH metrics (spec): {env, url}.
    assert up.labels == {"env": _ENV, "url": _HEALTH_URL}
    assert latency.labels == {"env": _ENV, "url": _HEALTH_URL}
    # Latency is present and non-negative on the up path.
    assert latency.value >= 0.0


@respx.mock
def test_down_5xx_emits_up_zero_with_latency_recorded() -> None:
    respx.get(_HEALTH_URL).mock(return_value=httpx.Response(503, text="unavailable"))

    metrics = _metrics_by_name(_source().fetch(_WINDOW))

    assert metrics["panoptes_health_up"].value == 0.0
    # Latency is still recorded even though the endpoint is down.
    assert metrics["panoptes_health_latency_ms"].value >= 0.0
    assert metrics["panoptes_health_latency_ms"].labels == {"env": _ENV, "url": _HEALTH_URL}


@respx.mock
def test_down_connection_refused_emits_up_zero_with_latency() -> None:
    respx.get(_HEALTH_URL).mock(side_effect=httpx.ConnectError("refused"))

    metrics = _metrics_by_name(_source().fetch(_WINDOW))

    assert metrics["panoptes_health_up"].value == 0.0
    assert metrics["panoptes_health_latency_ms"].value >= 0.0


@respx.mock
def test_down_timeout_emits_up_zero_with_latency() -> None:
    respx.get(_HEALTH_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    metrics = _metrics_by_name(_source().fetch(_WINDOW))

    assert metrics["panoptes_health_up"].value == 0.0
    assert metrics["panoptes_health_latency_ms"].value >= 0.0


@respx.mock
def test_health_reachable_on_200() -> None:
    respx.get(_HEALTH_URL).mock(return_value=httpx.Response(200))

    health = _source().health()

    assert health.reachable is True
    assert _HEALTH_URL in health.detail


@respx.mock
def test_health_unreachable_on_5xx() -> None:
    respx.get(_HEALTH_URL).mock(return_value=httpx.Response(500))

    health = _source().health()

    assert health.reachable is False
    assert _HEALTH_URL in health.detail
