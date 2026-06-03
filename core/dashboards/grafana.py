"""The `grafana` dashboard provider — file-provisioning sync + API ping.

`provision(packs)` is the single entry point. It receives BOTH tiers of resolved
`DashboardPack`s (always-shipped core packs under `core/dashboards/`, plus the
injected consumer pack whose `json_path` is a mounted dir) and:

1. **Resolves** each pack's `dashboard.json` file(s):
   - a *core* pack's `json_path` points directly at its `dashboard.json`; if that
     file does not exist the operator typo'd a `core_packs` id, so we FAIL FAST with
     a `PanoptesError` naming the missing pack (a silently-skipped core pack would
     surface as an empty Grafana later, far from the cause);
   - a *consumer* pack's `json_path` is the mounted *dir*; we GLOB
     `dashboards/**/dashboard.json` under it (the provider never hardcodes the
     `demo`/`consumer` pack name — it discovers whatever the consumer injected);
   - a *git*-selected consumer pack is rejected here: git injection is deferred to
     v0.2, so we raise a clear `CapabilityError`. The deferral is enforced at
     PROVISION time (not silently at resolve time) so "parses OK, acting on it fails
     in v0.1" stays explicit and testable.
2. **Syncs** every resolved JSON into the Grafana file-provisioning dir via
   `Path.write_text` (the no-write guard allowlists `write_text`/`write_bytes` —
   Risk R6; this is local file I/O, not a boto3 mutation).
3. **Pings** the Grafana HTTP API (`GET /api/search`) to confirm the dashboards
   loaded — but ONLY when a `url` is configured. With no url (the unit-test default)
   the ping is skipped so the sync is verifiable fully offline. The ping delegates to
   the shared `core.rest.RestClient`, so it gains the both-branch failure surfacing
   (the upstream body on an HTTP-status error) for free; `respx` intercepts the
   `RestClient`'s default client in tests, and the client is still an injectable seam.

Grafana assigns each dashboard a uid from its file; collisions across packs are
avoided by syncing each pack into a per-pack subdir keyed on the pack id + the
source pack-dir name, so two packs that both ship a `dashboard.json` never clobber.
"""

from pathlib import Path

import httpx

from core.errors import CapabilityError, PanoptesError
from core.model import DashboardPack
from core.registry import DASHBOARD_PROVIDERS, ConfigBlock
from core.rest import RestClient

# The consumer-pack dir is globbed for this pattern; a core pack's `json_path`
# already points straight at its `dashboard.json`.
_DASHBOARD_GLOB = "dashboards/**/dashboard.json"

# Default Grafana file-provisioning target. Overridable via config so unit tests
# point it at a `tmp_path` dir.
_DEFAULT_PROVISIONING_DIR = Path("core/dashboards/provisioning/generated")

# The Grafana HTTP endpoint hit to confirm dashboards loaded (read-only).
_SEARCH_PATH = "/api/search"

# The exact CapabilityError message the git-deferral negative path asserts on
# (spec `## Tests` → Config). Kept as a literal so the message never drifts.
_GIT_DEFERRED_DETAIL = "git injection is v0.2; use `path` in v0.1"


