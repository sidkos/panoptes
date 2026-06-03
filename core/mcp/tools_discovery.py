"""MCP discovery / parity tools — the read-only "what can I see?" surface.

These three tools answer discovery questions over the resolved config + dashboard
catalog (spec `## API Surface` → MCP server → Discovery / parity):

- `describe_signal_catalog` — the environments, the configured sources + their
  `capabilities()`, the known derived-metric names, and the dashboard ids.
- `list_dashboards` — the dashboard catalog (core + injected consumer packs) as
  catalog summaries (NOT the verbatim Grafana layout JSON; Open Question 3).
- `get_dashboard_data` — per panel: the panel title + its PromQL target(s) + the
  **executed series from the store** (Open Question 3 / Risk R5). The panel's
  `$env` template variable is substituted with the requested `env` before the
  PromQL is executed, so the returned series is the data the operator would see in
  Grafana for that env.

Each function takes an explicit **`QueryContext`** seam (a small read-only view of
the resolved config) rather than reaching for `ResolvedConfig`'s shape or module
globals, so they are unit-testable from a minimal hand-built config without standing
up FastMCP. The server module (`core/mcp/server.py`) builds one `QueryContext` and
binds it into thin FastMCP-registered wrappers. (`list_dashboards` still takes the
resolved pack list directly — it needs nothing else from the context.)

IMPORTANT (FastMCP / PEP-563): this module must NOT add
`from __future__ import annotations` — deferred annotations break FastMCP's schema
generation for the nested-`TypedDict` return shapes defined here.
"""

import json
from typing import TypedDict

from core.errors import CapabilityError
from core.mcp.context import QueryContext
from core.model import (
    DashboardPack,
    MetricQuery,
    MetricSeries,
    TimeWindow,
)

# The known derived-metric names Panoptes' sources emit (spec § Data Model →
# Derived metrics). Surfaced in the catalog so an LLM knows what it can query
# without guessing native upstream metric names.
_KNOWN_DERIVED_METRICS = (
    "panoptes_sentry_incident_count",
    "panoptes_log_error_rate",
    "panoptes_health_up",
    "panoptes_health_latency_ms",
)

# The window + step `get_dashboard_data` executes each panel target over. v0.1
# fixes a sensible default range (the data is the proof, not the time selector);
# the integration parity test pins both faces to this same TimeWindow + step.
_DASHBOARD_QUERY_MINUTES = 60
_DASHBOARD_QUERY_STEP_SECONDS = 60


class SourceCapabilityInfo(TypedDict):
    """One configured source in the signal catalog: its type + capability set."""

    type: str
    capabilities: list[str]


class SignalCatalog(TypedDict):
    """The discovery answer: what environments / sources / metrics / dashboards exist."""

    environments: list[str]
    sources: list[SourceCapabilityInfo]
    metrics: list[str]
    dashboards: list[str]


class DashboardSummary(TypedDict):
    """A catalog summary for one dashboard pack (NOT the verbatim layout JSON)."""

    id: str
    tier: str


class MetricSeriesData(TypedDict):
    """A serialized `MetricSeries` (FastMCP-serializable point tuples as lists)."""

    metric: str
    labels: dict[str, str]
    points: list[list[float]]


class PromTargetData(TypedDict):
    """One PromQL target of a panel + the executed series it resolved to."""

    expr: str
    series: list[MetricSeriesData]


class PanelData(TypedDict):
    """One dashboard panel: its title + its (executed) PromQL targets."""

    title: str
    targets: list[PromTargetData]


class DashboardData(TypedDict):
    """The executed view of one dashboard for one env: per-panel title + data."""

    id: str
    env: str
    panels: list[PanelData]


def describe_signal_catalog(context: QueryContext) -> SignalCatalog:
    """List the environments, configured sources + capabilities, metrics, dashboards.

    Args:
        context: The query context (sources are live only for enabled envs; the
            catalog lists EVERY declared env, hence `all_envs`/`env_names`).

    Returns:
        A `SignalCatalog` describing the full discoverable surface.
    """
    sources: list[SourceCapabilityInfo] = []
    for environment in context.all_envs():
        for resolved_source in environment.sources:
            sources.append(
                SourceCapabilityInfo(
                    type=resolved_source.source.type,
                    capabilities=sorted(
                        kind.value for kind in resolved_source.source.capabilities()
                    ),
                )
            )
    return SignalCatalog(
        environments=context.env_names(),
        sources=sources,
        metrics=list(_KNOWN_DERIVED_METRICS),
        dashboards=[pack.id for pack in context.dashboard_packs],
    )


