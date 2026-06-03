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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict, cast

import yaml

from core.alerts import AlertRule, Comparison
from core.errors import CapabilityMismatchError, MissingEnvVarError, PanoptesError
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


class AlertRuleConfig(TypedDict, total=False):
    """One declarative alert rule entry under `alerts:` (spec § Alert-rule model).

    `name`/`expr`/`comparison`/`threshold` are required; `for_cycles` (debounce, default
    1), `severity` (default `warning`), `envs` (default `["all"]`), and `labels` are
    optional. `comparison` is one of the `Comparison` enum values (`gt`/`ge`/`lt`/`le`);
    an unknown value fails fast at resolve time.
    """

    name: str
    expr: str
    comparison: str
    threshold: float
    for_cycles: int
    severity: str
    envs: list[str]
    labels: dict[str, str]


class _PanoptesBody(TypedDict, total=False):
    """The body under the top-level `panoptes:` key."""

    environments: dict[str, EnvironmentConfig]
    store: StoreConfig
    notifiers: list[NotifierConfig]
    dashboards: DashboardsConfig
    slos: list[SloConfig]
    alerts: list[AlertRuleConfig]
    mcp: McpConfig


class PanoptesConfig(TypedDict):
    """The whole config file: a single top-level `panoptes:` key."""

    panoptes: _PanoptesBody


# --- Runtime resolved shapes -----------------------------------------------------


# Per-source collector timing defaults (spec `## Performance Constraints`). Poll
# cadence + fetch bound are a *collector* concern, not a `Source` concern — the
# `Source` Protocol deliberately carries no timing attrs — so the loader wraps each
# built `Source` in a `ResolvedSource` carrying these resolved values for the
# collector to consume. Both are config-overridable per source.
_DEFAULT_FETCH_TIMEOUT_SECONDS = 30
_DEFAULT_POLL_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class ResolvedSource:
    """A built `Source` plus its resolved collector timing.

    The `Source` Protocol intentionally has no timing attributes — poll cadence and
    fetch-timeout are collector concerns, not properties of the upstream adapter. The
    loader reads `fetch_timeout_seconds` (default 30s) and `poll_interval_seconds`
    (default 60s) from each source's config block (both already in `SourceConfig`)
    and pairs them with the built `Source` here, so the collector has everything it
    needs to bound + schedule a fetch without the `Source` Protocol leaking timing.
    """

    source: Source
    fetch_timeout_seconds: int
    poll_interval_seconds: int


@dataclass(frozen=True)
class ResolvedEnvironment:
    """An environment after resolution. Disabled envs carry an empty source list."""

    name: str
    enabled: bool
    sources: list[ResolvedSource]


@dataclass(frozen=True)
class ResolvedConfig:
    """The fully-typed result of loading + resolving a config file."""

    environments: dict[str, ResolvedEnvironment]
    store: Store
    notifiers: list[Notifier]
    dashboard_packs: list[DashboardPack]
    slos: list[SloConfig]
    # The declarative alert rules the collector evaluates each cycle (v0.2). Defaults to
    # empty so the many existing keyword-arg construction sites (tests + the MCP query
    # context) need not pass `alerts=[]`; `load_config` always supplies the resolved list.
    # `mcp` is given a default too because a dataclass field with a default cannot precede
    # one without — an mcp-less config is a valid (tool-less) server, so `{}` is correct.
    alerts: list[AlertRule] = field(default_factory=list)
    # `McpConfig()` (not `dict`) as the factory so the default is typed `McpConfig`, not
    # `dict[Never, Never]` — `McpConfig` is `total=False`, so an empty instance is valid.
    mcp: McpConfig = field(default_factory=McpConfig)


@dataclass(frozen=True)
class PlaneRegistries:
    """The four plane registries the loader builds adapters from (injectable for tests).

    This is the documented canonical seam for everything OUTSIDE the self-registration
    path. The `core.registry` module globals (`SOURCES`/`STORES`/`NOTIFIERS`/
    `DASHBOARD_PROVIDERS`) remain load-bearing for the `@SOURCES.register(...)`
    self-registration decorators (and the demo pack's `@STORES.register(...)`); for
    everything else — most importantly test isolation — build a `PlaneRegistries` via
    one of the two factories below and pass it to `load_config(registries=...)`.

    Two factories cover the two needs:

    - `empty()` — four FRESH, empty, correctly-keyed registries: full isolation from
      the module globals, with no risk of mistyping a plane discriminator string.
    - `from_globals()` — the four module singletons: the production wiring the
      self-registration decorators populate (the `load_config` default).
    """

    sources: Registry[Source]
    stores: Registry[Store]
    notifiers: Registry[Notifier]
    dashboard_providers: Registry[DashboardProvider]

    @classmethod
    def empty(cls) -> "PlaneRegistries":
        """Four fresh, empty, plane-keyed registries — the test-isolation seam.

        Each plane gets a brand-new `Registry` keyed by its discriminator, fully
        independent of the `core.registry` module globals. A test obtains an ISOLATED
        registry set (register fakes, pass to `load_config`) instead of registering
        for-side-effect into the shared globals — so no test pollutes another.
        """
        return cls(
            sources=Registry("source"),
            stores=Registry("store"),
            notifiers=Registry("notifier"),
            dashboard_providers=Registry("dashboard"),
        )

    @classmethod
    def from_globals(cls) -> "PlaneRegistries":
        """The production registries — the four `core.registry` module singletons.

        These are the registries the self-registration decorators populate at adapter
        import, so this is the wiring production resolution uses (and the `load_config`
        default). Distinct from `empty()`, which deliberately does NOT see them.
        """
        return cls(
            sources=SOURCES,
            stores=STORES,
            notifiers=NOTIFIERS,
            dashboard_providers=DASHBOARD_PROVIDERS,
        )


