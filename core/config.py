"""The config loader — parses the `panoptes:` YAML into a typed `ResolvedConfig`.

Responsibilities (spec `## Configuration`):
1. Load YAML and validate the top-level shape against `TypedDict` schemas.
2. `${VAR}` interpolation from `os.environ`, failing fast with `MissingEnvVarError`
   naming the missing variable. Secrets never live in config — only `${VAR}` refs.
3. Fail fast on an unknown adapter `type` — delegated to the registry's `build`,
   which raises `UnknownAdapterError` listing valid types.
4. `enabled: false` environments parse but produce NO live adapters (inert).
5. Produce a fully-typed `ResolvedConfig`: instantiated sources per env, one store,
   notifiers, dashboard packs, slos, mcp settings.
6. `provides:` ↔ `capabilities()` reconciliation — `capabilities()` is authoritative
   (it reflects what the built adapter can actually emit); a config block whose
   declared `provides:` disagrees fails fast at resolve time, naming the source +
   the mismatch. `provides:` is advisory documentation, not the source of truth.

The loader reads exactly one file path + `os.environ` — no network/disk I/O beyond
that (keeps Phase 1 pure core). Plane registries are injectable so tests can supply
fakes; production passes the four module registries (the default).
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import yaml

from core.errors import MissingEnvVarError, PanoptesError
from core.model import DashboardPack, SignalKind
from core.planes.dashboard import DashboardProvider
from core.planes.notifier import Notifier
from core.planes.source import Source
from core.planes.store import Store
from core.registry import (
    DASHBOARD_PROVIDERS,
    NOTIFIERS,
    SOURCES,
    STORES,
    ConfigValue,
    Registry,
)

# Core dashboard packs resolve under this in-repo dir; consumer packs resolve under
# the injected `dashboards.consumer_pack.path`. Kept relative — the Grafana provider
# (Phase 5) anchors the glob; Phase 1 only records the path on `DashboardPack`.
_CORE_DASHBOARDS_DIR = Path("core/dashboards")

# `${VAR}` interpolation pattern. Anchored to a bare ${NAME} form (alphanumerics +
# underscore), which is the only form the v0.1 config uses.
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# --- TypedDict schemas (the raw YAML shape, post-interpolation) -------------------
#
# These describe the on-disk config. They use `total=False` where the spec marks a
# block optional, and precise value types throughout (no `Any`). The loader narrows
# these into the runtime `ResolvedConfig` dataclasses below.


class SourceConfig(TypedDict, total=False):
    """One source entry under an environment's `sources:` list."""

    type: str
    provides: list[str]
    region: str
    org: str
    project: str
    url: str
    profile: str
    assume_role_arn: str
    external_id: str
    poll_interval_seconds: int
    fetch_timeout_seconds: int


class EnvironmentConfig(TypedDict):
    """One environment block under `environments:`."""

    enabled: bool
    sources: list[SourceConfig]


class StoreConfig(TypedDict, total=False):
    """The single store block."""

    type: str
    url: str
    retention: str


class NotifierConfig(TypedDict, total=False):
    """One notifier entry under `notifiers:`."""

    type: str


class ConsumerPackConfig(TypedDict, total=False):
    """The injected consumer-pack reference (a `path`, v0.1; or `git`, deferred)."""

    path: str
    git: str
    ref: str
    subdir: str


class DashboardsConfig(TypedDict, total=False):
    """The two-tier dashboards block."""

    provider: str
    env_variable: bool
    core_packs: list[str]
    consumer_pack: ConsumerPackConfig


class SloConfig(TypedDict, total=False):
    """One SLO entry (parsed-and-carried in v0.1; evaluated in v0.2)."""

    name: str
    objective: float


class McpConfig(TypedDict, total=False):
    """The MCP server block."""

    transport: str
    tools: list[str]


class _PanoptesBody(TypedDict, total=False):
    """The body under the top-level `panoptes:` key."""

    environments: dict[str, EnvironmentConfig]
    store: StoreConfig
    notifiers: list[NotifierConfig]
    dashboards: DashboardsConfig
    slos: list[SloConfig]
    mcp: McpConfig


class PanoptesConfig(TypedDict):
    """The whole config file: a single top-level `panoptes:` key."""

    panoptes: _PanoptesBody


