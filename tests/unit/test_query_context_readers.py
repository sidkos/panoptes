"""Unit tests for `QueryContext.read_gauge` / `read_series` — the store-reader seam (deepening B).

Five MCP-tool sites used to re-implement the same `escape env → build MetricQuery →
store.query → swallow/propagate CapabilityError → latest` sequence — including FIVE copies of
the PromQL-injection escape (a security invariant). These two `QueryContext` methods
concentrate that sequence so the escape lives in ONE place:

- `read_gauge(metric, env, window) -> float | None` — SWALLOWS `CapabilityError` (→ `None`,
  never an invented `0.0`) and returns the latest scalar across the resolved series.
- `read_series(metric, env, window) -> list[MetricSeries]` — PROPAGATES `CapabilityError` (so
  the `compare_envs` fan-out can mark an env down) and returns the raw series list.

Both OWN the security escape: `escape_promql_value(env)` is applied unconditionally, so a
caller can NEVER interpolate `env` raw. The load-bearing test asserts a quote-bearing env
reaches the store in its ESCAPED form.
"""

from datetime import UTC, datetime

import pytest
from core.config import ResolvedConfig
from core.errors import CapabilityError
from core.mcp._metric_helpers import _step_seconds_for
from core.mcp.context import QueryContext
from core.model import CanonicalSignal, MetricQuery, MetricSeries


def _now() -> datetime:
    return datetime.now(UTC)


class _RecordingStore:
    """A store recording each full `MetricQuery` (expr + window + step); returns fixed series."""

    type = "recording"

    def __init__(self, series: list[MetricSeries] | None = None) -> None:
        self.queries: list[MetricQuery] = []
        self._series = series if series is not None else []

    @property
    def exprs(self) -> list[str]:
        """The recorded query exprs (back-compat for the escape/expr assertions)."""
        return [q.expr for q in self.queries]

    def write(self, signals: list[CanonicalSignal]) -> None:  # pragma: no cover - unused
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        self.queries.append(query)
        return self._series


class _PassthroughLikeStore:
    """A store that cannot answer PromQL — raises `CapabilityError` like `PassthroughStore`."""

    type = "passthrough-like"

    def __init__(self) -> None:
        self.queried = False

    def write(self, signals: list[CanonicalSignal]) -> None:  # pragma: no cover - unused
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        self.queried = True
        raise CapabilityError("passthrough store cannot answer PromQL")


def _context(store: object) -> QueryContext:
    """A `QueryContext` over a minimal config whose only populated field is the store."""
    config = ResolvedConfig(
        environments={},
        store=store,  # type: ignore[arg-type]
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )
    return QueryContext(config)


def _series(metric: str, value: float, *, labels: dict[str, str] | None = None) -> MetricSeries:
    return MetricSeries(
        metric=metric,
        labels=labels if labels is not None else {"env": "dev"},
        points=[(datetime(2026, 1, 1, tzinfo=UTC), value)],
    )


# --- read_gauge ------------------------------------------------------------------


def test_read_gauge_returns_latest_scalar() -> None:
    """`read_gauge` returns the latest sample value across the resolved series."""
    store = _RecordingStore(
        [
            MetricSeries(
                metric="panoptes_k8s_node_count",
                labels={"env": "dev"},
                points=[
                    (datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 3.0),
                    (datetime(2026, 1, 1, 1, 0, tzinfo=UTC), 5.0),  # the latest
                ],
            )
        ]
    )
    value = _context(store).read_gauge("panoptes_k8s_node_count", "dev")
    assert value == 5.0


def test_read_gauge_returns_none_when_no_data() -> None:
    """`read_gauge` returns `None` (NOT an invented 0.0) when the store has no data."""
    store = _RecordingStore([])  # the store answers, but with no series
    value = _context(store).read_gauge("panoptes_cost_budget_burn", "dev")
    assert value is None


def test_read_gauge_swallows_capability_error_to_none() -> None:
    """A passthrough store's `CapabilityError` is SWALLOWED by `read_gauge` → `None`."""
    store = _PassthroughLikeStore()
    value = _context(store).read_gauge("panoptes_k8s_node_count", "dev")
    assert value is None
    assert store.queried, "read_gauge must have attempted the store query"


