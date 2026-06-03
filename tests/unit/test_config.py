"""Phase 1 unit tests for the config loader (`core/config.py`).

Covers (spec `## Configuration` / playbook Phase 1 table):
- `${VAR}` interpolation from the process environment;
- a **missing var fails fast** naming the missing variable;
- an **unknown adapter `type` fails fast** (delegated to the registry);
- an `enabled: false` environment parses but produces **no live adapters**;
- the full `ResolvedConfig` shape for the reference example YAML;
- **`provides:` ↔ `capabilities()` reconciliation** — a source whose declared
  `provides:` disagrees with the built adapter's `capabilities()` fails fast at
  resolve time, naming the source + the mismatch.

The fixture YAML is written to `tmp_path` (NOT imported from Phase 7) and mirrors
the spec's `examples/demo-pack/panoptes.yaml` shape. Real adapters do not exist
until Phases 2-6, so this test injects a registry pre-populated with typed fake
adapters for the types the fixture references. The loader accepts injected
registries so resolution succeeds without importing Phase 2-6 modules.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from core.config import (
    PlaneRegistries,
    ResolvedConfig,
    load_config,
)
from core.errors import MissingEnvVarError, PanoptesError, UnknownAdapterError
from core.model import (
    Alert,
    CanonicalSignal,
    DashboardPack,
    MetricQuery,
    MetricSeries,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.planes.dashboard import DashboardProvider
from core.planes.notifier import Notifier
from core.planes.source import Source
from core.planes.store import Store
from core.registry import Registry

ConfigBlock = Mapping[str, str | int | bool | list[str]]

_REFERENCE_YAML = """
panoptes:
  environments:
    dev:
      enabled: true
      sources:
        - type: cloudwatch
          provides: [metric, log]
          region: ${AWS_REGION}
        - type: sentry
          provides: [incident, metric]
          org: ${SENTRY_ORG}
        - type: http-health
          provides: [metric]
          url: ${DEV_HEALTH_URL}
    stage:
      enabled: false
      sources:
        - type: cloudwatch
          provides: [metric, log]
          region: ${AWS_REGION}
    prod:
      enabled: false
      sources: []
  store:
    type: victoriametrics
    url: http://vm:8428
  notifiers:
    - type: logging
  dashboards:
    provider: grafana
    env_variable: true
    core_packs: [errors-sentry, logs, overview]
    consumer_pack:
      path: ${CONSUMER_PACK_DIR}
  slos:
    - name: health-uptime
      objective: 0.99
  mcp:
    transport: stdio
    tools: [describe_signal_catalog, query_metric]
"""


def _now() -> datetime:
    return datetime.now(UTC)


class _FakeSource:
    """Typed fake `Source`. Its `capabilities()` are set by the registered type."""

    def __init__(self, config: ConfigBlock, capabilities: set[SignalKind]) -> None:
        self.config = config
        self._capabilities = capabilities
        self.type = str(config.get("type", "fake"))

    def capabilities(self) -> set[SignalKind]:
        return self._capabilities

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return []

    def health(self) -> SourceHealth:
        return SourceHealth(reachable=True, detail="ok", checked_at=_now())


def _make_source_class(capabilities: set[SignalKind]) -> type[Source]:
    """Build a `Source` class whose instances report a fixed capability set."""

    class _ConfiguredSource(_FakeSource):
        def __init__(self, config: ConfigBlock) -> None:
            super().__init__(config, capabilities)

    return _ConfiguredSource


class _FakeStore:
    type = "victoriametrics"

    def __init__(self, config: ConfigBlock) -> None:
        self.config = config

    def write(self, signals: list[CanonicalSignal]) -> None:
        return None

    def query(self, query: MetricQuery) -> list[MetricSeries]:
        return []


class _FakeNotifier:
    type = "logging"

    def __init__(self, config: ConfigBlock) -> None:
        self.config = config

    def notify(self, alert: Alert) -> None:
        return None


class _FakeDashboardProvider:
    type = "grafana"

    def __init__(self, config: ConfigBlock) -> None:
        self.config = config

    def provision(self, packs: list[DashboardPack]) -> None:
        return None


def _registries_with_correct_capabilities() -> PlaneRegistries:
    """Registries whose fake source capabilities MATCH the fixture's `provides:`."""
    sources: Registry[Source] = Registry("source")
    sources.register("cloudwatch")(_make_source_class({SignalKind.METRIC, SignalKind.LOG}))
    sources.register("sentry")(_make_source_class({SignalKind.INCIDENT, SignalKind.METRIC}))
    sources.register("http-health")(_make_source_class({SignalKind.METRIC}))

    stores: Registry[Store] = Registry("store")
    stores.register("victoriametrics")(_FakeStore)

    notifiers: Registry[Notifier] = Registry("notifier")
    notifiers.register("logging")(_FakeNotifier)

    providers: Registry[DashboardProvider] = Registry("dashboard")
    providers.register("grafana")(_FakeDashboardProvider)

    return PlaneRegistries(
        sources=sources,
        stores=stores,
        notifiers=notifiers,
        dashboard_providers=providers,
    )


def _write_fixture(tmp_path: Path, body: str = _REFERENCE_YAML) -> Path:
    config_path = tmp_path / "panoptes.yaml"
    config_path.write_text(body)
    return config_path


def _set_reference_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("SENTRY_ORG", "acme")
    monkeypatch.setenv("DEV_HEALTH_URL", "https://dev.example/health")
    monkeypatch.setenv("CONSUMER_PACK_DIR", "/packs/consumer")