def _default_registries() -> PlaneRegistries:
    """The production registries (delegates to the `from_globals()` factory)."""
    return PlaneRegistries.from_globals()


# Map a `provides:` token to its `SignalKind`. `capabilities()` is authoritative, so
# this only exists to compare the *declared* set against the *built* set.
_PROVIDES_TO_KIND: dict[str, SignalKind] = {kind.value: kind for kind in SignalKind}


def _require_present(block: Mapping[str, object], key: str, where: str) -> None:
    """Assert a required key is present in a config block, raising a clear PanoptesError.

    The loader narrows raw YAML through `TypedDict`s that mark some keys required, but a
    YAML missing such a key still parses into a plain dict — so a bare `block[key]` index
    raises a raw `KeyError` that escapes a caller's `except PanoptesError` and undercuts
    the "malformed config → clear PanoptesError" contract (F2e). Callers run this presence
    check FIRST, then index the `TypedDict` directly — so the precise per-key type is
    preserved (a generic value-returning helper would collapse every value to `object`)
    while a missing key surfaces a clear, hierarchy-correct `PanoptesError` instead.

    `where` is a human location (e.g. "the `panoptes:` body", "environment 'dev'") so the
    operator sees exactly which block is incomplete.
    """
    if key not in block:
        raise PanoptesError(f"Config error: required key '{key}' is missing from {where}.")


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
            # A config-time error within the PanoptesError hierarchy (NOT stdlib
            # ValueError, which would escape a caller's `except PanoptesError`).
            raise CapabilityMismatchError(
                f"Source '{source_type}' declares unknown capability '{token}' in "
                f"provides:; valid capabilities are {sorted(_PROVIDES_TO_KIND.keys())}."
            )
        declared_kinds.add(kind)
    if declared_kinds != actual:
        declared_repr = sorted(kind.value for kind in declared_kinds)
        actual_repr = sorted(kind.value for kind in actual)
        raise CapabilityMismatchError(
            f"Source '{source_type}': declared provides {declared_repr} does not match "
            f"the adapter's capabilities() {actual_repr} (capabilities() is authoritative)."
        )


def _resolve_sources(
    env_name: str, env_config: EnvironmentConfig, registries: PlaneRegistries
) -> list[ResolvedSource]:
    """Build + reconcile the live sources for one enabled environment.

    The environment name is injected into each source's config block as `env`: the
    loader is the single place that knows which environment a source belongs to, so
    every source stamps `env` onto its signals (the model invariant) without the
    YAML having to repeat `env:` on every source entry. An explicit `env:` in the
    YAML is honored only if it matches; a stray different value would be an operator
    error, so the loader's name is authoritative.

    Each built `Source` is wrapped in a `ResolvedSource` carrying its per-source
    collector timing (`fetch_timeout_seconds` default 30s, `poll_interval_seconds`
    default 60s), read from the source's own config block. The timing fields are
    NOT forwarded into the adapter `__init__` block (they are collector concerns,
    not adapter config), so they are read off the raw `source_config` directly.
    """
    resolved: list[ResolvedSource] = []
    _require_present(env_config, "sources", f"environment '{env_name}'")
    for source_config in env_config["sources"]:
        _require_present(source_config, "type", f"a source entry in environment '{env_name}'")
        source_type = source_config["type"]
        block = {**_interpolate_block(source_config), "env": env_name}
        source = registries.sources.build(source_type, block)
        declared = source_config.get("provides", [])
        _reconcile_capabilities(source_type, declared, source.capabilities())
        resolved.append(
            ResolvedSource(
                source=source,
                fetch_timeout_seconds=source_config.get(
                    "fetch_timeout_seconds", _DEFAULT_FETCH_TIMEOUT_SECONDS
                ),
                poll_interval_seconds=source_config.get(
                    "poll_interval_seconds", _DEFAULT_POLL_INTERVAL_SECONDS
                ),
            )
        )
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
                    # The v0.1 resolved-and-provisioned selector (vs. deferred `git`).
                    selector="path",
                )
            )
        if git is not None:
            # The `git` variant parses + validates successfully (the shape is valid;
            # git is parsed-but-deferred to v0.2, NOT rejected at parse time), so the
            # loader DOES emit a consumer-tier pack for it — marked `selector="git"`.
            # That keeps the deferral boundary explicit and real: a git-only config
            # loads cleanly, but the Grafana provider raises a clear CapabilityError
            # when asked to PROVISION the git pack ("parses OK, acting on it fails in
            # v0.1"). `json_path` records the git ref for diagnostics; the provider
            # rejects on `selector` before ever touching it.
            packs.append(
                DashboardPack(
                    id="consumer",
                    tier="consumer",
                    json_path=Path(_interpolate(git)),
                    selector="git",
                )
            )
    return packs