def list_dashboards(packs: list[DashboardPack]) -> list[DashboardSummary]:
    """Return the dashboard catalog (core + injected consumer packs) as summaries.

    Args:
        packs: The resolved dashboard-pack catalog.

    Returns:
        One `DashboardSummary` per pack — id + tier, never the verbatim layout JSON
        (Open Question 3: only `get_dashboard_data` returns executed series).
    """
    return [DashboardSummary(id=pack.id, tier=pack.tier) for pack in packs]


def get_dashboard_data(
    dashboard_id: str,
    env: str,
    context: QueryContext,
) -> DashboardData:
    """Execute one dashboard's panels for `env` and return title + PromQL + series.

    Resolves the pack by id, reads its dashboard JSON, and for each panel target
    substitutes the `$env` template variable with `env`, executes the resulting
    PromQL against the store, and attaches the resolved series (Open Q3 / Risk R5).

    Args:
        dashboard_id: The pack id to render (must exist in the catalog).
        env: The environment to substitute into each panel's `$env` template var.
        context: The query context (its `store` answers the PromQL; its
            `dashboard_packs` is the catalog `dashboard_id` is resolved against).

    Returns:
        A `DashboardData` with per-panel titles + executed PromQL + series.

    Raises:
        CapabilityError: `dashboard_id` is not in the catalog (the spec defines no
            separate `NotFoundError`; an unknown id is surfaced explicitly, never
            silent/None).
    """
    packs = context.dashboard_packs
    pack = next((p for p in packs if p.id == dashboard_id), None)
    if pack is None:
        available = ", ".join(p.id for p in packs) or "(none)"
        raise CapabilityError(
            f"No dashboard with id '{dashboard_id}' is in the catalog. "
            f"Available dashboards: {available}."
        )

    layout = _read_dashboard_layout(pack)
    window = TimeWindow.last(minutes=_DASHBOARD_QUERY_MINUTES)
    panels: list[PanelData] = []
    raw_panels = layout.get("panels")
    raw_panel_list = raw_panels if isinstance(raw_panels, list) else []
    for raw_panel in raw_panel_list:
        if not isinstance(raw_panel, dict):
            continue
        title = raw_panel.get("title")
        panel_title = title if isinstance(title, str) else ""
        targets: list[PromTargetData] = []
        raw_targets = raw_panel.get("targets")
        if isinstance(raw_targets, list):
            for raw_target in raw_targets:
                if not isinstance(raw_target, dict):
                    continue
                raw_expr = raw_target.get("expr")
                if not isinstance(raw_expr, str):
                    continue
                # Substitute the Grafana `$env` template var with the requested env,
                # so the PromQL executed against the store matches what the dashboard
                # would render for that env.
                expr = _substitute_env(raw_expr, env)
                series = context.store.query(
                    MetricQuery(
                        expr=expr, window=window, step_seconds=_DASHBOARD_QUERY_STEP_SECONDS
                    )
                )
                targets.append(
                    PromTargetData(expr=expr, series=[_serialize_series(s) for s in series])
                )
        panels.append(PanelData(title=panel_title, targets=targets))

    return DashboardData(id=dashboard_id, env=env, panels=panels)


def _read_dashboard_layout(pack: DashboardPack) -> dict[str, object]:
    """Read + JSON-parse a pack's dashboard layout.

    For a core pack `json_path` points either directly at a `dashboard.json` file or
    at the pack dir containing one; both are resolved here so the Phase-6 inline
    fixture (a direct file) and the Phase-5 shipped layout (a dir) both work.

    Raises:
        CapabilityError: the layout file cannot be read or is not a JSON object.
    """
    path = pack.json_path
    if path.is_dir():
        path = path / "dashboard.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityError(
            f"Dashboard '{pack.id}' layout at {path} could not be read: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise CapabilityError(f"Dashboard '{pack.id}' layout at {path} is not a JSON object.")
    return raw


def _substitute_env(expr: str, env: str) -> str:
    """Replace the Grafana `$env` template variable in a PromQL expr with `env`.

    Grafana panels reference the env template var as `$env` (and a regex match
    `=~"$env"`); v0.1 substitutes the literal `$env` token so the executed PromQL
    is scoped to the requested environment.
    """
    return expr.replace("$env", env)


def _serialize_series(series: MetricSeries) -> MetricSeriesData:
    """Serialize a `MetricSeries` into a FastMCP-serializable TypedDict.

    Points are `(datetime, float)` tuples; they are serialized as
    `[epoch_seconds, value]` float lists so the return shape stays JSON-friendly
    and precisely typed (no `Any`).
    """
    return MetricSeriesData(
        metric=series.metric,
        labels=dict(series.labels),
        points=[[point[0].timestamp(), point[1]] for point in series.points],
    )