def test_interpolation_substitutes_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_reference_env(monkeypatch)
    config_path = _write_fixture(tmp_path)
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    dev_sources = resolved.environments["dev"].sources
    http_health = next(s for s in dev_sources if s.type == "http-health")
    # `Source` is a Protocol without a `config` accessor; the resolved instance is a
    # `_FakeSource`, so cast to read back the interpolated config block.
    assert cast(_FakeSource, http_health).config["url"] == "https://dev.example/health"


def test_loader_injects_environment_name_into_source_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each source's config block gets the env name as `env` (sources stamp it on
    signals; the YAML need not repeat `env:` per source)."""
    _set_reference_env(monkeypatch)
    config_path = _write_fixture(tmp_path)
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    for source in resolved.environments["dev"].sources:
        assert cast(_FakeSource, source).config["env"] == "dev"


def test_missing_env_var_fails_fast_naming_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_reference_env(monkeypatch)
    monkeypatch.delenv("DEV_HEALTH_URL", raising=False)
    config_path = _write_fixture(tmp_path)
    with pytest.raises(MissingEnvVarError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "DEV_HEALTH_URL" in str(excinfo.value)


def test_unknown_adapter_type_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_reference_env(monkeypatch)
    body = _REFERENCE_YAML.replace("type: http-health", "type: does-not-exist")
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(UnknownAdapterError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "does-not-exist" in str(excinfo.value)


def test_disabled_env_produces_no_adapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_reference_env(monkeypatch)
    config_path = _write_fixture(tmp_path)
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    assert resolved.environments["stage"].enabled is False
    assert resolved.environments["stage"].sources == []
    assert resolved.environments["prod"].sources == []


def test_full_resolved_config_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_reference_env(monkeypatch)
    config_path = _write_fixture(tmp_path)
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    assert isinstance(resolved, ResolvedConfig)
    # One store, three live dev sources, one notifier.
    assert resolved.store.type == "victoriametrics"
    assert len(resolved.environments["dev"].sources) == 3
    assert len(resolved.notifiers) == 1
    # Dashboard packs: three core + one consumer.
    pack_ids = {pack.id for pack in resolved.dashboard_packs}
    assert {"errors-sentry", "logs", "overview"} <= pack_ids
    consumer_packs = [p for p in resolved.dashboard_packs if p.tier == "consumer"]
    assert len(consumer_packs) == 1
    assert str(consumer_packs[0].json_path).startswith("/packs/consumer")
    # SLOs + MCP settings carried through.
    assert resolved.slos[0]["name"] == "health-uptime"
    assert resolved.mcp["transport"] == "stdio"


def test_provides_capabilities_mismatch_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`capabilities()` is authoritative; a disagreeing `provides:` fails fast."""
    _set_reference_env(monkeypatch)
    config_path = _write_fixture(tmp_path)
    registries = _registries_with_correct_capabilities()
    # Override http-health to report a capability set that disagrees with the
    # fixture's `provides: [metric]` (it now claims LOG, not METRIC).
    registries.sources.register("http-health")(_make_source_class({SignalKind.LOG}))
    with pytest.raises(ValueError) as excinfo:
        load_config(config_path, registries=registries)
    message = str(excinfo.value)
    assert "http-health" in message
    assert "metric" in message.lower() or "capab" in message.lower()


# --- Negative paths (spec ## Tests "Config") -------------------------------------


def test_malformed_yaml_raises_panoptes_error(tmp_path: Path) -> None:
    """A YAML syntax error surfaces as a clear PanoptesError, not a raw YAMLError."""
    config_path = _write_fixture(tmp_path, "panoptes:\n  environments: [unclosed\n")
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert str(config_path) in str(excinfo.value)


def test_top_level_without_panoptes_key_rejected(tmp_path: Path) -> None:
    """A file lacking the top-level `panoptes:` mapping fails fast."""
    config_path = _write_fixture(tmp_path, "something_else:\n  foo: bar\n")
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "panoptes" in str(excinfo.value)


def test_consumer_pack_neither_path_nor_git_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer_pack with neither `path` nor `git` is an invalid union."""
    _set_reference_env(monkeypatch)
    body = _REFERENCE_YAML.replace(
        "    consumer_pack:\n      path: ${CONSUMER_PACK_DIR}\n",
        "    consumer_pack: {}\n",
    )
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "consumer_pack" in str(excinfo.value)


def test_consumer_pack_both_path_and_git_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer_pack with BOTH `path` and `git` is an invalid union."""
    _set_reference_env(monkeypatch)
    body = _REFERENCE_YAML.replace(
        "    consumer_pack:\n      path: ${CONSUMER_PACK_DIR}\n",
        "    consumer_pack:\n      path: ${CONSUMER_PACK_DIR}\n      git: https://example/repo\n",
    )
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "consumer_pack" in str(excinfo.value)


def test_consumer_pack_git_only_parses_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git-only is a VALID shape (parsed-but-deferred): it resolves with no error and
    emits a consumer-tier pack marked `selector="git"`. The deferral is real — the
    Grafana provider raises a CapabilityError when asked to PROVISION that git pack
    in v0.1 (parse OK, acting on it fails). test_dashboards_valid covers the latter."""
    _set_reference_env(monkeypatch)
    git_block = (
        "    consumer_pack:\n"
        "      git: https://example/repo\n"
        "      ref: main\n"
        "      subdir: dashboards\n"
    )
    body = _REFERENCE_YAML.replace(
        "    consumer_pack:\n      path: ${CONSUMER_PACK_DIR}\n", git_block
    )
    config_path = _write_fixture(tmp_path, body)
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    assert isinstance(resolved, ResolvedConfig)
    consumer_packs = [p for p in resolved.dashboard_packs if p.tier == "consumer"]
    assert len(consumer_packs) == 1
    assert consumer_packs[0].selector == "git"
