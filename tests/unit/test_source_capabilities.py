"""The TRACE-absence invariant across all core sources (spec § Data Model — re-asserted).

Asserts the **union** of `capabilities()` across every CORE source is **exactly**
`{METRIC, LOG, INCIDENT}` — TRACE is provided by **no** core source. This is the
precondition that makes the "no trace source" negative paths (MCP + end-to-end) true;
a single targeted guard catches a future adapter accidentally adding TRACE to a
capability set rather than letting the no-trace tests start failing confusingly.

v0.2 carries the invariant forward: the `kubernetes` source adds `{METRIC, INCIDENT}`,
so the union is UNCHANGED at `{METRIC, LOG, INCIDENT}` — no TRACE (`loki`/`tempo` are
v0.3). The sources are constructed directly with minimal config blocks (no network is
touched — only `.capabilities()` is called, a pure method).
"""

from core.model import SignalKind
from core.planes.source import Source
from core.registry import SOURCES
from core.sources.cloudwatch import CloudWatchSource
from core.sources.http_health import HttpHealthSource
from core.sources.kubernetes import KubernetesSource
from core.sources.sentry import SentrySource


def _all_core_sources() -> list[Source]:
    """Construct each core source with a minimal, network-free config block."""
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
    # The v0.2 kubernetes source — adds {METRIC, INCIDENT}, no TRACE (no live API touched,
    # only `.capabilities()` is read; the injectable api seam stays unused here).
    kubernetes = KubernetesSource({"env": "dev", "cluster": "observed-dev"})
    return [http_health, sentry, cloudwatch, kubernetes]


def test_core_sources_are_all_registered() -> None:
    # Importing the modules above triggers their `@SOURCES.register(...)`.
    assert {"cloudwatch", "sentry", "http-health", "kubernetes"} <= set(SOURCES.available())


def test_capability_union_is_exactly_metric_log_incident_no_trace() -> None:
    union: set[SignalKind] = set()
    for source in _all_core_sources():
        union |= source.capabilities()

    # v0.2 re-assertion: kubernetes adds {METRIC, INCIDENT}, so the union is unchanged.
    assert union == {SignalKind.METRIC, SignalKind.LOG, SignalKind.INCIDENT}
    # The load-bearing assertion: no core source provides TRACE (loki/tempo are v0.3).
    assert SignalKind.TRACE not in union
