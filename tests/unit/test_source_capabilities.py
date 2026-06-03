"""The TRACE-absence invariant across all v0.1-registered sources (spec Sources bullet).

Asserts the **union** of `capabilities()` across the three v0.1 sources is **exactly**
`{METRIC, LOG, INCIDENT}` — TRACE is provided by **no** v0.1 source. This is the
precondition that makes the "no trace source" negative paths (MCP + end-to-end) true;
a single targeted guard catches a future adapter accidentally adding TRACE to a
capability set rather than letting the no-trace tests start failing confusingly.

The three sources are constructed directly with minimal config blocks (no network is
touched — only `.capabilities()` is called, a pure method).
"""

from core.model import SignalKind
from core.planes.source import Source
from core.registry import SOURCES
from core.sources.cloudwatch import CloudWatchSource
from core.sources.http_health import HttpHealthSource
from core.sources.sentry import SentrySource


def _all_v01_sources() -> list[Source]:
    """Construct each v0.1 source with a minimal, network-free config block."""
    http_health = HttpHealthSource({"url": "http://app/health", "env": "dev"})
    sentry = SentrySource({"org": "acme", "project": "backend", "token": "tok", "env": "dev"})
    cloudwatch = CloudWatchSource(
        {
            "region": "us-east-1",
            "namespace": "AWS/ELB",
            "metric_names": ["RequestCount"],
            "log_groups": ["/app/api"],
            "env": "dev",
        }
    )
    return [http_health, sentry, cloudwatch]


def test_v01_sources_are_all_registered() -> None:
    # Importing the three modules above triggers their `@SOURCES.register(...)`.
    assert {"cloudwatch", "sentry", "http-health"} <= set(SOURCES.available())


def test_capability_union_is_exactly_metric_log_incident_no_trace() -> None:
    union: set[SignalKind] = set()
    for source in _all_v01_sources():
        union |= source.capabilities()

    assert union == {SignalKind.METRIC, SignalKind.LOG, SignalKind.INCIDENT}
    # The load-bearing assertion: no v0.1 source provides TRACE.
    assert SignalKind.TRACE not in union
