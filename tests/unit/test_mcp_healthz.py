"""Unit tests for the MCP HTTP `/healthz` route (spec § MCP HTTP face).

`/healthz` is the ONLY unauthenticated route on the MCP HTTP face: the nginx ingress
exempts it from forward-auth so the kubelet liveness probe + the ingress can check the
server is up WITHOUT a token. It returns `{"status": "ok"}` (200) and carries NO signal
data — it must never leak any tool result, metric, or config detail.

The route is registered on the FastMCP instance during `build_server`; this test drives
the rendered Starlette HTTP app with Starlette's SYNCHRONOUS `TestClient` (no `asyncio` in
the test body, no socket bound), asserting the response shape + the no-signal-data promise.
The auth gate stays the ingress's job — the server validates NO token (so `/healthz`, like
every route here, answers without one; the difference is the ingress GATES the others).
"""

import warnings
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from core.config import ResolvedConfig, ResolvedEnvironment, ResolvedSource
from core.mcp.server import build_server
from core.model import (
    Alert,
    CanonicalSignal,
    MetricQuery,
    MetricSeries,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.planes.notifier import Notifier

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    from starlette.testclient import TestClient

    # The Starlette ASGI app callable shape `TestClient` accepts. The rendered FastMCP HTTP
    # app satisfies it structurally; `http_app()` types it as `object` (the `FastMCP[Any]`
    # boundary the adapter confines), so it is cast to this shape at the TestClient seam.
    _AsgiApp = Callable[
        [
            MutableMapping[str, object],
            Callable[[], Awaitable[MutableMapping[str, object]]],
            Callable[[MutableMapping[str, object]], Awaitable[None]],
        ],
        Awaitable[None],
    ]


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
    fetch_when_unreachable = False

    def __init__(self, source_type: str, capabilities: set[SignalKind]) -> None:
        self.type = source_type
        self._capabilities = capabilities

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return []

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


class _FakeStore:
    type = "fake"

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return []


class _NoopNotifier:
    type = "logging"

    def notify(self, alert: Alert) -> None:
        return None


def _config() -> ResolvedConfig:
    notifiers: list[Notifier] = [_NoopNotifier()]
    return ResolvedConfig(
        environments={
            "dev": ResolvedEnvironment(
                name="dev",
                enabled=True,
                sources=[
                    ResolvedSource(
                        source=_FakeSource("http-health", {SignalKind.METRIC}),
                        fetch_timeout_seconds=30,
                        poll_interval_seconds=60,
                    )
                ],
            )
        },
        store=_FakeStore(),
        notifiers=notifiers,
        dashboard_packs=[],
        slos=[],
        mcp={"transport": "http"},
    )


def _http_test_client() -> "TestClient":
    """Build the MCP HTTP app from a default config and wrap it in a SYNC TestClient.

    `build_server` registers the `/healthz` custom route on the FastMCP instance; the
    server's `http_app()` renders the Starlette app the streamable-HTTP transport serves.
    Starlette's `TestClient` drives it synchronously (no socket bound, no asyncio in the
    test body). The httpx-deprecation warning the TestClient emits is suppressed here — it
    is unrelated to the assertions and noisy under `-W error`.
    """
    server = build_server(_config())
    # `http_app()` types the app as `object` (the `FastMCP[Any]` boundary); cast it to the
    # ASGI-app shape TestClient accepts (it satisfies it structurally).
    app = cast("_AsgiApp", server.http_app())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

        return TestClient(app)


def test_healthz_returns_ok_without_auth() -> None:
    """`GET /healthz` returns 200 `{"status": "ok"}` with no auth header."""
    client = _http_test_client()
    # No Authorization / identity header — /healthz is the unauthenticated liveness route.
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok"}


def test_healthz_returns_no_signal_data() -> None:
    """`/healthz` carries NO signal data — only the bare status, no metrics/tools/config.

    A liveness route must never leak observability data; the body is EXACTLY the status
    object, with no extra keys that could carry a metric value, a tool result, or config.
    """
    client = _http_test_client()
    body = client.get("/healthz").json()
    # The body has exactly one key — `status` — and nothing signal-bearing.
    assert set(body.keys()) == {"status"}
    forbidden_substrings = ("metric", "panoptes_", "series", "value", "env", "token", "arn")
    rendered = client.get("/healthz").text.lower()
    for forbidden in forbidden_substrings:
        assert forbidden not in rendered, f"/healthz body leaked signal-ish token {forbidden!r}"


def test_healthz_only_get_is_allowed() -> None:
    """`/healthz` is a GET liveness probe — a POST is not the health contract."""
    client = _http_test_client()
    response = client.post("/healthz")
    # A non-GET method is not allowed on the liveness route (405), proving the route is
    # GET-scoped (the kubelet probe + the ingress liveness check use GET).
    assert response.status_code == 405