@DASHBOARD_PROVIDERS.register("grafana")
class GrafanaDashboardProvider:
    """Provisions core + injected consumer dashboard packs via Grafana file provisioning."""

    type = "grafana"

    def __init__(self, config: ConfigBlock, client: httpx.Client | None = None) -> None:
        """Read the provisioning dir + optional Grafana `url`; accept an httpx seam.

        Args:
            config: the provider config block. `provisioning_dir` (str) overrides the
                file-provisioning target (tests point it at `tmp_path`); `url` (str,
                optional) is the Grafana base URL — when absent the post-sync ping is
                skipped, so the sync is verifiable fully offline.
            client: an optional injected httpx client (mirrors the VM store seam). A
                default `httpx.Client()` is intercepted globally by `respx` in tests,
                so production passes none. It is threaded into the shared `RestClient`,
                which owns the ping transport + failure surfacing.
        """
        provisioning_dir = config.get("provisioning_dir")
        if isinstance(provisioning_dir, str) and provisioning_dir:
            self._provisioning_dir = Path(provisioning_dir)
        else:
            self._provisioning_dir = _DEFAULT_PROVISIONING_DIR

        url = config.get("url")
        # An empty/absent url disables the ping (the offline unit-test default).
        self._grafana_url = url.rstrip("/") if isinstance(url, str) and url else None
        self._rest = RestClient(client)

    def provision(self, packs: list[DashboardPack]) -> None:
        """Resolve, sync, and (when a url is configured) confirm every pack's JSON.

        Raises:
            CapabilityError: a consumer pack selected via `git` (deferred to v0.2).
            PanoptesError: a core pack whose `dashboard.json` does not exist (an
                operator-typo'd `core_packs` id), named in the message.
        """
        # Collect (sync_subdir, source_json_path) pairs first so a fail-fast on any
        # pack happens BEFORE we touch the provisioning dir (no half-synced state).
        resolved: list[tuple[str, Path]] = []
        for pack in packs:
            resolved.extend(self._resolve_pack(pack))

        for sync_subdir, source_json in resolved:
            self._sync_one(sync_subdir, source_json)

        # Confirm the dashboards loaded — only when a Grafana url is configured.
        if self._grafana_url is not None:
            self._ping_grafana()

    def _resolve_pack(self, pack: DashboardPack) -> list[tuple[str, Path]]:
        """Resolve one pack to its `(sync_subdir, dashboard.json)` pairs.

        Core packs resolve to a single file (fail fast if missing); consumer packs
        glob their mounted dir; a git-selected consumer pack raises `CapabilityError`.
        """
        if pack.tier == "consumer" and pack.selector == "git":
            # Deferred to v0.2 — enforced here at provision time, not at resolve time.
            raise CapabilityError(_GIT_DEFERRED_DETAIL)

        if pack.tier == "core":
            if not pack.json_path.exists():
                # An operator typo'd a core_packs id — name it so the fix is obvious.
                raise PanoptesError(
                    f"Core dashboard pack '{pack.id}' has no dashboard.json at "
                    f"{pack.json_path} — check the core_packs id is spelled correctly."
                )
            return [(pack.id, pack.json_path)]

        # A path-selected consumer pack: glob `dashboards/**/dashboard.json` under the
        # mounted dir. The provider discovers whatever the consumer injected — it
        # never hardcodes a pack name.
        matches = sorted(pack.json_path.glob(_DASHBOARD_GLOB))
        # Key each synced file on the pack id + its source pack-dir name so two packs
        # that both ship a `dashboard.json` never clobber in the provisioning dir.
        return [(f"{pack.id}-{source_json.parent.name}", source_json) for source_json in matches]

    def _sync_one(self, sync_subdir: str, source_json: Path) -> None:
        """Copy one source `dashboard.json` into the provisioning dir under `sync_subdir`.

        The file is named `<sync_subdir>.json` so its provenance (which pack it came
        from) is visible on disk and the test's pack-id glob finds it. `write_text` is
        the allowlisted file-I/O verb (Risk R6) — local disk, not a boto3 mutation.
        """
        target_dir = self._provisioning_dir / sync_subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        # Name the synced file `<sync_subdir>.json` so its provenance (the pack id) is
        # visible on disk and the test's per-pack-id glob locates it.
        target_file = target_dir / f"{sync_subdir}.json"
        target_file.write_text(source_json.read_text(encoding="utf-8"), encoding="utf-8")

    def _ping_grafana(self) -> None:
        """GET the Grafana search API to confirm the provisioned dashboards loaded.

        A non-2xx response or a transport error is surfaced as a `PanoptesError` via the
        shared `RestClient` — so the ping now carries the upstream response body on an
        HTTP-status error (the deeper failure surfacing it gains for free), naming the
        endpoint. A provisioning sync that Grafana never picked up is a real failure the
        operator must see, not a silent success.
        """
        endpoint = f"{self._grafana_url}{_SEARCH_PATH}"
        self._rest.send(
            lambda http: http.get(endpoint),
            prefix="Grafana provisioning ping failed",
            identifier=endpoint,
        )
