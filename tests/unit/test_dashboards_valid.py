"""Phase 5 unit tests — the Grafana provider + the three shipped core packs.

Covers (spec `## Tests` → Dashboards + the two provision-time negative paths in
`## Tests` → Config):

- each shipped core dashboard JSON (`errors-sentry`, `logs`, `overview`) is valid
  JSON;
- each declares the `env` Grafana template variable (in `templating.list`, named
  `env`) — every face keys on `env`, so a missing template variable is a regression;
- each panel's PromQL targets reference ONLY `panoptes_*` metric names AND only the
  labels the corresponding derived metric actually emits (a label-drift guard: a
  panel that filters on a label the metric never emits silently renders empty);
- `provision(packs)` SYNCS the resolved JSON into the provisioning dir (the files
  land under a `tmp_path` provisioning dir);
- the INJECTION test: a `tmp_path` consumer dir containing
  `dashboards/demo/dashboard.json` is globbed + provisioned ALONGSIDE the core
  packs (the provider must NOT hardcode the `demo`/`consumer` names — it globs);
- the git-only consumer pack raises `CapabilityError` at provision;
- an unknown core-pack id (no `dashboard.json` under `core/dashboards/`) raises a
  clear `PanoptesError` naming the missing pack id.

The Grafana HTTP ping is exercised through an injected fake `httpx.Client`-shaped
seam (mirroring the VM store): with no client/url the provider skips the ping, so
the disk-sync assertions stay deterministic and offline. Brand-neutrality: the
literal `demo` appears ONLY here in test code — never in `core/`.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
import respx
from core.dashboards.grafana import GrafanaDashboardProvider
from core.errors import CapabilityError, PanoptesError
from core.model import DashboardPack

# The in-repo core packs the provider ships + provisions (spec UI-Surfaces table).
_CORE_PACK_IDS = ("errors-sentry", "logs", "overview")
_CORE_DASHBOARDS_DIR = Path(__file__).resolve().parents[2] / "core" / "dashboards"

# The exact label set each derived metric emits (asserted against each source's
# `MetricSignal.labels` — a panel must not filter on a label outside this set).
_METRIC_LABELS: dict[str, frozenset[str]] = {
    "panoptes_sentry_incident_count": frozenset({"env", "level", "project"}),
    "panoptes_log_error_rate": frozenset({"env", "log_group"}),
    "panoptes_health_up": frozenset({"env", "url"}),
    "panoptes_health_latency_ms": frozenset({"env", "url"}),
}

# A PromQL label selector token: `metric_name{label="value", ...}`. The metric name
# is the leading identifier; the brace body holds `label op value` filters.
_METRIC_REF_RE = re.compile(r"\b(panoptes_[a-z_]+)\b(?:\s*\{([^}]*)\})?")
_LABEL_KEY_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=|!=|=~|!~)")
# A PromQL aggregation grouping clause: `by (label, ...)` (e.g. `sum by (project, env)`).
# The captured body is the comma-separated grouping label list.
_BY_CLAUSE_RE = re.compile(r"\bby\s*\(([^)]*)\)")
# Grafana template-variable interpolation (e.g. `env="$env"`) — always allowed.
_TEMPLATE_VAR = "env"


def _load_core_json(pack_id: str) -> dict[str, object]:
    """Parse a shipped core pack's `dashboard.json` (asserts it is valid JSON)."""
    path = _CORE_DASHBOARDS_DIR / pack_id / "dashboard.json"
    text = path.read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert isinstance(parsed, dict), f"{pack_id} dashboard.json must be a JSON object"
    return parsed


def _templating_names(dashboard: dict[str, object]) -> set[str]:
    """The set of template-variable names declared in `templating.list`."""
    templating = dashboard.get("templating")
    assert isinstance(templating, dict), "dashboard must declare a 'templating' object"
    variables = templating.get("list")
    assert isinstance(variables, list), "'templating.list' must be a list"
    names: set[str] = set()
    for variable in variables:
        assert isinstance(variable, dict), "each template variable must be an object"
        name = variable.get("name")
        assert isinstance(name, str), "each template variable must have a string 'name'"
        names.add(name)
    return names


