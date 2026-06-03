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
from core.alerts import Comparison
from core.config import (
    PlaneRegistries,
    ResolvedConfig,
    load_config,
)
from core.errors import (
    CapabilityMismatchError,
    MissingEnvVarError,
    PanoptesError,
    UnknownAdapterError,
)
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
from core.planes.notifier import Notifier
from core.planes.source import Source

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

    # Default outage-fetch opt-out (most sources skip fetch when unreachable — F3a).
    fetch_when_unreachable = False

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


def _make_notifier_class(notifier_type: str) -> type[Notifier]:
    """Build a `Notifier` class reporting a fixed `type` (the v0.2 sns/slack fakes).

    Mirrors `_make_source_class`: the config test must resolve `sns`/`slack` notifiers
    WITHOUT importing the real Phase-2 adapter modules (which pull in boto3/httpx), so a
    typed fake registered under each type proves the loader wires them up.
    """

    class _ConfiguredNotifier(_FakeNotifier):
        type = notifier_type

        def __init__(self, config: ConfigBlock) -> None:
            super().__init__(config)
            self.type = notifier_type

    return _ConfiguredNotifier


class _FakeDashboardProvider:
    type = "grafana"

    def __init__(self, config: ConfigBlock) -> None:
        self.config = config

    def provision(self, packs: list[DashboardPack]) -> None:
        return None


def _registries_with_correct_capabilities() -> PlaneRegistries:
    """Registries whose fake source capabilities MATCH the fixture's `provides:`.

    Built from the `PlaneRegistries.empty()` isolation seam (four fresh, plane-keyed
    registries) — so this fixture is fully isolated from the `core.registry` module
    globals, with no hand-written `Registry("source")`/… boilerplate or
    discriminator-string-typo risk.
    """
    registries = PlaneRegistries.empty()
    registries.sources.register("cloudwatch")(
        _make_source_class({SignalKind.METRIC, SignalKind.LOG})
    )
    registries.sources.register("sentry")(
        _make_source_class({SignalKind.INCIDENT, SignalKind.METRIC})
    )
    registries.sources.register("http-health")(_make_source_class({SignalKind.METRIC}))
    registries.stores.register("victoriametrics")(_FakeStore)
    registries.notifiers.register("logging")(_FakeNotifier)
    registries.dashboard_providers.register("grafana")(_FakeDashboardProvider)
    return registries


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
    # `.sources` now holds `ResolvedSource` wrappers; the built adapter is `.source`.
    http_health = next(rs for rs in dev_sources if rs.source.type == "http-health")
    # `Source` is a Protocol without a `config` accessor; the resolved instance is a
    # `_FakeSource`, so cast to read back the interpolated config block.
    assert cast(_FakeSource, http_health.source).config["url"] == "https://dev.example/health"