# --- Runtime resolved shapes -----------------------------------------------------


@dataclass(frozen=True)
class ResolvedEnvironment:
    """An environment after resolution. Disabled envs carry an empty source list."""

    name: str
    enabled: bool
    sources: list[Source]


@dataclass(frozen=True)
class ResolvedConfig:
    """The fully-typed result of loading + resolving a config file."""

    environments: dict[str, ResolvedEnvironment]
    store: Store
    notifiers: list[Notifier]
    dashboard_packs: list[DashboardPack]
    slos: list[SloConfig]
    mcp: McpConfig


@dataclass(frozen=True)
class PlaneRegistries:
    """The four plane registries the loader builds adapters from (injectable for tests)."""

    sources: Registry[Source]
    stores: Registry[Store]
    notifiers: Registry[Notifier]
    dashboard_providers: Registry[DashboardProvider]


def _default_registries() -> PlaneRegistries:
    """The production registries (the four `core.registry` module singletons)."""
    return PlaneRegistries(
        sources=SOURCES,
        stores=STORES,
        notifiers=NOTIFIERS,
        dashboard_providers=DASHBOARD_PROVIDERS,
    )


# Map a `provides:` token to its `SignalKind`. `capabilities()` is authoritative, so
# this only exists to compare the *declared* set against the *built* set.
_PROVIDES_TO_KIND: dict[str, SignalKind] = {kind.value: kind for kind in SignalKind}


def _interpolate(value: str) -> str:
    """Replace every `${VAR}` in `value` from `os.environ`, failing fast if unset."""

    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        resolved = os.environ.get(var)
        if resolved is None:
            raise MissingEnvVarError(var)
        return resolved

    return _VAR_PATTERN.sub(_replace, value)


def _interpolate_block(block: Mapping[str, object]) -> dict[str, ConfigValue]:
    """Interpolate `${VAR}` in every string (and string-list) value of a config block.

    Returns a `ConfigValue`-typed mapping suitable for `Registry.build`. Non-scalar
    nested structures are not adapter config in v0.1; only scalars + string lists
    reach an adapter `__init__`.
    """
    resolved: dict[str, ConfigValue] = {}
    for key, raw in block.items():
        if isinstance(raw, str):
            resolved[key] = _interpolate(raw)
        elif isinstance(raw, bool):
            # bool before int — `bool` is a subclass of `int`.
            resolved[key] = raw
        elif isinstance(raw, int):
            resolved[key] = raw
        elif isinstance(raw, list):
            resolved[key] = [
                _interpolate(item) if isinstance(item, str) else str(item) for item in raw
            ]
        # Anything else (nested dicts) is not adapter config and is skipped.
    return resolved


def _reconcile_capabilities(source_type: str, declared: list[str], actual: set[SignalKind]) -> None:
    """Fail fast if a source's declared `provides:` disagrees with `capabilities()`.

    `capabilities()` is authoritative; `provides:` is advisory and OPTIONAL — a
    source that omits `provides:` makes no claim, so there is nothing to reconcile
    and the function returns. When `provides:` IS declared it must match exactly; a
    disagreement is a config error the operator must fix, so it raises naming the
    source + both sets.
    """
    if not declared:
        return
    declared_kinds: set[SignalKind] = set()
    for token in declared:
        kind = _PROVIDES_TO_KIND.get(token)
        if kind is None:
            raise ValueError(
                f"Source '{source_type}' declares unknown capability '{token}' in "
                f"provides:; valid capabilities are {sorted(_PROVIDES_TO_KIND.keys())}."
            )
        declared_kinds.add(kind)
    if declared_kinds != actual:
        declared_repr = sorted(kind.value for kind in declared_kinds)
        actual_repr = sorted(kind.value for kind in actual)
        raise ValueError(
            f"Source '{source_type}': declared provides {declared_repr} does not match "
            f"the adapter's capabilities() {actual_repr} (capabilities() is authoritative)."
        )


def _resolve_sources(env_config: EnvironmentConfig, registries: PlaneRegistries) -> list[Source]:
    """Build + reconcile the live sources for one enabled environment."""
    resolved: list[Source] = []
    for source_config in env_config["sources"]:
        source_type = source_config["type"]
        block = _interpolate_block(source_config)
        source = registries.sources.build(source_type, block)
        declared = source_config.get("provides", [])
        _reconcile_capabilities(source_type, declared, source.capabilities())
        resolved.append(source)
    return resolved