def _panel_exprs(dashboard: dict[str, object]) -> list[str]:
    """Every panel target's PromQL `expr` string in a dashboard."""
    panels = dashboard.get("panels")
    assert isinstance(panels, list), "dashboard must declare a 'panels' list"
    exprs: list[str] = []
    for panel in panels:
        assert isinstance(panel, dict), "each panel must be an object"
        targets = panel.get("targets", [])
        assert isinstance(targets, list), "panel 'targets' must be a list"
        for target in targets:
            assert isinstance(target, dict), "each target must be an object"
            expr = target.get("expr")
            assert isinstance(expr, str) and expr, "each target must carry a non-empty 'expr'"
            exprs.append(expr)
    return exprs


# --- core JSON validity + env template variable ---------------------------------


@pytest.mark.parametrize("pack_id", _CORE_PACK_IDS)
def test_core_dashboard_json_is_valid(pack_id: str) -> None:
    dashboard = _load_core_json(pack_id)
    # A non-empty title + an id-less dashboard (Grafana assigns the uid) is the
    # minimum a provisioned file needs; the parse above already proves validity.
    assert dashboard.get("title"), f"{pack_id} dashboard must declare a non-empty title"


@pytest.mark.parametrize("pack_id", _CORE_PACK_IDS)
def test_core_dashboard_has_env_template_variable(pack_id: str) -> None:
    dashboard = _load_core_json(pack_id)
    assert _TEMPLATE_VAR in _templating_names(dashboard), (
        f"{pack_id} dashboard must declare an '{_TEMPLATE_VAR}' template variable"
    )


@pytest.mark.parametrize("pack_id", _CORE_PACK_IDS)
def test_core_dashboard_references_only_known_metrics_and_labels(pack_id: str) -> None:
    """Every PromQL target references a known `panoptes_*` metric and only labels it emits."""
    for expr in _panel_exprs(_load_core_json(pack_id)):
        matched_metric = False
        for metric_match in _METRIC_REF_RE.finditer(expr):
            metric_name = metric_match.group(1)
            assert metric_name in _METRIC_LABELS, (
                f"{pack_id}: expr {expr!r} references unknown metric {metric_name!r}"
            )
            matched_metric = True
            label_body = metric_match.group(2) or ""
            for label_key in _LABEL_KEY_RE.findall(label_body):
                if label_key == _TEMPLATE_VAR:
                    continue  # the env template variable is always permitted
                assert label_key in _METRIC_LABELS[metric_name], (
                    f"{pack_id}: expr {expr!r} filters {metric_name!r} on label "
                    f"{label_key!r} which it never emits "
                    f"(emits {sorted(_METRIC_LABELS[metric_name])})"
                )
        assert matched_metric, f"{pack_id}: expr {expr!r} references no panoptes_* metric"


@pytest.mark.parametrize("pack_id", _CORE_PACK_IDS)
def test_core_dashboard_grouping_labels_are_emitted_by_the_metric(pack_id: str) -> None:
    """Every `by (...)` grouping label is a label the grouped metric actually emits (F6).

    A `sum by (level) (metric{...})` panel can only render a real per-label breakdown if
    `level` is a label `metric` emits — otherwise the grouping collapses to a single
    constant-labeled series (exactly the bug F6 fixed in the sentry source). This guard
    catches a future grouping-on-a-non-emitted-label drift at test time.
    """
    for expr in _panel_exprs(_load_core_json(pack_id)):
        grouping_labels: set[str] = set()
        for by_clause in _BY_CLAUSE_RE.finditer(expr):
            for raw_label in by_clause.group(1).split(","):
                label = raw_label.strip()
                if label:
                    grouping_labels.add(label)
        if not grouping_labels:
            continue
        # The expr references exactly one panoptes metric family; group against its labels.
        metric_names = {match.group(1) for match in _METRIC_REF_RE.finditer(expr)}
        metric_names &= set(_METRIC_LABELS)
        assert metric_names, f"{pack_id}: expr {expr!r} groups but references no known metric"
        for metric_name in metric_names:
            emitted = _METRIC_LABELS[metric_name]
            for label in grouping_labels:
                if label == _TEMPLATE_VAR:
                    continue  # the env template variable is always a valid grouping key
                assert label in emitted, (
                    f"{pack_id}: expr {expr!r} groups {metric_name!r} by label {label!r} "
                    f"which it never emits (emits {sorted(emitted)})"
                )


# --- provider sync + injection --------------------------------------------------


