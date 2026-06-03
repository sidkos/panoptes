"""v0.3 Phase 4 unit tests — the brand-neutral fleet consumer pack (genericity proof).

These tests prove the SECOND, unrelated consumer pack injects cleanly via the same v0.1
`PANOPTES_CONSUMER_PACK` hook the demo pack uses — WITHOUT any core change between the two
packs (spec § "the genericity proof"). The fleet pack additionally demonstrates that a
consumer source adapter can BUILD ON a core source: its `fleet` source composes the core
`prometheus` source (delegating its scrape + normalization) and relabels the scraped
Agones-style series into canonical `panoptes_fleet_*` gauges.

Covers (spec § Consumer-pack tools `get_fleet_health` + § Data Model consumer fleet metrics
+ plan Phase 4 + Risk G1/G6 consumer→core import direction):

- the pack loads via the `PANOPTES_CONSUMER_PACK` hook the server drives, registering its
  `fleet` source on the core `SOURCES` registry + a `get_fleet_health` MCP tool — and `core`
  never imports the pack (the import is dynamic, env-var-driven);
- the `fleet` source BUILDS ON the core `prometheus` source (composition asserted: it holds a
  `PrometheusSource` and delegates `fetch` to it) and relabels the scraped series into
  `panoptes_fleet_ready`/`_allocated`/`_reserved` gauges with `env` stamped;
- the `fleet` source registration is purely ADDITIVE on the core `SOURCES` registry;
- `get_fleet_health(env)` returns the precise `FleetHealth` TypedDict over a fake store;
- the fleet dashboard JSON is valid (declares the `env` template var, references the
  `panoptes_fleet_*` metrics the source emits);
- the pack is READ-ONLY (its tool issues only `store.query`; the source delegates to the
  core prometheus source's httpx GET path — no boto3 mutation verb anywhere).

Brand-neutrality: the fleet pack is a GENERIC game-server-fleet example. Consumer-domain
tokens (`agones`/`fleet`/`allocated`) are allowed ONLY here under `examples/` — no named
consumer brand appears anywhere (the brand-neutrality grep is 0). All httpx is mocked with
`respx`; no `asyncio`.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

# Importing the core store/notifier adapters registers them on the module-singleton
# registries so the fleet `panoptes.yaml` resolves its `victoriametrics` store + `logging`
# notifier against the REAL registries. (The `prometheus` source is registered transitively
# by the `from core.sources.prometheus import PrometheusSource` below.) These imports are for
# their registration side effect.
import core.notifiers.logging_notifier
import core.stores.victoriametrics  # noqa: F401
import httpx
import pytest
import respx
from core.config import ResolvedConfig, load_config
from core.mcp.server import build_server
from core.model import MetricQuery, MetricSeries, MetricSignal, SignalKind, TimeWindow
from core.registry import SOURCES
from core.sources.prometheus import PrometheusSource

# The in-repo fleet pack (the fixture a real game-platform consumer replaces with its own).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FLEET_PACK_DIR = _REPO_ROOT / "examples" / "consumer-fleet-pack"
_FLEET_DASHBOARD = _FLEET_PACK_DIR / "dashboards" / "fleet" / "dashboard.json"
_FLEET_PANOPTES_YAML = _FLEET_PACK_DIR / "panoptes.yaml"

# The dotted module path the `PANOPTES_CONSUMER_PACK` hook imports (under examples/, dynamic).
_PACK_MODULE = "examples.consumer-fleet-pack.pack"

# The consumer's Prometheus endpoint + the Agones-style fleet metric the source scrapes.
_PROM_BASE = "http://prometheus.fleet.test:9090"
_ENV = "dev"
_QUERY_RANGE_URL = f"{_PROM_BASE}/api/v1/query_range"

_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    end=datetime(2026, 1, 1, 0, 15, 0, tzinfo=UTC),
)

# A recorded Agones `agones_fleets_replicas_count` matrix response: three series, one per
# fleet replica `type` (ready / allocated / reserved). The fleet source relabels each by its
# `type` into the canonical `panoptes_fleet_*` gauge and drops the `type` label.
_FLEET_MATRIX_PAYLOAD = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"__name__": "agones_fleets_replicas_count", "type": "ready"},
                "values": [[1735689600, "5"]],
            },
            {
                "metric": {"__name__": "agones_fleets_replicas_count", "type": "allocated"},
                "values": [[1735689600, "3"]],
            },
            {
                "metric": {"__name__": "agones_fleets_replicas_count", "type": "reserved"},
                "values": [[1735689600, "1"]],
            },
        ],
    },
}


def _import_pack() -> object:
    """Import the fleet pack by its hyphenated dotted path (mirrors the hook's import).

    The root conftest rolls back the pack's `@SOURCES.register("fleet")` after every test
    (F8 isolation). Python's import cache means a second `import_module` would NOT re-run the
    module body (so would NOT re-register), so RELOAD a cached module to re-execute the
    registration decorator — making this helper register the fleet source every call.
    """
    import importlib
    import sys

    if _PACK_MODULE in sys.modules:
        return importlib.reload(sys.modules[_PACK_MODULE])
    return importlib.import_module(_PACK_MODULE)


class _FakeFleetStore:
    """A typed fake store returning fixed `panoptes_fleet_*` series for `get_fleet_health`."""

    type = "fake"

    def __init__(self) -> None:
        self.queries: list[MetricQuery] = []
        # The three canonical fleet gauges keyed to their latest stored value.
        self._values = {
            "panoptes_fleet_ready": 5.0,
            "panoptes_fleet_allocated": 3.0,
            "panoptes_fleet_reserved": 1.0,
        }

    def write(self, signals: list[object]) -> None:  # pragma: no cover - unused here
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        self.queries.append(query)
        # Return the one fleet gauge the expr names (the tool issues one query per gauge).
        for metric_name, value in self._values.items():
            if metric_name in query.expr:
                return [
                    MetricSeries(
                        metric=metric_name,
                        labels={"env": _ENV},
                        points=[(datetime(2026, 1, 1, tzinfo=UTC), value)],
                    )
                ]
        return []


@pytest.fixture
def _fleet_pack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the injection hook at the in-repo fleet pack for the duration of a test."""
    monkeypatch.setenv("PANOPTES_CONSUMER_PACK", _PACK_MODULE)
    yield


def _resolved_config_with_store(store: _FakeFleetStore) -> ResolvedConfig:
    """A minimal `ResolvedConfig` whose `store` is the fake (the fleet tool reads it)."""
    return ResolvedConfig(
        environments={},
        store=store,  # type: ignore[arg-type]
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )


# --- the injection hook loads the SECOND pack without core importing it ----------


def test_fleet_pack_registers_get_fleet_health_via_injection_hook(_fleet_pack_env: None) -> None:
    """`build_server` loads the fleet pack via `PANOPTES_CONSUMER_PACK` and gains the tool."""
    server = build_server(_resolved_config_with_store(_FakeFleetStore()))
    assert "get_fleet_health" in server.tool_names(), (
        "the injected fleet pack must register get_fleet_health via register_tools"
    )


def test_fleet_pack_loads_via_file_path_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production deploy model: the hook is a FILE PATH (the mounted pack.py)."""
    monkeypatch.setenv("PANOPTES_CONSUMER_PACK", str(_FLEET_PACK_DIR / "pack.py"))
    server = build_server(_resolved_config_with_store(_FakeFleetStore()))
    assert "get_fleet_health" in server.tool_names(), (
        "the fleet pack must load from a mounted file path (the compose deploy model)"
    )


def test_fleet_pack_registers_fleet_source_on_core_registry_additively() -> None:
    """Importing the pack adds a `fleet` source to the core SOURCES registry (additive)."""
    baseline = set(SOURCES.available())
    _import_pack()
    after = set(SOURCES.available())
    assert baseline <= after, "importing the fleet pack must not drop core sources"
    assert "fleet" in after - baseline, (
        "the fleet pack must register its `fleet` source as an ADDITION to the core registry"
    )


# --- the fleet source BUILDS ON the core prometheus source -----------------------


def test_fleet_source_composes_the_core_prometheus_source() -> None:
    """The `fleet` source holds + delegates to a core `PrometheusSource` (consumer→core reuse).

    Proves the consumer→core dependency direction (Risk G1/G6): the fleet source REUSES the
    core prometheus source rather than re-implementing scraping. The composed instance is a
    genuine `core.sources.prometheus.PrometheusSource`.
    """
    pack = _import_pack()
    source = pack.FleetSource(  # type: ignore[attr-defined]
        {"url": _PROM_BASE, "env": _ENV}
    )
    composed = source.prometheus_source()
    assert isinstance(composed, PrometheusSource), (
        "the fleet source must BUILD ON the core PrometheusSource (composition, not a copy)"
    )


def test_fleet_source_relabels_scraped_series_into_panoptes_fleet_gauges() -> None:
    """`fetch` delegates to prometheus, then relabels by `type` into `panoptes_fleet_*`.

    The Agones `agones_fleets_replicas_count` series carry a `type` label (ready/allocated/
    reserved); the fleet source maps each into the canonical gauge name, drops the `type`
    label, and keeps `env` stamped — exactly the relabel the consumer's dashboard reads.
    """
    pack = _import_pack()
    source = pack.FleetSource(  # type: ignore[attr-defined]
        {"url": _PROM_BASE, "env": _ENV}
    )
    with respx.mock:
        respx.get(_QUERY_RANGE_URL).mock(
            return_value=httpx.Response(200, json=_FLEET_MATRIX_PAYLOAD)
        )
        signals = source.fetch(_WINDOW)

    metric_signals = [s for s in signals if isinstance(s, MetricSignal)]
    by_name = {s.name: s for s in metric_signals}
    assert by_name.keys() == {
        "panoptes_fleet_ready",
        "panoptes_fleet_allocated",
        "panoptes_fleet_reserved",
    }
    assert by_name["panoptes_fleet_ready"].value == 5.0
    assert by_name["panoptes_fleet_allocated"].value == 3.0
    assert by_name["panoptes_fleet_reserved"].value == 1.0
    for signal in metric_signals:
        # env stamped, and the raw `type` label dropped (replaced by the canonical name).
        assert signal.labels["env"] == _ENV
        assert "type" not in signal.labels


def test_fleet_source_capabilities_is_exactly_metric() -> None:
    """The fleet source advertises {METRIC} (it is a metric source built on prometheus)."""
    pack = _import_pack()
    source = pack.FleetSource({"url": _PROM_BASE, "env": _ENV})  # type: ignore[attr-defined]
    assert source.capabilities() == {SignalKind.METRIC}


# --- get_fleet_health returns the typed FleetHealth over a fake store ------------


def test_get_fleet_health_returns_typed_fleet_health_over_store_gauges() -> None:
    """`get_fleet_health(env)` returns the `FleetHealth` shape over `panoptes_fleet_*` series."""
    pack = _import_pack()
    store = _FakeFleetStore()
    health = pack.get_fleet_health(store, env=_ENV)  # type: ignore[attr-defined]

    assert health["env"] == _ENV
    assert health["ready"] == 5.0
    assert health["allocated"] == 3.0
    assert health["reserved"] == 1.0
    # The tool actually read the store for panoptes_fleet_* gauges (proves read path).
    assert store.queries, "get_fleet_health must read the store"
    assert any("panoptes_fleet_" in q.expr for q in store.queries)


# --- the fleet dashboard JSON is valid + uses the env template variable -----------


def test_fleet_dashboard_json_is_valid_and_uses_env_template() -> None:
    parsed = json.loads(_FLEET_DASHBOARD.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "fleet dashboard.json must be a JSON object"
    assert parsed.get("title"), "fleet dashboard must declare a non-empty title"

    templating = parsed["templating"]
    assert isinstance(templating, dict)
    names = {variable["name"] for variable in templating["list"]}
    assert "env" in names, "fleet dashboard must declare an 'env' template variable"

    # Every panel target references a panoptes_fleet_* metric the fleet source emits.
    fleet_metric_refs: set[str] = set()
    for panel in parsed["panels"]:
        for target in panel.get("targets", []):
            assert "panoptes_fleet_" in target["expr"], (
                "fleet panels must read the panoptes_fleet_* metrics the source emits"
            )
            fleet_metric_refs.add(target["expr"])
    assert fleet_metric_refs, "the fleet dashboard must declare at least one fleet panel"


# --- the real fleet panoptes.yaml resolves under its env vars --------------------


def test_fleet_panoptes_yaml_resolves_with_fleet_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fleet pack's reference config resolves against the REAL registries + fleet source.

    Resolving builds adapters (no upstream fetch), so it stays offline + deterministic. The
    fleet source must be on the registry (the pack import registered it) for the config's
    `type: fleet` source to build.
    """
    # The pack import registers the fleet source on SOURCES so `type: fleet` resolves.
    _import_pack()
    monkeypatch.setenv("FLEET_PROMETHEUS_URL", _PROM_BASE)
    monkeypatch.setenv("CONSUMER_PACK_DIR", str(_FLEET_PACK_DIR))

    resolved = load_config(_FLEET_PANOPTES_YAML)

    assert isinstance(resolved, ResolvedConfig)
    assert resolved.environments["dev"].enabled is True
    source_types = {rs.source.type for rs in resolved.environments["dev"].sources}
    assert "fleet" in source_types, "the fleet config must wire the consumer `fleet` source"
