"""The `sentry` source — Sentry issues into incident signals + per-level gauges.

`fetch` GETs `/api/0/projects/{org}/{project}/issues/` (filtered by `environment`),
normalizing each returned issue into an `IncidentSignal`, and additionally derives one
`panoptes_sentry_incident_count` gauge `MetricSignal` PER DISTINCT level so a Grafana
`sum by (level)` breakdown panel renders from the store (spec `## Data Model` → Derived
metrics, Open Question 4). A single `level="all"` aggregate would collapse that panel to
one mislabeled series, so the gauge is emitted per level (F6).

Capability set: `{INCIDENT, METRIC}` — `incident` is native (one per issue), `metric`
is the per-level derived count gauge.

Three REST-client behaviors the spec mandates:

- **`Retry-After` honored.** On a 429, the source reads the `Retry-After` header and
  retries once after that delay. The sleep is performed through an **injectable seam**
  (`sleep`) so a unit test can assert the value was read/respected without stalling on
  a real wall-clock sleep.
- **Failure body surfaced.** A non-429 4xx/5xx raises a typed `PanoptesError` whose
  message carries the trimmed upstream response body via the shared `core.rest`
  failure surfacing (the bare status code omits *which* field/token the upstream
  rejected — spec `## Performance Constraints`). A 401 (bad token) or 404 (wrong
  project) therefore raises rather than returning a silently-empty incident list.
- **`env` stamped.** Every emitted signal carries the configured `env` (model
  invariant); the derived gauge's exact label set is `{env, level, project}`.

The transport plumbing (the injectable `httpx.Client` seam, `raise_for_status`, and
the both-branch failure surfacing) lives once in `core.rest`; this source keeps only
its own URL building, the `Retry-After` honor, and the issue normalization. The
Retry-After flow needs to inspect a 429 status BEFORE deciding to retry, so it drives
the raw client (`RestClient.http`) for the GETs and funnels the FINAL response through
`RestClient.send` for the shared raise + failure surfacing.

httpx is mocked in tests with `respx`; the `RestClient`'s default `httpx.Client()` is
intercepted globally, and is threaded as a constructor seam for explicit control.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from core.errors import PanoptesError
from core.model import (
    CanonicalSignal,
    IncidentLevel,
    IncidentSignal,
    MetricSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.registry import SOURCES, ConfigBlock
from core.rest import RestClient
from core.sources._config import require_str_field

# Derived-metric name (spec `## Data Model` — `panoptes_` prefix avoids PromQL
# collisions with native upstream metric names).
_METRIC_INCIDENT_COUNT = "panoptes_sentry_incident_count"

# Default Sentry SaaS base; overridable per-deployment (self-hosted Sentry) via the
# `base_url` config field.
_DEFAULT_BASE_URL = "https://sentry.io"

# Cap the `Retry-After` honor so a hostile/huge header can never stall the collector
# cycle unbounded; beyond this we give up and surface the rate-limit as a failure.
_MAX_RETRY_AFTER_SECONDS = 60.0

# Map Sentry's issue `level` strings onto the `IncidentLevel` enum. Sentry emits
# `info`/`warning`/`error`/`fatal`; anything else defaults to ERROR (a conservative
# "something is wrong" rather than silently dropping the incident).
_SENTRY_LEVEL_TO_INCIDENT = {
    "info": IncidentLevel.INFO,
    "warning": IncidentLevel.WARNING,
    "error": IncidentLevel.ERROR,
    "fatal": IncidentLevel.FATAL,
}


@SOURCES.register("sentry")
class SentrySource:
    """Reads Sentry issues into `IncidentSignal`s + a derived count gauge."""

    type = "sentry"

    def __init__(
        self,
        config: ConfigBlock,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Read `org`/`project`/`token`/`env` from config; accept httpx + sleep seams.

        The `sleep` seam (default `time.sleep`) lets a unit test assert that a
        `Retry-After` delay was read and respected without actually blocking — the test
        injects a recording fake. The `client` seam mirrors the store's pattern.
        """
        self._org = require_str_field(config, "org", self.type)
        self._project = require_str_field(config, "project", self.type)
        self._token = require_str_field(config, "token", self.type)
        # `env` is mandatory: it is both the Sentry `environment` query filter and the
        # `env` label stamped on every emitted signal (model invariant).
        self._env = require_str_field(config, "env", self.type)
        base_url = config.get("base_url")
        self._base_url = (
            base_url.rstrip("/") if isinstance(base_url, str) and base_url else _DEFAULT_BASE_URL
        )
        self._rest = RestClient(client)
        self._sleep = sleep if sleep is not None else time.sleep

    def capabilities(self) -> set[SignalKind]:
        """sentry emits native incidents plus the one derived count gauge metric."""
        return {SignalKind.INCIDENT, SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """GET the project's issues filtered by `environment`, normalize, and derive.

        Returns one `IncidentSignal` per issue followed by a single
        `panoptes_sentry_incident_count` gauge `MetricSignal` (the count of returned
        issues at this sample time). `window` is part of the `Source` Protocol; the
        issues feed reflects currently-open issues for the environment.
        """
        issues = self._get_issues()
        timestamp = datetime.now(UTC)
        normalized = [self._to_incident(issue) for issue in issues]
        signals: list[CanonicalSignal] = list(normalized)
        # Emit one `panoptes_sentry_incident_count` gauge PER DISTINCT level (F6) so the
        # dashboard's `sum by (level)` panel renders a real per-level breakdown. A single
        # `level="all"` aggregate would collapse that panel to one mislabeled series.
        signals.extend(self._derive_per_level_counts(normalized, timestamp))
        return signals

    def health(self) -> SourceHealth:
        """Probe reachability by issuing the same issues GET and reporting the result.

        On failure the detail is a GENERIC transport/auth summary (the exception class
        name), NOT a verbatim `str(exc)` (F4): the surfaced `health().detail` reaches the
        MCP client, and a header-reflecting upstream could otherwise echo the bearer token
        through the exception body. The shared `_format_failure` redaction is the primary
        defense; this keeps `detail` generic as defense-in-depth (the body is never
        surfaced through health at all).
        """
        checked_at = datetime.now(UTC)
        try:
            issues = self._get_issues()
        except PanoptesError as exc:
            return SourceHealth(
                reachable=False,
                detail=f"sentry {self._org}/{self._project} unreachable "
                f"(auth/transport error: {type(exc.__cause__ or exc).__name__})",
                checked_at=checked_at,
            )
        return SourceHealth(
            reachable=True,
            detail=f"sentry {self._org}/{self._project} returned {len(issues)} issue(s)",
            checked_at=checked_at,
        )

    def _issues_url(self) -> str:
        """The Sentry issues endpoint for the configured org/project."""
        return f"{self._base_url}/api/0/projects/{self._org}/{self._project}/issues/"

    def _get_issues(self) -> list[dict[str, object]]:
        """GET the issues feed, honoring a single `Retry-After` on 429.

        On a 429 the `Retry-After` header is read and respected (via the injectable
        `sleep` seam) for exactly one retry; a still-429 or any other non-2xx raises a
        typed `PanoptesError` carrying the upstream body. A 401/404 therefore raises
        rather than returning an empty list.
        """
        endpoint = self._issues_url()
        params = {"environment": self._env}
        headers = {"Authorization": f"Bearer {self._token}"}

        # The Retry-After flow needs to inspect a 429 status BEFORE deciding to retry, so
        # the GETs run on the raw client; the FINAL response is funneled through the shared
        # `RestClient.send` for the raise_for_status + both-branch failure surfacing (a
        # transport error on EITHER GET surfaces as a typed PanoptesError, never raw httpx).
        def _request(http: httpx.Client) -> httpx.Response:
            response = http.get(endpoint, params=params, headers=headers)
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                # Read + respect Retry-After, then retry exactly once.
                self._honor_retry_after(response)
                response = http.get(endpoint, params=params, headers=headers)
            return response

        response = self._rest.send(
            _request, prefix="sentry issues fetch failed", identifier=endpoint
        )
        return self._parse_issues(response.json())

    def _honor_retry_after(self, response: httpx.Response) -> None:
        """Sleep for the `Retry-After` header value (capped), via the injectable seam.

        Sentry sends `Retry-After` as an integer number of seconds. A missing or
        unparseable header falls back to a 1-second courtesy delay; an absurdly large
        value is capped so a hostile header can never stall the cycle unbounded.
        """
        raw = response.headers.get("Retry-After", "1")
        try:
            delay = float(raw)
        except ValueError:
            delay = 1.0
        delay = max(0.0, min(delay, _MAX_RETRY_AFTER_SECONDS))
        self._sleep(delay)

    @staticmethod
    def _parse_issues(payload: object) -> list[dict[str, object]]:
        """The issues endpoint returns a JSON array of issue objects."""
        if not isinstance(payload, list):
            raise PanoptesError(f"sentry issues response was not a JSON array: {payload!r}.")
        issues: list[dict[str, object]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                raise PanoptesError(f"sentry issue entry is not an object: {entry!r}.")
            issues.append(entry)
        return issues

    def _to_incident(self, issue: dict[str, object]) -> IncidentSignal:
        """Normalize one Sentry issue object into an `IncidentSignal`.

        Maps Sentry's `id`/`title`/`level`/`firstSeen`/`lastSeen`/`count` onto the
        canonical incident fields and stamps the configured `env`. The incident's
        labels carry `env`, the issue `level`, and the `project` so a downstream
        consumer can pivot without re-deriving.
        """
        issue_id = self._issue_str(issue, "id")
        title = self._issue_str(issue, "title")
        level = self._map_level(issue.get("level"))
        first_seen = self._parse_timestamp(issue.get("firstSeen"))
        last_seen = self._parse_timestamp(issue.get("lastSeen"))
        count = self._issue_count(issue.get("count"))
        return IncidentSignal(
            id=issue_id,
            title=title,
            level=level,
            first_seen=first_seen,
            last_seen=last_seen,
            count=count,
            labels={"env": self._env, "level": level.value, "project": self._project},
        )

    def _derive_per_level_counts(
        self, incidents: list[IncidentSignal], timestamp: datetime
    ) -> list[MetricSignal]:
        """Derive one `panoptes_sentry_incident_count` gauge PER DISTINCT level (F6).

        Groups the normalized incidents by their `IncidentLevel` and emits one gauge per
        level present, each with the exact label set `{env, level, project}` carrying the
        ACTUAL level value (e.g. `error`, `warning`). This is what makes the dashboard's
        `sum by (level)` breakdown panel render a real per-level series — a single
        `level="all"` aggregate could only ever render one mislabeled `all` series.

        With zero issues no level is present, so no gauge is emitted (an absent series is
        the correct "nothing to count" answer for a per-level breakdown).
        """
        counts_by_level: dict[str, int] = {}
        for incident in incidents:
            level_value = incident.level.value
            counts_by_level[level_value] = counts_by_level.get(level_value, 0) + 1
        # Sorted for a deterministic emission order (stable tests + stable import lines).
        return [
            MetricSignal(
                name=_METRIC_INCIDENT_COUNT,
                value=float(count),
                timestamp=timestamp,
                labels={"env": self._env, "level": level_value, "project": self._project},
            )
            for level_value, count in sorted(counts_by_level.items())
        ]

    @staticmethod
    def _map_level(raw_level: object) -> IncidentLevel:
        """Map a Sentry `level` string onto `IncidentLevel`, defaulting to ERROR."""
        if isinstance(raw_level, str):
            return _SENTRY_LEVEL_TO_INCIDENT.get(raw_level.lower(), IncidentLevel.ERROR)
        return IncidentLevel.ERROR

    @staticmethod
    def _issue_str(issue: dict[str, object], field: str) -> str:
        """Extract a required string field from an issue, raising if absent/non-string."""
        value = issue.get(field)
        if not isinstance(value, str):
            raise PanoptesError(f"sentry issue is missing a string '{field}': {issue!r}.")
        return value

    @staticmethod
    def _issue_count(raw_count: object) -> int:
        """Coerce Sentry's `count` (string or int) into an int, defaulting to 0."""
        if isinstance(raw_count, bool):
            # bool is an int subclass; an explicit guard avoids treating True as 1.
            return 0
        if isinstance(raw_count, int):
            return raw_count
        if isinstance(raw_count, str):
            try:
                return int(raw_count)
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _parse_timestamp(raw: object) -> datetime:
        """Parse a Sentry ISO-8601 timestamp; fall back to epoch-UTC if absent/bad.

        Sentry emits `firstSeen`/`lastSeen` as ISO-8601 (e.g. `2026-01-01T00:00:00Z`).
        A missing or unparseable value collapses to the UTC epoch rather than raising —
        a malformed timestamp on one issue should not abort the whole fetch.
        """
        if isinstance(raw, str):
            try:
                # `fromisoformat` accepts the `+00:00` offset; normalize a trailing `Z`.
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.fromtimestamp(0, tz=UTC)