def _resolve_alerts(raw_alerts: list[AlertRuleConfig]) -> list[AlertRule]:
    """Parse the `alerts:` block into typed `AlertRule`s, failing fast on errors.

    Each entry requires `name`, `expr`, `comparison`, and `threshold`; `for_cycles`
    (default 1), `severity` (default `warning`), `envs` (default `["all"]`), and `labels`
    (default `{}`) are optional. The `comparison` string is mapped onto the `Comparison`
    enum; an unknown value raises a clear `PanoptesError` (hierarchy-correct, NOT a stdlib
    `ValueError` that would escape a caller's `except PanoptesError`). String fields are
    `${VAR}`-interpolated (so an alert can reference an env var, e.g. in a label value).
    """
    rules: list[AlertRule] = []
    for index, raw in enumerate(raw_alerts):
        where = f"alert rule #{index + 1}"
        for required in ("name", "expr", "comparison", "threshold"):
            _require_present(raw, required, where)
        name = _interpolate(raw["name"])
        expr = _interpolate(raw["expr"])
        comparison = _resolve_comparison(raw["comparison"], name)
        threshold = _resolve_threshold(raw["threshold"], name)
        # Optional fields with their spec defaults.
        for_cycles = raw.get("for_cycles", 1)
        severity = _interpolate(raw.get("severity", "warning"))
        envs = [_interpolate(env) for env in raw.get("envs", ["all"])]
        labels = {key: _interpolate(value) for key, value in raw.get("labels", {}).items()}
        rules.append(
            AlertRule(
                name=name,
                expr=expr,
                comparison=comparison,
                threshold=threshold,
                for_cycles=for_cycles,
                severity=severity,
                envs=envs,
                labels=labels,
            )
        )
    return rules


def _resolve_comparison(raw_comparison: str, rule_name: str) -> Comparison:
    """Map a `comparison` string onto the `Comparison` enum, failing fast if unknown.

    A bad value raises a `PanoptesError` (NOT the stdlib `ValueError` that `Comparison(...)`
    would raise, which escapes a caller's `except PanoptesError`), naming the rule + the
    valid operators.
    """
    try:
        return Comparison(raw_comparison)
    except ValueError as exc:
        valid = sorted(comparison.value for comparison in Comparison)
        raise PanoptesError(
            f"Alert rule '{rule_name}' has unknown comparison '{raw_comparison}'; "
            f"valid comparisons are {valid}."
        ) from exc


def _resolve_threshold(raw_threshold: object, rule_name: str) -> float:
    """Coerce a `threshold` to float, failing fast if it is not numeric.

    YAML parses a numeric threshold as int/float; a non-numeric value (e.g. a string typo)
    raises a clear `PanoptesError` naming the rule, never a downstream `TypeError`.
    """
    # bool is an int subclass; reject it so `threshold: true` is not silently read as 1.0.
    if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, int | float):
        raise PanoptesError(
            f"Alert rule '{rule_name}' has a non-numeric threshold {raw_threshold!r}; "
            f"threshold must be a number."
        )
    return float(raw_threshold)


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
        CapabilityMismatchError: a source's `provides:` disagrees with its
            `capabilities()` (or declares an unknown `provides:` token) — raised
            within the `PanoptesError` hierarchy, never stdlib `ValueError`.
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
        # Every env block must declare `enabled` (F2e — a missing key was a raw KeyError).
        _require_present(env_config, "enabled", f"environment '{env_name}'")
        enabled = env_config["enabled"]
        # Disabled envs parse but produce NO live adapters (wired-but-inert).
        sources = _resolve_sources(env_name, env_config, active_registries) if enabled else []
        environments[env_name] = ResolvedEnvironment(
            name=env_name, enabled=enabled, sources=sources
        )

    # The store block is required; a config without it was a raw KeyError (F2e).
    _require_present(body, "store", "the `panoptes:` body")
    store_config = body["store"]
    _require_present(store_config, "type", "the `store` block")
    store = active_registries.stores.build(store_config["type"], _interpolate_block(store_config))

    notifiers: list[Notifier] = []
    for notifier_config in body.get("notifiers", []):
        _require_present(notifier_config, "type", "a `notifiers` entry")
        notifiers.append(
            active_registries.notifiers.build(
                notifier_config["type"], _interpolate_block(notifier_config)
            )
        )

    dashboard_packs = _resolve_dashboard_packs(body.get("dashboards", {}))
    alerts = _resolve_alerts(list(body.get("alerts", [])))

    return ResolvedConfig(
        environments=environments,
        store=store,
        notifiers=notifiers,
        dashboard_packs=dashboard_packs,
        slos=list(body.get("slos", [])),
        alerts=alerts,
        mcp=body.get("mcp", {}),
    )