def test_read_gauge_escapes_a_quote_bearing_env_in_the_selector() -> None:
    """SECURITY: a quote-bearing env reaches the store ESCAPED, never breaking out (F7).

    `read_gauge` owns the escape — an env like `a"b` must arrive as the single closed PromQL
    string `env="a\\"b"`, so a caller can never inject a breakout token by passing a raw env.
    """
    store = _RecordingStore([_series("panoptes_k8s_node_count", 1.0)])
    _context(store).read_gauge("panoptes_k8s_node_count", 'a"b')
    assert store.exprs, "read_gauge must have queried the store"
    assert r'env="a\"b"' in store.exprs[0]


# --- read_series -----------------------------------------------------------------


def test_read_series_returns_the_multi_series_list() -> None:
    """`read_series` returns the raw list of series (multiple label-sets preserved)."""
    store = _RecordingStore(
        [
            _series("panoptes_cost_spend", 12.0, labels={"env": "dev", "service": "ec2"}),
            _series("panoptes_cost_spend", 3.0, labels={"env": "dev", "service": "s3"}),
        ]
    )
    series = _context(store).read_series("panoptes_cost_spend", "dev")
    assert len(series) == 2
    assert {s.labels["service"] for s in series} == {"ec2", "s3"}


def test_read_series_propagates_capability_error() -> None:
    """A passthrough store's `CapabilityError` PROPAGATES from `read_series` (not swallowed).

    The `compare_envs` fan-out relies on this: a per-env outage must surface so the env is
    marked down in the comparison, not silently treated as an empty result.
    """
    store = _PassthroughLikeStore()
    with pytest.raises(CapabilityError):
        _context(store).read_series("panoptes_health_up", "dev")


def test_read_series_escapes_a_quote_bearing_env_in_the_selector() -> None:
    """SECURITY: `read_series` likewise escapes a quote-bearing env (F7) — same invariant."""
    store = _RecordingStore([_series("panoptes_cost_spend", 1.0)])
    _context(store).read_series("panoptes_cost_spend", 'a"b')
    assert store.exprs and r'env="a\"b"' in store.exprs[0]


# --- the window arg scopes the query (protects get_slo / compare_envs windowing) -------------


def test_read_series_window_arg_scopes_the_query_window_and_step() -> None:
    """A non-default `window` ('24h') is honored: the MetricQuery spans ~1440min + the right step.

    The `window` arg is what `get_slo`/`compare_envs` pass to scope their reads; this pins that a
    non-default value reaches the store (the query's window span + step both reflect '24h').
    """
    store = _RecordingStore([_series("panoptes_health_up", 1.0)])
    _context(store).read_series("panoptes_health_up", "dev", window="24h")
    assert store.queries, "read_series must query the store"
    query = store.queries[0]
    span_minutes = round((query.window.end - query.window.start).total_seconds() / 60)
    assert span_minutes == 1440, "a '24h' window must span 1440 minutes"
    assert query.step_seconds == _step_seconds_for("24h")


def test_read_gauge_window_arg_scopes_the_query_window() -> None:
    """`read_gauge` likewise honors a non-default `window` (it delegates to `read_series`)."""
    store = _RecordingStore([_series("panoptes_k8s_node_count", 3.0)])
    _context(store).read_gauge("panoptes_k8s_node_count", "dev", window="24h")
    span_minutes = round(
        (store.queries[0].window.end - store.queries[0].window.start).total_seconds() / 60
    )
    assert span_minutes == 1440


# --- NIT: read_series is self-defending on the metric name (F7) ------------------------------


def test_read_series_rejects_a_non_identifier_metric() -> None:
    """NIT: `read_series` validates `metric` against the identifier regex (self-defending seam).

    `read_series` interpolates `metric` UNQUOTED into the selector, so it now rejects a
    breakout metric name itself (a `CapabilityError`) rather than relying solely on callers to
    pre-validate. All current callers pass validated/constant metrics, so behaviour is unchanged.
    """
    store = _RecordingStore([])
    with pytest.raises(CapabilityError):
        _context(store).read_series('up"} or up{', "dev")
    # Rejected before any query — the store was never touched.
    assert store.queries == []


def test_read_gauge_rejects_a_non_identifier_metric() -> None:
    """`read_gauge` inherits the metric-name guard (it delegates to `read_series`)."""
    store = _RecordingStore([])
    with pytest.raises(CapabilityError):
        _context(store).read_gauge('up"} or up{', "dev")
    assert store.queries == []
