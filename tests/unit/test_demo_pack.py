"""Phase 7 unit tests — the brand-neutral demo consumer pack (injection-path proof).

These tests prove the consumer-pack INJECTION mechanism end to end without `core`
ever importing the pack (spec `## Tests` → Demo pack):

- the pack loads via the `PANOPTES_CONSUMER_PACK` hook the Phase-6 server drives,
  registering a synthetic adapter + a `get_demo_signal` tool — and `core` never
  imports it (the import is dynamic, env-var-driven);
- `get_demo_signal(env, window)` returns the precise `DemoSignal` TypedDict, built
  over `panoptes_*` metric series read from a store;
- the demo dashboard JSON parses and declares the `env` Grafana template variable;
- the real `examples/demo-pack/panoptes.yaml` RESOLVES under the demo env vars set,
  building live adapters from the REAL core registries (it only BUILDS adapters — no
  upstream fetch happens at resolve time, so the test stays offline + deterministic).

Brand-neutrality: the pack is a generic synthetic example a real consumer replaces
with its own; nothing here names any consumer brand or domain.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

# Importing the core adapters registers them on the module-singleton registries, so
# `load_config` against the real registries can build cloudwatch/sentry/http-health/
# victoriametrics/logging. The import is for its registration side effect.
import core.notifiers.logging_notifier
import core.sources.cloudwatch
import core.sources.http_health
import core.sources.sentry
import core.stores.victoriametrics  # noqa: F401
import pytest
from core.config import ResolvedConfig, load_config
from core.mcp.server import build_server
from core.model import MetricQuery, MetricSeries

# The in-repo demo pack (the fixture a real consumer replaces with its own repo dir).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_PACK_DIR = _REPO_ROOT / "examples" / "demo-pack"
_DEMO_DASHBOARD = _DEMO_PACK_DIR / "dashboards" / "demo" / "dashboard.json"
_DEMO_PANOPTES_YAML = _DEMO_PACK_DIR / "panoptes.yaml"

# The dotted module path the `PANOPTES_CONSUMER_PACK` hook imports. The pack lives
# under `examples/` and is imported ONLY dynamically (never by core).
_PACK_MODULE = "examples.demo-pack.pack"

# A fake metric series the synthetic store hands `get_demo_signal`, so the tool's
# `DemoSignal` shape is asserted without a live store.
_FAKE_POINTS = [(datetime(2026, 1, 1, tzinfo=UTC), 1.0), (datetime(2026, 1, 1, 1, tzinfo=UTC), 0.0)]


class _FakeStore:
    """A typed fake store returning fixed `panoptes_*` series for `get_demo_signal`."""

    type = "fake"

    def __init__(self) -> None:
        self.queries: list[MetricQuery] = []

    def write(self, signals: list[object]) -> None:  # pragma: no cover - unused here
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        self.queries.append(query)
        return [
            MetricSeries(
                metric="panoptes_health_up",
                labels={"env": "dev", "url": "https://dev/health"},
                points=list(_FAKE_POINTS),
            )
        ]


@pytest.fixture
def _consumer_pack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the injection hook at the in-repo demo pack for the duration of a test."""
    monkeypatch.setenv("PANOPTES_CONSUMER_PACK", _PACK_MODULE)
    yield


def _import_pack() -> object:
    """Import the demo pack module by its hyphenated path (mirrors the hook's import).

    The root conftest rolls back the pack's `@STORES.register("demo-synthetic")` after
    every test (F8 isolation). Python's import cache, however, means a second
    `import_module` would NOT re-run the module body (and so would NOT re-register). So if
    the module is already cached, RELOAD it to re-execute the registration decorator —
    making this helper register the synthetic adapter every call, regardless of order.
    """
    import importlib
    import sys

    if _PACK_MODULE in sys.modules:
        return importlib.reload(sys.modules[_PACK_MODULE])
    return importlib.import_module(_PACK_MODULE)


def _resolved_config_with_store(store: _FakeStore) -> ResolvedConfig:
    """A minimal `ResolvedConfig` whose `store` is the fake (the tool reads it)."""
    return ResolvedConfig(
        environments={},
        store=store,  # type: ignore[arg-type]
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )


# --- the injection hook loads the pack without core importing it -----------------


def test_pack_registers_get_demo_signal_via_injection_hook(
    _consumer_pack_env: None,
) -> None:
    """`build_server` loads the pack via `PANOPTES_CONSUMER_PACK` and gains the tool."""
    store = _FakeStore()
    server = build_server(_resolved_config_with_store(store))
    assert "get_demo_signal" in server.tool_names(), (
        "the injected demo pack must register get_demo_signal via register_tools"
    )