def test_loader_injects_environment_name_into_source_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each source's config block gets the env name as `env` (sources stamp it on
    signals; the YAML need not repeat `env:` per source)."""
    _set_reference_env(monkeypatch)
    config_path = _write_fixture(tmp_path)
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    for resolved_source in resolved.environments["dev"].sources:
        assert cast(_FakeSource, resolved_source.source).config["env"] == "dev"


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
    # A provides/capabilities mismatch must raise within the PanoptesError hierarchy
    # (CapabilityMismatchError) — NOT stdlib ValueError, which would escape a caller's
    # `except PanoptesError` handler (F3).
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=registries)
    assert isinstance(excinfo.value, CapabilityMismatchError)
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


# --- F2e: required-key access raises PanoptesError, not raw KeyError -------------


def test_missing_store_block_raises_panoptes_error_naming_store(tmp_path: Path) -> None:
    """A config with `panoptes:` but no `store` raises a clear PanoptesError (F2e).

    The loader indexes `body["store"]` directly; a missing key would raise a raw
    `KeyError` that escapes a caller's `except PanoptesError`. It must instead raise a
    PanoptesError naming the missing `store` block.
    """
    body = "panoptes:\n  environments: {}\n"
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "store" in str(excinfo.value)
    # Not a bare KeyError leaking through.
    assert not isinstance(excinfo.value, KeyError)


def test_env_missing_enabled_raises_panoptes_error_naming_field(tmp_path: Path) -> None:
    """An environment block missing `enabled` raises a clear PanoptesError (F2e)."""
    body = (
        "panoptes:\n"
        "  environments:\n"
        "    dev:\n"
        "      sources: []\n"
        "  store:\n"
        "    type: victoriametrics\n"
        "    url: http://vm:8428\n"
    )
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    message = str(excinfo.value)
    assert "enabled" in message
    assert "dev" in message


def test_env_missing_sources_raises_panoptes_error_naming_field(tmp_path: Path) -> None:
    """An enabled environment block missing `sources` raises a clear PanoptesError (F2e)."""
    body = (
        "panoptes:\n"
        "  environments:\n"
        "    dev:\n"
        "      enabled: true\n"
        "  store:\n"
        "    type: victoriametrics\n"
        "    url: http://vm:8428\n"
    )
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "sources" in str(excinfo.value)


def test_source_missing_type_raises_panoptes_error_naming_field(tmp_path: Path) -> None:
    """A source entry missing `type` raises a clear PanoptesError (F2e), not KeyError."""
    body = (
        "panoptes:\n"
        "  environments:\n"
        "    dev:\n"
        "      enabled: true\n"
        "      sources:\n"
        "        - url: http://app/health\n"
        "  store:\n"
        "    type: victoriametrics\n"
        "    url: http://vm:8428\n"
    )
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "type" in str(excinfo.value)
    assert not isinstance(excinfo.value, KeyError)


def test_store_missing_type_raises_panoptes_error_naming_field(tmp_path: Path) -> None:
    """A store block missing `type` raises a clear PanoptesError (F2e)."""
    body = "panoptes:\n  environments: {}\n  store:\n    url: http://vm:8428\n"
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "type" in str(excinfo.value)


def test_notifier_missing_type_raises_panoptes_error_naming_field(tmp_path: Path) -> None:
    """A notifier entry missing `type` raises a clear PanoptesError (F2e)."""
    body = (
        "panoptes:\n"
        "  environments: {}\n"
        "  store:\n"
        "    type: victoriametrics\n"
        "    url: http://vm:8428\n"
        "  notifiers:\n"
        "    - foo: bar\n"
    )
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "type" in str(excinfo.value)


# --- v0.2: sns / slack notifier resolution ---------------------------------------


def _registries_with_v0_2_notifiers() -> PlaneRegistries:
    """The reference registries PLUS fake `sns` + `slack` notifiers (v0.2 adapters)."""
    registries = _registries_with_correct_capabilities()
    registries.notifiers.register("sns")(_make_notifier_class("sns"))
    registries.notifiers.register("slack")(_make_notifier_class("slack"))
    return registries


def test_config_listing_sns_and_slack_resolves_to_registered_notifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config listing `sns`/`slack` resolves to the registered notifier adapters (v0.2)."""
    _set_reference_env(monkeypatch)
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:panoptes-alerts")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/services/T/B/X")
    body = _REFERENCE_YAML.replace(
        "  notifiers:\n    - type: logging\n",
        "  notifiers:\n"
        "    - type: logging\n"
        "    - type: sns\n"
        "      topic_arn: ${ALERT_TOPIC_ARN}\n"
        "    - type: slack\n"
        "      webhook_url: ${SLACK_WEBHOOK_URL}\n",
    )
    config_path = _write_fixture(tmp_path, body)
    resolved = load_config(config_path, registries=_registries_with_v0_2_notifiers())
    notifier_types = {notifier.type for notifier in resolved.notifiers}
    assert {"logging", "sns", "slack"} <= notifier_types


def test_unregistered_notifier_type_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unregistered notifier `type` fails fast with a clear `PanoptesError` (v0.2).

    The reference registries do NOT register `sns`, so listing it must raise an
    `UnknownAdapterError` (a `PanoptesError` subclass) naming the bad type — the same
    fail-fast contract sources have, applied to the notifier plane.
    """
    _set_reference_env(monkeypatch)
    body = _REFERENCE_YAML.replace(
        "  notifiers:\n    - type: logging\n",
        "  notifiers:\n    - type: logging\n    - type: not-a-real-notifier\n",
    )
    config_path = _write_fixture(tmp_path, body)
    with pytest.raises(PanoptesError) as excinfo:
        # Use the BASE reference registries (sns/slack NOT registered) so the unknown
        # type genuinely fails — `not-a-real-notifier` is registered nowhere.
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert isinstance(excinfo.value, UnknownAdapterError)
    assert "not-a-real-notifier" in str(excinfo.value)


# --- v0.2: declarative alert rules (`alerts:` block) -----------------------------

_ALERTS_BLOCK = """
  alerts:
    - name: crashloop-high
      expr: panoptes_k8s_pods_crashloop
      comparison: gt
      threshold: 0
      for_cycles: 3
      severity: critical
      envs: [dev, stage]
      labels:
        team: platform
    - name: nodes-low
      expr: panoptes_k8s_node_count
      comparison: lt
      threshold: 1
"""


def test_alerts_parse_to_typed_alert_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An `alerts:` block parses to typed `AlertRule`s — enum resolved, defaults applied."""
    _set_reference_env(monkeypatch)
    body = _REFERENCE_YAML + _ALERTS_BLOCK
    config_path = _write_fixture(tmp_path, body)
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    assert len(resolved.alerts) == 2
    by_name = {rule.name: rule for rule in resolved.alerts}

    crashloop = by_name["crashloop-high"]
    # The comparison string resolved to the Comparison enum.
    assert crashloop.comparison is Comparison.GT
    assert crashloop.threshold == 0.0
    assert crashloop.for_cycles == 3
    assert crashloop.severity == "critical"
    assert crashloop.envs == ["dev", "stage"]
    assert crashloop.labels == {"team": "platform"}

    # The second rule exercises the DEFAULTS (for_cycles=1, severity=warning, envs=["all"]).
    nodes_low = by_name["nodes-low"]
    assert nodes_low.comparison is Comparison.LT
    assert nodes_low.for_cycles == 1
    assert nodes_low.severity == "warning"
    assert nodes_low.envs == ["all"]
    assert nodes_low.labels == {}


