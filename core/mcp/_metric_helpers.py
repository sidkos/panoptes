"""Leaf metric-query helpers shared by `QueryContext` and the MCP query tools.

These four helpers (plus the window-parsing internals) were lifted OUT of
`core/mcp/tools_query.py` to break a would-be import cycle: `QueryContext`
(`core/mcp/context.py`) needs the PromQL escape + window/step builders + latest-value
extraction for its `read_gauge`/`read_series` store-readers, but `tools_query.py` already
imports `QueryContext` from `context.py`. Putting the helpers in `tools_query.py` and importing
them into `context.py` would be a cycle; this module sits at the BOTTOM of that graph —
it imports ONLY from `core.model` (and stdlib), so both `context.py` and `tools_query.py`
depend on it without a cycle.

`tools_query.py` RE-EXPORTS `escape_promql_value` (and `_window_for` / `_DEFAULT_WINDOW_MINUTES`)
so the PUBLIC path `from core.mcp.tools_query import escape_promql_value` keeps working for
consumer packs (the demo/fleet/pipeline packs import it) and the existing tools_query tests.

IMPORTANT (FastMCP / PEP-563): like the other `core/mcp/*` modules, this must NOT add
`from __future__ import annotations` — deferred annotations break FastMCP's schema generation
for the tool returns that transitively consume these helpers.
"""

import logging
from datetime import datetime

from core.model import MetricSeries, TimeWindow

_LOGGER = logging.getLogger(__name__)

# Default query window for the live-source fetch tools. The MCP `window` argument is a human
# string (e.g. "15m"); an empty/None/unrecognized window resolves to this trailing default so
# a tool always has a bounded fetch.
_DEFAULT_WINDOW_MINUTES = 15

# The known human window strings → trailing minutes (F2f). A previous `_window_for`
# hard-returned 15m for EVERY input, so `window="24h"` silently gave 15m. These cover the
# common cadences; a bare integer is interpreted as minutes; anything else falls back to the
# default WITH the value surfaced (explicit, never silent).
_WINDOW_STRING_TO_MINUTES: dict[str, int] = {
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "6h": 360,
    "24h": 1440,
    "1d": 1440,
    "7d": 10080,
}

# The step floor (seconds) for a range query (F2f). The step is computed as
# window_seconds / _STEP_TARGET_BUCKETS so a range yields multiple points (never one
# degenerate bucket), then clamped to at least this floor so a tiny window cannot ask the
# store for an absurdly fine grid.
_STEP_TARGET_BUCKETS = 60
_MIN_STEP_SECONDS = 15


def escape_promql_value(value: str) -> str:
    """Escape a value for a double-quoted PromQL label-matcher string (F7 / F2d).

    Backslash is escaped FIRST so the quote-escape's own backslash is not re-doubled, then
    the double quote. This keeps a value like `a"b` a single closed string (`"a\\"b"`)
    instead of breaking out of the selector (cross-env read / corrupted query).

    This is the canonical PromQL value-escape primitive (F2d): it is PUBLIC (re-exported from
    `core.mcp.tools_query`) so every caller that interpolates a value into a quoted PromQL
    string — `query_metric`, `get_dashboard_data`'s `$env` substitution, the `QueryContext`
    store-readers, and the consumer packs — reuses ONE implementation rather than hand-copying
    the two `.replace(...)` calls (a copy that can drift and miss the backslash-first ordering).
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _window_minutes(window: str) -> int:
    """Parse the MCP `window` string into trailing minutes (F2f).

    Recognizes the common cadence strings (`"15m"`, `"30m"`, `"1h"`, `"6h"`, `"24h"`/`"1d"`,
    `"7d"`) and a bare integer (interpreted as minutes). An empty/None window resolves to the
    default. An UNRECOGNIZED window is handled EXPLICITLY: it falls back to the default AND logs
    a warning surfacing the offending value — never the old silent-15m-for-everything behavior
    that made `window="24h"` quietly mean 15m.
    """
    normalized = window.strip().lower() if window else ""
    if not normalized:
        return _DEFAULT_WINDOW_MINUTES
    known = _WINDOW_STRING_TO_MINUTES.get(normalized)
    if known is not None:
        return known
    # A bare integer is accepted as minutes (forward-compatible with a fuller parser).
    if normalized.isdigit():
        parsed = int(normalized)
        if parsed > 0:
            return parsed
    # Unrecognized — fall back to the default, but surface the value so the operator knows
    # their window was not honored (explicit, not silent).
    _LOGGER.warning(
        f"Unrecognized MCP window {window!r}; falling back to the default "
        f"{_DEFAULT_WINDOW_MINUTES}-minute window."
    )
    return _DEFAULT_WINDOW_MINUTES


def _window_for(window: str) -> TimeWindow:
    """Resolve the MCP `window` string to a trailing `TimeWindow` (F2f).

    Delegates to `_window_minutes` (which genuinely PARSES the window rather than ignoring it)
    and returns a trailing window of that many minutes ending 'now'.
    """
    return TimeWindow.last(minutes=_window_minutes(window))


def _step_seconds_for(window: str) -> int:
    """Compute a sane range step (seconds) that is strictly sub-window (F2f).

    The old `query_metric` used `step == window`, producing a single degenerate bucket. This
    divides the window into `_STEP_TARGET_BUCKETS` points, then clamps the step to a
    `_MIN_STEP_SECONDS` floor so a small window cannot request an absurdly fine grid. The
    result is always strictly less than the window span, so a range yields multiple points.
    """
    window_seconds = _window_minutes(window) * 60
    step = window_seconds // _STEP_TARGET_BUCKETS
    return max(_MIN_STEP_SECONDS, step)


def _latest_value(series: list[MetricSeries]) -> float | None:
    """The most recent sample value across a metric's series, or None if there is none.

    A caller surfacing a single scalar per metric picks the latest point across the returned
    series (each series is one label-set; for a single-env `env`-scoped gauge there is
    typically one). Returns None when there is no data, so the caller omits the metric (or
    coerces to 0.0 via `... or 0.0`) rather than inventing a 0.0 here.
    """
    latest_timestamp: datetime | None = None
    latest_value: float | None = None
    for one_series in series:
        for timestamp, value in one_series.points:
            if latest_timestamp is None or timestamp >= latest_timestamp:
                latest_timestamp = timestamp
                latest_value = value
    return latest_value