def test_pack_loads_via_file_path_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production deploy model: `PANOPTES_CONSUMER_PACK` is a FILE PATH (the compose
    mount `/packs/consumer/pack.py`), loaded via spec_from_file_location — not a dotted
    module. Proves the path Phase 8's docker-compose actually uses."""
    monkeypatch.setenv("PANOPTES_CONSUMER_PACK", str(_DEMO_PACK_DIR / "pack.py"))
    server = build_server(_resolved_config_with_store(_FakeStore()))
    assert "get_demo_signal" in server.tool_names(), (
        "the pack must load from a mounted file path (the compose deploy model)"
    )


def test_get_demo_signal_returns_typed_demo_signal_over_panoptes_metrics() -> None:
    """`get_demo_signal(env, window)` returns the `DemoSignal` shape over the store series."""
    pack = _import_pack()
    store = _FakeStore()
    signal = pack.get_demo_signal(store, env="dev", window="15m")  # type: ignore[attr-defined]

    assert signal["env"] == "dev"
    assert signal["window"] == "15m"
    assert signal["sample_count"] == len(_FAKE_POINTS)
    assert signal["metrics"], "DemoSignal must surface panoptes_* derived metric points"
    first = signal["metrics"][0]
    assert first["metric"].startswith("panoptes_"), "demo signal reads panoptes_* metrics only"
    assert isinstance(first["value"], float)
    # The tool actually queried the store for a panoptes_* metric (proves read path).
    assert store.queries, "get_demo_signal must read the store"
    assert "panoptes_" in store.queries[0].expr


def test_pack_registers_synthetic_adapter_on_a_core_registry() -> None:
    """Importing the pack adds a synthetic adapter to a core registry (additive injection)."""
    from core.registry import STORES

    _import_pack()
    assert "demo-synthetic" in STORES.available(), (
        "the demo pack must register its synthetic adapter on the core STORES registry"
    )


# --- the demo dashboard JSON is valid + uses the env template variable -----------


def test_demo_dashboard_json_is_valid_and_uses_env_template() -> None:
    parsed = json.loads(_DEMO_DASHBOARD.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "demo dashboard.json must be a JSON object"
    assert parsed.get("title"), "demo dashboard must declare a non-empty title"

    templating = parsed["templating"]
    assert isinstance(templating, dict)
    names = {variable["name"] for variable in templating["list"]}
    assert "env" in names, "demo dashboard must declare an 'env' template variable"

    # Every panel target references a panoptes_* metric (generic, no domain content).
    for panel in parsed["panels"]:
        for target in panel.get("targets", []):
            assert "panoptes_" in target["expr"], (
                "demo panels must read panoptes_* metrics only (brand-neutral)"
            )


# --- the real panoptes.yaml resolves under the demo env vars ---------------------


def test_panoptes_yaml_resolves_under_demo_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped reference config resolves cleanly against the REAL core registries.

    Only BUILDS adapters (no upstream fetch), so it is offline + deterministic. Every
    `${VAR}` the config interpolates is supplied here, mirroring the `.env.example`.
    """
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SENTRY_ORG", "demo-org")
    monkeypatch.setenv("SENTRY_PROJECT", "demo-project")
    monkeypatch.setenv("SENTRY_TOKEN", "read-only-token")  # nosec B105 - synthetic test value
    monkeypatch.setenv("DEV_HEALTH_URL", "https://demo.invalid/health")
    monkeypatch.setenv("CONSUMER_PACK_DIR", str(_DEMO_PACK_DIR))

    resolved = load_config(_DEMO_PANOPTES_YAML)

    assert isinstance(resolved, ResolvedConfig)
    assert resolved.environments["dev"].enabled is True
    assert {rs.source.type for rs in resolved.environments["dev"].sources} == {
        "cloudwatch",
        "sentry",
        "http-health",
    }
    # stage/prod are wired-but-inert (enabled: false → no live adapters).
    assert resolved.environments["stage"].enabled is False
    assert resolved.environments["stage"].sources == []
    assert resolved.environments["prod"].enabled is False
    assert resolved.store.type == "victoriametrics"
    pack_ids = {pack.id for pack in resolved.dashboard_packs}
    assert {"errors-sentry", "logs", "overview"} <= pack_ids
    assert "consumer" in pack_ids
    assert resolved.mcp.get("transport") == "stdio"