def _provider(provisioning_dir: Path) -> GrafanaDashboardProvider:
    """A provider syncing into `provisioning_dir`, with the Grafana ping disabled."""
    # No `url` -> the provider skips the API ping, keeping the test offline.
    return GrafanaDashboardProvider({"provisioning_dir": str(provisioning_dir)})


def _core_packs() -> list[DashboardPack]:
    return [
        DashboardPack(
            id=pack_id, tier="core", json_path=_CORE_DASHBOARDS_DIR / pack_id / "dashboard.json"
        )
        for pack_id in _CORE_PACK_IDS
    ]


def test_provision_writes_core_json_into_provisioning_dir(tmp_path: Path) -> None:
    provisioning_dir = tmp_path / "provisioning"
    _provider(provisioning_dir).provision(_core_packs())
    synced = {path.name for path in provisioning_dir.rglob("*.json")}
    # Each core pack's dashboard.json lands under the provisioning dir.
    assert synced, "provision must write at least one dashboard.json"
    for pack_id in _CORE_PACK_IDS:
        matches = list(provisioning_dir.rglob(f"*{pack_id}*.json"))
        assert matches, f"core pack {pack_id} not synced into the provisioning dir"


def test_provision_globs_injected_consumer_pack_alongside_core(tmp_path: Path) -> None:
    """A temp consumer dir's `dashboards/demo/dashboard.json` is globbed + provisioned."""
    consumer_dir = tmp_path / "packs" / "consumer"
    demo_json = consumer_dir / "dashboards" / "demo" / "dashboard.json"
    demo_json.parent.mkdir(parents=True)
    demo_json.write_text(
        json.dumps(
            {
                "title": "demo",
                "templating": {"list": [{"name": "env"}]},
                "panels": [{"targets": [{"expr": 'panoptes_health_up{env="$env"}'}]}],
            }
        ),
        encoding="utf-8",
    )
    provisioning_dir = tmp_path / "provisioning"
    consumer_pack = DashboardPack(id="consumer", tier="consumer", json_path=consumer_dir)
    _provider(provisioning_dir).provision([*_core_packs(), consumer_pack])

    synced_names = {path.name for path in provisioning_dir.rglob("*.json")}
    assert synced_names, "provision must sync dashboards"
    # The consumer demo pack is provisioned ALONGSIDE the core packs (glob, not hardcode).
    demo_matches = list(provisioning_dir.rglob("*demo*.json"))
    assert demo_matches, "injected consumer demo pack was not globbed + provisioned"
    for pack_id in _CORE_PACK_IDS:
        assert list(provisioning_dir.rglob(f"*{pack_id}*.json")), (
            f"core pack {pack_id} missing after consumer injection"
        )


def test_provision_pings_grafana_when_url_configured(tmp_path: Path) -> None:
    """With a `url`, the provider pings the Grafana search API after syncing."""
    provisioning_dir = tmp_path / "provisioning"
    grafana_url = "http://grafana:3000"
    with respx.mock:
        route = respx.get(f"{grafana_url}/api/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        provider = GrafanaDashboardProvider(
            {"provisioning_dir": str(provisioning_dir), "url": grafana_url}
        )
        provider.provision(_core_packs())
        assert route.called, "provider must ping the Grafana search API when a url is configured"


# --- negative paths this phase owns ---------------------------------------------


def test_git_selected_consumer_pack_raises_capability_error(tmp_path: Path) -> None:
    """A git-selected consumer pack raises CapabilityError at provision (v0.2 deferral)."""
    git_pack = DashboardPack(
        id="consumer",
        tier="consumer",
        json_path=Path("https://example.invalid/repo.git"),
        selector="git",
    )
    with pytest.raises(CapabilityError) as excinfo:
        _provider(tmp_path / "provisioning").provision([git_pack])
    assert "git injection is v0.2" in str(excinfo.value)
    assert "`path`" in str(excinfo.value)


def test_unknown_core_pack_id_fails_fast_naming_the_pack(tmp_path: Path) -> None:
    """A core pack whose json_path does not exist raises a PanoptesError naming the id."""
    missing_pack = DashboardPack(
        id="typo-pack",
        tier="core",
        json_path=_CORE_DASHBOARDS_DIR / "typo-pack" / "dashboard.json",
    )
    with pytest.raises(PanoptesError) as excinfo:
        _provider(tmp_path / "provisioning").provision([missing_pack])
    assert "typo-pack" in str(excinfo.value)
