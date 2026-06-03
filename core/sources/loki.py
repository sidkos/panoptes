"""The `loki` core log source — read-only LogQL scrape into `LogSignal`s.

`fetch` GETs `<url>/loki/api/v1/query_range` (window-aware) for each configured LogQL
`query` and normalizes the Loki `streams` envelope into one `LogSignal` per line, stamping
the mandatory `env`. It is the SECOND core log producer alongside cloudwatch-logs (spec
§ Data Model — Loki log lines → `LogSignal`).

Capability set: `{LOG}` — loki is a log-only source. With loki (LOG) added and `tempo`
DEFERRED, the union of core-source capabilities is STILL exactly `{METRIC, LOG, INCIDENT}` —
no TRACE (the TRACE-absence invariant, re-asserted in test_source_capabilities).

The Loki envelope:

    {"status": "success",
     "data": {"resultType": "streams",
              "result": [{"stream": {<labels>}, "values": [["<ns-ts>", "<log line>"], ...]}]}}

Each `[ns-timestamp, log-line]` pair becomes a `LogSignal`: the line is the `message`, the
nanosecond-precision string timestamp is parsed to UTC, and the level is resolved from the
stream's STRUCTURED level label (`level` / `detected_level` — Loki's structured/auto-detected
level) FIRST, falling back to scanning the line text (the cloudwatch severity-marker scan),
defaulting to INFO. The stream labels are carried (the consumed level label dropped), with
`env` ALWAYS the source's authoritative value (never an upstream `env` label silently
overwriting it — the model invariant).

A non-`success` envelope status (a Loki query error returned with HTTP 200) yields no signals
for that query rather than crashing the cycle.

**Read-only (documented known-miss).** The source issues only httpx `GET`s. The no-write
guard is boto3-shaped and does not see httpx GETs — the same documented read-only known-miss
as the v0.1 sentry/http-health + the prometheus source. The authoritative control is the
read-only endpoint/token, not the boto3 grep; no guard change is needed.

The transport plumbing (the injectable `httpx.Client` seam, `raise_for_status`, and the
body-surfacing failure formatting) lives once in `core.rest`; this source delegates its
query GETs to `RestClient.get_json` (so an HTTP 4xx/5xx surfaces the upstream response BODY
via `_format_failure`) and keeps only its URL building + envelope normalization. httpx is
mocked in tests with `respx`; the client is still threaded as a constructor seam.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

import httpx

from core.model import (
    CanonicalSignal,
    LogLevel,
    LogSignal,
    SignalKind,
    SourceHealth,
    TimeWindow,
)
from core.registry import SOURCES, ConfigBlock
from core.rest import RestClient
from core.sources.probe import probe_health
from core.validation import require_str_field, require_str_list_field

# The mandatory env label every signal carries; the source's value is authoritative.
_ENV_LABEL = "env"

# Loki stream labels that carry a STRUCTURED log level (consumed as the level, not a label).
# `level` is the explicit label; `detected_level` is Loki's auto-detected level (v2.9+).
_LEVEL_LABELS = ("level", "detected_level")

# Map a Loki structured level STRING onto `LogLevel`. Loki emits `debug`/`info`/`warn`/
# `error`/`critical` (and `warning`/`fatal` variants); anything unrecognized falls through
# to the line-text scan.
_STRUCTURED_LEVEL_MAP: dict[str, LogLevel] = {
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "warn": LogLevel.WARNING,
    "warning": LogLevel.WARNING,
    "error": LogLevel.ERROR,
    "err": LogLevel.ERROR,
    "critical": LogLevel.CRITICAL,
    "crit": LogLevel.CRITICAL,
    "fatal": LogLevel.CRITICAL,
}

# Message-substring markers → their `LogLevel`, ordered MOST-SEVERE FIRST so a line carrying
# multiple markers classifies at its highest severity (mirrors the cloudwatch source's
# `_LEVEL_MARKERS`). Used only when no structured level label is present.
_LINE_MARKERS: tuple[tuple[tuple[str, ...], LogLevel], ...] = (
    (("CRITICAL", "FATAL"), LogLevel.CRITICAL),
    (("ERROR", "EXCEPTION", "TRACEBACK"), LogLevel.ERROR),
    (("WARNING", "WARN"), LogLevel.WARNING),
    (("DEBUG",), LogLevel.DEBUG),
)


@SOURCES.register("loki")
class LokiSource:
    """Reads LogQL query results from a Loki endpoint into `LogSignal`s."""

    type = "loki"

    # An unreachable loki source means the endpoint/transport is unusable, so a fetch is
    # pointless and its signals must NOT reach the store — keep the collector's default
    # skip-on-unreachable behavior (F3a, same as prometheus/sentry/cloudwatch).
    fetch_when_unreachable = False

    def __init__(self, config: ConfigBlock, client: httpx.Client | None = None) -> None:
        """Read `url`/`queries`/`env` from config; accept an injectable httpx client seam.

        The `client` seam mirrors the prometheus/sentry pattern: under `respx` the default
        `httpx.Client()` is intercepted globally, so production passes none and tests need
        not inject one — the seam exists for explicit control.
        """
        # The Loki base URL (e.g. `http://loki:3100`); a trailing slash is stripped so the
        # endpoint paths join cleanly.
        self._url = require_str_field(config, "url", self.type).rstrip("/")
        # The LogQL queries to scrape — each is fetched over the window and normalized.
        self._queries = require_str_list_field(config, "queries", self.type)
        # `env` is mandatory: stamped on every emitted signal (the model invariant).
        self._env = require_str_field(config, "env", self.type)
        self._rest = RestClient(client)

    def capabilities(self) -> set[SignalKind]:
        """loki emits log signals only (no metrics/incidents/traces)."""
        return {SignalKind.LOG}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Scrape each configured query over `window` and normalize every log line.

        For each query a single `query_range` GET is issued over the window; the returned
        streams are normalized into `LogSignal`s. An HTTP 4xx/5xx surfaces the upstream body
        via the shared `RestClient.get_json`; a query whose envelope status is not `success`
        (a Loki-side query error returned with HTTP 200) contributes no signals rather than
        aborting the whole fetch.
        """
        signals: list[CanonicalSignal] = []
        for query in self._queries:
            payload = self._scrape(query, window)
            signals.extend(self._normalize(payload))
        return signals

    def health(self) -> SourceHealth:
        """Probe reachability via a cheap `GET /ready`, delegating to the probe seam.

        Loki exposes `/ready` as a trivial readiness endpoint (plain text "ready"). The
        no-raise + no-`str(exc)`-leak discipline lives in `core.sources.probe.probe_health`:
        any failure becomes `reachable=False` with a generic class-name-only detail (never a
        verbatim `str(exc)` that could echo a token/endpoint through the MCP-visible
        `describe_health` rollup), so this method is just the label + the probe lambda.
        """

        # `/ready` returns plain text ("ready"), not JSON — so use the raw `send`
        # (raise_for_status + body-surfacing) rather than `get_json` (which would try to
        # `.json()`-decode the text body and fail spuriously).
        def _probe() -> object:
            return self._rest.send(
                lambda http: http.get(f"{self._url}/ready"),
                prefix="loki health probe failed",
                identifier=self._url,
            )

        return probe_health(
            "loki endpoint",
            _probe,
            success_detail_factory=lambda _result: f"loki endpoint reachable ({self._url})",
        )

    def _scrape(self, query: str, window: TimeWindow) -> object:
        """GET `query_range` for one LogQL query over the window, returning the decoded JSON.

        Loki's `query_range` takes the window bounds as NANOSECOND unix timestamps. Delegates
        to `RestClient.get_json`, so a 4xx/5xx raises a `PanoptesError` carrying the surfaced
        upstream body (the slack-notifier/sentry body-surfacing discipline).
        """
        params = {
            "query": query,
            # Loki expects nanosecond unix timestamps for the range bounds.
            "start": str(int(window.start.timestamp() * 1_000_000_000)),
            "end": str(int(window.end.timestamp() * 1_000_000_000)),
        }
        return self._rest.get_json(
            f"{self._url}/loki/api/v1/query_range",
            prefix="loki query failed",
            identifier=query,
            params=params,
        )

    def _normalize(self, payload: object) -> list[LogSignal]:
        """Normalize a Loki `streams` envelope into `LogSignal`s.

        Returns no signals when the envelope status is not `success` (a Loki-side query error
        returned with HTTP 200) — the cycle continues with the next query.
        """
        if not isinstance(payload, dict):
            return []
        if payload.get("status") != "success":
            # A Loki query error (e.g. bad LogQL) returned with HTTP 200 — no log lines.
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        results = data.get("result")
        if not isinstance(results, list):
            return []
        signals: list[LogSignal] = []
        for stream in results:
            signals.extend(self._normalize_stream(stream))
        return signals

    def _normalize_stream(self, stream: object) -> list[LogSignal]:
        """Normalize one stream (a `result` entry) into one `LogSignal` per line."""
        if not isinstance(stream, dict):
            return []
        raw_labels = stream.get("stream")
        stream_labels = raw_labels if isinstance(raw_labels, dict) else {}
        # The structured level (from a `level`/`detected_level` stream label) is resolved
        # once per stream; a line with no structured level falls back to its own text scan.
        structured_level = self._structured_level(stream_labels)
        signal_labels = self._build_labels(stream_labels)
        values = stream.get("values")
        if not isinstance(values, list):
            return []
        signals: list[LogSignal] = []
        for pair in values:
            parsed = self._parse_line(pair, structured_level)
            if parsed is not None:
                timestamp, message, level = parsed
                signals.append(
                    LogSignal(
                        timestamp=timestamp,
                        message=message,
                        level=level,
                        labels=dict(signal_labels),
                    )
                )
        return signals

    def _parse_line(
        self, pair: object, structured_level: LogLevel | None
    ) -> tuple[datetime, str, LogLevel] | None:
        """Parse one `[ns-timestamp, log-line]` pair into `(timestamp, message, level)`.

        The level is the stream's STRUCTURED level when present, else scanned from the line
        text, else INFO. A malformed pair (missing ts/line) is skipped rather than aborting
        the stream.
        """
        if not isinstance(pair, list | tuple) or len(pair) < 2:
            return None
        raw_timestamp, raw_message = pair[0], pair[1]
        if not isinstance(raw_message, str):
            return None
        timestamp = self._parse_ns_timestamp(raw_timestamp)
        if timestamp is None:
            return None
        level = (
            structured_level if structured_level is not None else self._scan_line_level(raw_message)
        )
        return timestamp, raw_message, level

    @staticmethod
    def _parse_ns_timestamp(raw_timestamp: object) -> datetime | None:
        """Parse a Loki nanosecond-precision unix timestamp (a string) into UTC.

        Loki serializes the timestamp as a STRING of nanoseconds. A malformed value is
        skipped (returns None) rather than crashing.
        """
        if not isinstance(raw_timestamp, str | int):
            return None
        try:
            nanos = int(raw_timestamp)
        except ValueError:
            return None
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)

    @staticmethod
    def _structured_level(labels: Mapping[str, object]) -> LogLevel | None:
        """The structured log level from a `level`/`detected_level` stream label, if present.

        Returns the mapped `LogLevel` for a recognized structured value, else `None` (the
        caller then scans the line text). Tries `level` first, then `detected_level`.
        """
        for label_name in _LEVEL_LABELS:
            value = labels.get(label_name)
            if isinstance(value, str):
                mapped = _STRUCTURED_LEVEL_MAP.get(value.strip().lower())
                if mapped is not None:
                    return mapped
        return None

    @staticmethod
    def _scan_line_level(message: str) -> LogLevel:
        """Classify a log line's level from its severity marker, defaulting to INFO.

        Used only when the stream carries no structured level label. Matches the line text
        (case-insensitive) against `_LINE_MARKERS`, ordered most-severe-first so a line with
        multiple markers classifies at its highest severity (the cloudwatch discipline).
        """
        upper = message.upper()
        for markers, level in _LINE_MARKERS:
            if any(marker in upper for marker in markers):
                return level
        return LogLevel.INFO

    def _build_labels(self, labels: Mapping[str, object]) -> dict[str, str]:
        """Build the signal's labels: the stream labels (level labels dropped) + the stamped env.

        The `level`/`detected_level` labels are CONSUMED as the level (not carried as labels).
        The source's `env` is AUTHORITATIVE — it is set LAST, so an upstream stream carrying
        its own `env` label can never silently overwrite it (the model invariant).
        """
        built: dict[str, str] = {}
        for key, value in labels.items():
            if key in _LEVEL_LABELS:
                # The structured level label became the LEVEL, not a carried label.
                continue
            if isinstance(value, str):
                built[key] = value
        # env stamped LAST so it wins over any clashing upstream `env` label.
        built[_ENV_LABEL] = self._env
        return built