def _resolve_dashboard_packs(dashboards: DashboardsConfig) -> list[DashboardPack]:
    """Resolve core packs (by id, under `core/dashboards/`) + the injected consumer pack."""
    packs: list[DashboardPack] = []
    for pack_id in dashboards.get("core_packs", []):
        packs.append(
            DashboardPack(
                id=pack_id,
                tier="core",
                json_path=_CORE_DASHBOARDS_DIR / pack_id / "dashboard.json",
            )
        )
    consumer_pack = dashboards.get("consumer_pack")
    if consumer_pack is not None:
        path = consumer_pack.get("path")
        git = consumer_pack.get("git")
        # The consumer_pack selector is a union: EXACTLY one of `path` (v0.1) or
        # `git` (v0.2, parsed-but-deferred). Neither (an empty/ambiguous block) or
        # both at once is a config error rejected here at resolve time.
        if (path is None) == (git is None):
            raise PanoptesError(
                "consumer_pack must specify exactly one of `path` (v0.1) or `git` "
                f"(v0.2); got path={path!r}, git={git!r}."
            )
        if path is not None:
            resolved_path = _interpolate(path)
            packs.append(
                DashboardPack(
                    id="consumer",
                    tier="consumer",
                    # The provider (Phase 5) globs dashboard.json under this dir; the
                    # path itself is the injected mount root.
                    json_path=Path(resolved_path),
                )
            )
        # The `git` variant parses + validates here but is NOT resolved in v0.1: the
        # Grafana provider raises a clear CapabilityError at provision time (Phase 5).
    return packs


def load_config(path: Path, registries: PlaneRegistries | None = None) -> ResolvedConfig:
    """Load + resolve a Panoptes config file into a fully-typed `ResolvedConfig`.

    Args:
        path: Path to the `panoptes:` YAML file.
        registries: Plane registries to build adapters from. Defaults to the four
            `core.registry` module singletons; tests inject fakes.

    Returns:
        A `ResolvedConfig` with instantiated adapters (live only for enabled envs).

    Raises:
        MissingEnvVarError: a referenced `${VAR}` is unset.
        UnknownAdapterError: a config block names an unregistered adapter `type`.
        ValueError: a source's `provides:` disagrees with its `capabilities()`.
    """
    active_registries = registries if registries is not None else _default_registries()

    # Parse YAML, surfacing a syntax error as a clear PanoptesError rather than a
    # raw yaml.YAMLError the operator would have to decode.
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PanoptesError(f"Config file {path} is not valid YAML: {exc}") from exc
    # The top-level shape is the loader's contract: a single `panoptes:` mapping.
    if not isinstance(raw, dict) or "panoptes" not in raw:
        raise PanoptesError(
            f"Config file {path} must have a top-level 'panoptes:' mapping; "
            f"got {type(raw).__name__}."
        )
    config = cast(PanoptesConfig, raw)
    body = config["panoptes"]

    environments: dict[str, ResolvedEnvironment] = {}
    for env_name, env_config in body.get("environments", {}).items():
        enabled = env_config["enabled"]
        # Disabled envs parse but produce NO live adapters (wired-but-inert).
        sources = _resolve_sources(env_config, active_registries) if enabled else []
        environments[env_name] = ResolvedEnvironment(
            name=env_name, enabled=enabled, sources=sources
        )

    store_config = body["store"]
    store = active_registries.stores.build(store_config["type"], _interpolate_block(store_config))

    notifiers: list[Notifier] = []
    for notifier_config in body.get("notifiers", []):
        notifiers.append(
            active_registries.notifiers.build(
                notifier_config["type"], _interpolate_block(notifier_config)
            )
        )

    dashboard_packs = _resolve_dashboard_packs(body.get("dashboards", {}))

    return ResolvedConfig(
        environments=environments,
        store=store,
        notifiers=notifiers,
        dashboard_packs=dashboard_packs,
        slos=list(body.get("slos", [])),
        mcp=body.get("mcp", {}),
    )
