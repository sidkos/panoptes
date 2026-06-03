"""Integration: Grafana loads the core packs AND the injected demo consumer pack.

Proves the dashboard-provisioning leg + the mount-injection path (spec `## Tests` →
Integration, bullet 2). The `grafana` fixture provisions the three core dashboard
packs PLUS the injected demo pack (synced via the real `GrafanaDashboardProvider`,
bind-mounted into Grafana's file provider) and the datasource. This test then POLLS
`GET /api/search` with bounded retry/backoff (Risk R13 — provisioning is async; never
a single immediate assertion) until ALL FOUR expected dashboard titles appear.

Seeing the injected `Demo (consumer pack)` title alongside the three core titles is
the literal proof that the consumer-pack mount-injection path works end to end — the
provider globbed the mounted dir and Grafana loaded what it found, with zero domain
content bundled into core.
"""

import pytest

from .conftest import _EXPECTED_DASHBOARD_TITLES, GrafanaHandle

pytestmark = pytest.mark.integration


def test_all_four_dashboards_provisioned_including_injected_demo(
    grafana: GrafanaHandle,
) -> None:
    """Poll `/api/search` until the 3 core + 1 injected-demo dashboards all appear.

    The injected `Demo (consumer pack)` title is the mount-injection proof: it is NOT
    shipped in core, so its presence confirms the consumer pack was discovered by glob
    and loaded by Grafana via the read-only mount.
    """
    observed_titles = grafana.poll_search_titles(_EXPECTED_DASHBOARD_TITLES)

    # The poll only returns once `expected` is a subset; assert explicitly for a clear
    # failure message if the contract ever changes.
    missing = _EXPECTED_DASHBOARD_TITLES - observed_titles
    assert not missing, f"Grafana never loaded dashboards: {sorted(missing)}"
    # The injected consumer pack specifically — the mount-injection path under test.
    assert "Demo (consumer pack)" in observed_titles
