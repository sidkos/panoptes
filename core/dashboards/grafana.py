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
   - a *git*-selected consumer pack (v0.2 hosted path) first has its pinned ref
     VALIDATED here in-pod — a full 40-hex commit SHA is REQUIRED; a mutable branch
     (`main`/`HEAD`), a tag, or a short SHA is rejected with a clear `CapabilityError`,
     because the pinned ref is a code-execution trust boundary (the fetched subdir
     may carry an executable `pack.py`). This provider does the ref VALIDATION + a
     READ-ONLY glob of the already-present `json_path` subdir (exactly like the
     mounted-`path` case) — it does NOT itself fetch. The deploy-time `git fetch`
     job that places the subdir (a Terraform null_resource / Helm pre-install job)
     is the OTHER HALF of the trust boundary and is unimplemented future
     deploy-wiring; when it lands it MUST pass THIS same validated `git_ref` to its
     `git fetch` so the validated ref and the fetched ref cannot diverge.
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

import re
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

# A FULL git commit SHA: exactly 40 lowercase hex chars (git's default SHA-1 object id).
# The git-injection variant REQUIRES a full-SHA pin (DASHBOARDS §4 control (a)): a mutable
# branch ref (`main`/`HEAD`) or a short ref is rejected — pulling a moving ref would be
# arbitrary-code-execution-on-deploy (the pinned ref is a code-execution trust boundary,
# because the fetched subdir may carry an executable `pack.py`). 40-hex only (a 7-char
# short SHA, a tag, or `main`/`HEAD` all fail the anchor).
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


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

    def close(self) -> None:
        """Close the provider's REST client httpx pool (F2c socket hygiene).

        The provider owns a `RestClient` even when no Grafana `url` is configured (the
        offline file-sync case), so a directly-built provider should close it to keep
        teardown clean under `-W error::ResourceWarning`. Idempotent.
        """
        self._rest.close()

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

        Core packs resolve to a single file (fail fast if missing); consumer packs glob
        their mounted/already-fetched dir. A `git`-selected consumer pack first has its
        pinned ref VALIDATED (a full commit SHA is required — a mutable branch is rejected),
        then its `json_path` dir is globbed READ-ONLY exactly like the mounted-`path` case.
        """
        if pack.tier == "consumer" and pack.selector == "git":
            # The git-injection variant (DASHBOARDS §4). This provider's role is the IN-POD
            # ref-validation security gate + a read-only consume of the already-present
            # `pack.json_path` dir — it does NOT fetch. The deploy-time `git fetch` that
            # places that dir (a Terraform null_resource / Helm pre-install job, the OTHER
            # half of the trust boundary) is unimplemented future deploy-wiring; running the
            # fetch in this long-running pod would be a worse trust boundary than a reviewed
            # deploy-time fetch (spec § No repo-write credential). When the fetch job lands
            # it MUST pass THIS same validated `git_ref` to `git fetch` so the validated ref
            # and the fetched ref cannot diverge.
            self._require_full_sha_pin(pack)
            return self._glob_consumer_dir(pack)

        if pack.tier == "core":
            if not pack.json_path.exists():
                # An operator typo'd a core_packs id — name it so the fix is obvious.
                raise PanoptesError(
                    f"Core dashboard pack '{pack.id}' has no dashboard.json at "
                    f"{pack.json_path} — check the core_packs id is spelled correctly."
                )
            return [(pack.id, pack.json_path)]

        # A path-selected consumer pack: glob the mounted dir (read-only) — the provider
        # discovers whatever the consumer injected; it never hardcodes a pack name.
        return self._glob_consumer_dir(pack)

    def _require_full_sha_pin(self, pack: DashboardPack) -> None:
        """Reject a `git` pack whose ref is not a full commit SHA (the code-exec trust gate).

        DASHBOARDS §4 control (a): the pinned ref MUST be an immutable full 40-hex commit
        SHA — a mutable branch (`main`/`HEAD`), a tag, or a short SHA is rejected, because
        the (future) deploy-time fetch job uses this ref to pull a subdir that may carry an
        executable `pack.py`; a moving ref would be arbitrary-code-execution-on-deploy. This
        method is the IN-POD validation half of that boundary: it does not fetch — it gates
        the ref so the validated ref is the one the fetch job must use (they cannot diverge).
        The `\\A...\\Z` anchors on `_FULL_SHA_RE` also reject newline-smuggling / path-traversal
        / uppercase-hex bypass refs (only 40 lowercase-hex with no surrounding bytes passes).
        Raises a clear `CapabilityError` naming the requirement so the operator fix is obvious.

        Raises:
            CapabilityError: `git_ref` is absent or not a full 40-hex commit SHA.
        """
        ref = pack.git_ref
        if ref is None or not _FULL_SHA_RE.match(ref):
            raise CapabilityError(
                f"git dashboard pack '{pack.id}' must pin an IMMUTABLE full 40-hex commit "
                f"SHA (got {ref!r}); a mutable branch (main/HEAD), a tag, or a short SHA is "
                f"rejected — the pinned ref is a code-execution trust boundary (DASHBOARDS §4)."
            )

    def _glob_consumer_dir(self, pack: DashboardPack) -> list[tuple[str, Path]]:
        """Glob `dashboards/**/dashboard.json` under a consumer pack's dir (READ-ONLY).

        Shared by the `path` (mounted dir) and validated-`git` (already-fetched dir) cases:
        the provider only READS the source tree here (the write happens later into the
        SEPARATE provisioning dir via `_sync_one`), so the mounted/fetched tree is never
        mutated in place. Each synced file is keyed on the pack id + its source pack-dir name
        so two packs that both ship a `dashboard.json` never clobber in the provisioning dir.
        """
        matches = sorted(pack.json_path.glob(_DASHBOARD_GLOB))
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