def test_config_without_alerts_block_resolves_to_empty_alert_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config with no `alerts:` block resolves to an empty alert list (not an error)."""
    _set_reference_env(monkeypatch)
    config_path = _write_fixture(tmp_path)  # the reference YAML has no alerts:
    resolved = load_config(config_path, registries=_registries_with_correct_capabilities())
    assert resolved.alerts == []


def test_unknown_comparison_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown `comparison` fails fast with a clear PanoptesError (not stdlib ValueError)."""
    _set_reference_env(monkeypatch)
    bad_alert = (
        "\n  alerts:\n"
        "    - name: bogus\n"
        "      expr: panoptes_k8s_node_count\n"
        "      comparison: approximately-equals\n"
        "      threshold: 1\n"
    )
    config_path = _write_fixture(tmp_path, _REFERENCE_YAML + bad_alert)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    # Hierarchy-correct (a PanoptesError, never a stdlib ValueError escaping the handler).
    assert not isinstance(excinfo.value, ValueError) or isinstance(excinfo.value, PanoptesError)
    message = str(excinfo.value)
    assert "bogus" in message
    assert "approximately-equals" in message


def test_alert_missing_required_field_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alert rule missing a required field (here `threshold`) fails fast."""
    _set_reference_env(monkeypatch)
    missing_threshold = (
        "\n  alerts:\n"
        "    - name: no-threshold\n"
        "      expr: panoptes_k8s_node_count\n"
        "      comparison: gt\n"
    )
    config_path = _write_fixture(tmp_path, _REFERENCE_YAML + missing_threshold)
    with pytest.raises(PanoptesError) as excinfo:
        load_config(config_path, registries=_registries_with_correct_capabilities())
    assert "threshold" in str(excinfo.value)


# --- PlaneRegistries test-isolation seam -----------------------------------------
#
# `PlaneRegistries.empty()` is the documented canonical seam for obtaining a fully
# ISOLATED registry set OUTSIDE the self-registration path (the `@SOURCES.register`
# decorators + the demo pack keep using the module globals — those are load-bearing
# for the self-registration design and untouched here). The factory removes the
# four-line `Registry("source")`/`("store")`/`("notifier")`/`("dashboard")` boilerplate
# (and the discriminator-string-typo risk) a test otherwise hand-writes, and proves
# isolation: registering into an `empty()` set must not leak into the globals, and the
# globals' adapters must not appear in the empty set.


def test_plane_registries_empty_gives_four_fresh_isolated_registries() -> None:
    """`empty()` returns four fresh, empty, correctly-keyed `Registry` instances."""
    registries = PlaneRegistries.empty()
    # Correctly keyed by plane (no discriminator-string typo possible at call sites).
    assert registries.sources.kind == "source"
    assert registries.stores.kind == "store"
    assert registries.notifiers.kind == "notifier"
    assert registries.dashboard_providers.kind == "dashboard"
    # Genuinely empty — no adapters carried over from anywhere.
    assert registries.sources.available() == []
    assert registries.stores.available() == []
    assert registries.notifiers.available() == []
    assert registries.dashboard_providers.available() == []


def test_plane_registries_empty_is_isolated_from_the_module_globals() -> None:
    """Registering into an `empty()` set does not touch the `core.registry` globals."""
    from core import SOURCES

    registries = PlaneRegistries.empty()
    fake_source_class = _make_source_class({SignalKind.METRIC})
    registries.sources.register("isolated-fake")(fake_source_class)

    # The fake registered into the isolated set is visible there...
    assert "isolated-fake" in registries.sources.available()
    # ...but did NOT leak into the global SOURCES registry (full test isolation).
    assert "isolated-fake" not in SOURCES.available()
    # ...and a fresh empty set is independent of the one we just mutated.
    assert PlaneRegistries.empty().sources.available() == []


def test_plane_registries_from_globals_mirrors_the_module_singletons() -> None:
    """`from_globals()` returns the four `core.registry` module singletons (production seam)."""
    from core import DASHBOARD_PROVIDERS, NOTIFIERS, SOURCES, STORES

    registries = PlaneRegistries.from_globals()
    # IS the production wiring — the same singleton objects the decorators register into.
    assert registries.sources is SOURCES
    assert registries.stores is STORES
    assert registries.notifiers is NOTIFIERS
    assert registries.dashboard_providers is DASHBOARD_PROVIDERS
