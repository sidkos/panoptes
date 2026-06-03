"""Unit tests for the `kubernetes` source — fake K8s API, no live cluster (Risk K5).

The source reads a cluster's API server (nodes / pods / events) and normalizes it into
the four derived `panoptes_k8s_*` gauges + `Warning`-event `IncidentSignal`s, exactly
the "two faces, one store" pattern the v0.1 sentry/cloudwatch sources prove. The official
kubernetes client is wrapped behind an INJECTABLE seam (constructor `api_client` param,
the cloudwatch `sts_client`-seam pattern) so every test here drives a hand-built FAKE api
object — no network, no cluster, no real `kubernetes` import at test time.

Covers (spec § Data Model — additions / § New source adapter):
- the four gauges with their EXACT label sets (incl. the `cluster` label that
  distinguishes an observed cluster from Panoptes' own);
- `Warning` events → `IncidentSignal` with the exact field mapping;
- `env` stamped on every emitted signal (the model invariant);
- `capabilities() == {METRIC, INCIDENT}` (no TRACE);
- an unreachable API → `health()` reports unreachable, raising NOTHING.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from core.model import (
    CanonicalSignal,
    IncidentLevel,
    IncidentSignal,
    MetricSignal,
    SignalKind,
    TimeWindow,
)
from core.registry import SOURCES
from core.sources.kubernetes import KubernetesSource

# A fixed window — the kubernetes feed reflects current cluster state, so the window is
# part of the Source Protocol but does not slice the read; any window works.
_WINDOW = TimeWindow(
    start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
)


# --- Minimal fakes mirroring the kubernetes client object surface ------------------
# Each fake mirrors only the attributes the source reads off the real V1* objects, so
# the source's normalization is exercised without importing the kubernetes client.


class _FakeContainerState:
    """Mirrors `V1ContainerState` — only the `waiting.reason` path the source reads."""

    def __init__(self, waiting_reason: str | None) -> None:
        # `waiting` is a small object carrying `.reason`; None when the container is running.
        self.waiting = _FakeWaiting(waiting_reason) if waiting_reason is not None else None


class _FakeWaiting:
    def __init__(self, reason: str) -> None:
        self.reason = reason


class _FakeContainerStatus:
    """Mirrors `V1ContainerStatus` — restart count + the waiting-state reason."""

    def __init__(self, restart_count: int, waiting_reason: str | None = None) -> None:
        self.restart_count = restart_count
        self.state = _FakeContainerState(waiting_reason)


class _FakePodStatus:
    def __init__(self, phase: str, container_statuses: list[_FakeContainerStatus] | None) -> None:
        self.phase = phase
        self.container_statuses = container_statuses


class _FakeMetadata:
    def __init__(self, namespace: str | None = None, uid: str | None = None) -> None:
        self.namespace = namespace
        self.uid = uid


class _FakePod:
    def __init__(
        self,
        namespace: str,
        phase: str,
        container_statuses: list[_FakeContainerStatus] | None,
    ) -> None:
        self.metadata = _FakeMetadata(namespace=namespace)
        self.status = _FakePodStatus(phase, container_statuses)


class _FakeInvolvedObject:
    def __init__(self, kind: str) -> None:
        self.kind = kind


class _FakeEvent:
    """Mirrors `CoreV1Event` — the Warning-event fields the source maps to an incident."""

    def __init__(
        self,
        uid: str,
        event_type: str,
        reason: str,
        message: str,
        namespace: str,
        kind: str,
        count: int,
        first_timestamp: datetime,
        last_timestamp: datetime,
    ) -> None:
        self.metadata = _FakeMetadata(namespace=namespace, uid=uid)
        self.type = event_type
        self.reason = reason
        self.message = message
        self.count = count
        self.first_timestamp = first_timestamp
        self.last_timestamp = last_timestamp
        self.involved_object = _FakeInvolvedObject(kind)


class _FakeList[ItemT]:
    """Mirrors the `V1*List` envelope — only its `.items` attribute is read."""

    def __init__(self, items: Sequence[ItemT]) -> None:
        self.items = list(items)


class _FakeCoreV1Api:
    """A fake CoreV1Api: returns the configured node/pod/event lists, no network.

    Each list method mirrors the real client's name + return-envelope shape (`.items`).
    Setting `raise_on` makes every read raise the supplied exception, simulating an
    unreachable API server for the `health()` test.
    """

    def __init__(
        self,
        *,
        nodes: Sequence[object] = (),
        pods: Sequence[object] = (),
        events: Sequence[object] = (),
        raise_on: Exception | None = None,
    ) -> None:
        self._nodes = nodes
        self._pods = pods
        self._events = events
        self._raise_on = raise_on

    def list_node(self) -> _FakeList[object]:
        if self._raise_on is not None:
            raise self._raise_on
        return _FakeList(self._nodes)

    def list_pod_for_all_namespaces(self) -> _FakeList[object]:
        if self._raise_on is not None:
            raise self._raise_on
        return _FakeList(self._pods)

    def list_event_for_all_namespaces(self) -> _FakeList[object]:
        if self._raise_on is not None:
            raise self._raise_on
        return _FakeList(self._events)


def _running_container(restart_count: int) -> _FakeContainerStatus:
    return _FakeContainerStatus(restart_count=restart_count, waiting_reason=None)


def _crashloop_container(restart_count: int) -> _FakeContainerStatus:
    return _FakeContainerStatus(restart_count=restart_count, waiting_reason="CrashLoopBackOff")


def _source(
    api: _FakeCoreV1Api, *, env: str = "dev", cluster: str = "observed-dev"
) -> KubernetesSource:
    """Build a KubernetesSource bound to the fake api with the required config fields."""
    return KubernetesSource({"env": env, "cluster": cluster}, api_client=api)


def _metrics_by_name(signals: Sequence[CanonicalSignal]) -> dict[str, list[MetricSignal]]:
    # `Sequence` (covariant) so a `list[CanonicalSignal]` from `fetch()` is accepted;
    # `list` is invariant and would reject the union element type.
    by_name: dict[str, list[MetricSignal]] = {}
    for signal in signals:
        if isinstance(signal, MetricSignal):
            by_name.setdefault(signal.name, []).append(signal)
    return by_name


# --- capabilities ------------------------------------------------------------------


def test_capabilities_is_exactly_metric_and_incident_no_trace() -> None:
    """The kubernetes source advertises exactly {METRIC, INCIDENT} — never TRACE."""
    source = _source(_FakeCoreV1Api())
    assert source.capabilities() == {SignalKind.METRIC, SignalKind.INCIDENT}
    assert SignalKind.TRACE not in source.capabilities()


def test_source_is_registered() -> None:
    """Importing the module self-registers the `kubernetes` adapter on SOURCES."""
    assert "kubernetes" in SOURCES.available()


# --- node_count gauge --------------------------------------------------------------


def test_node_count_gauge_has_exact_labels_env_cluster() -> None:
    """`panoptes_k8s_node_count` is the node tally with exactly the {env, cluster} labels."""
    api = _FakeCoreV1Api(nodes=[object(), object(), object()])
    signals = _source(api, env="dev", cluster="observed-dev").fetch(_WINDOW)
    node_gauges = _metrics_by_name(signals)["panoptes_k8s_node_count"]
    assert len(node_gauges) == 1
    gauge = node_gauges[0]
    assert gauge.value == 3.0
    # EXACT label set — no namespace on the cluster-wide node count.
    assert gauge.labels == {"env": "dev", "cluster": "observed-dev"}


# --- pods_pending gauge ------------------------------------------------------------


def test_pods_pending_gauge_counts_pending_phase_pods() -> None:
    """`panoptes_k8s_pods_pending` counts `Pending`-phase pods, labels {env, cluster}."""
    pods = [
        _FakePod("default", "Pending", None),
        _FakePod("default", "Running", [_running_container(0)]),
        _FakePod("kube-system", "Pending", None),
    ]
    api = _FakeCoreV1Api(pods=pods)
    signals = _source(api, env="prod", cluster="own").fetch(_WINDOW)
    pending = _metrics_by_name(signals)["panoptes_k8s_pods_pending"]
    assert len(pending) == 1
    assert pending[0].value == 2.0
    assert pending[0].labels == {"env": "prod", "cluster": "own"}


# --- pods_crashloop gauge ----------------------------------------------------------


def test_pods_crashloop_gauge_counts_crashloopbackoff_containers() -> None:
    """`panoptes_k8s_pods_crashloop` counts CrashLoopBackOff containers, {env, cluster}."""
    pods = [
        # One pod with a crashlooping container.
        _FakePod("app", "Running", [_crashloop_container(5)]),
        # A healthy pod contributes nothing.
        _FakePod("app", "Running", [_running_container(0)]),
        # A pod with TWO crashlooping containers contributes two.
        _FakePod("app", "Running", [_crashloop_container(3), _crashloop_container(1)]),
    ]
    api = _FakeCoreV1Api(pods=pods)
    signals = _source(api, env="dev", cluster="c1").fetch(_WINDOW)
    crashloop = _metrics_by_name(signals)["panoptes_k8s_pods_crashloop"]
    assert len(crashloop) == 1
    assert crashloop[0].value == 3.0
    assert crashloop[0].labels == {"env": "dev", "cluster": "c1"}


# --- pod_restarts_total gauge (per namespace) --------------------------------------


def test_pod_restarts_total_gauge_is_per_namespace_with_namespace_label() -> None:
    """`panoptes_k8s_pod_restarts_total` sums restartCounts PER namespace.

    Exact label set {env, cluster, namespace} — one gauge per namespace present, so a
    Grafana `by (namespace)` panel renders a real per-namespace breakdown.
    """
    pods = [
        _FakePod("ns-a", "Running", [_running_container(2), _running_container(3)]),
        _FakePod("ns-a", "Running", [_running_container(1)]),
        _FakePod("ns-b", "Running", [_crashloop_container(10)]),
    ]
    api = _FakeCoreV1Api(pods=pods)
    signals = _source(api, env="dev", cluster="c1").fetch(_WINDOW)
    restarts = _metrics_by_name(signals)["panoptes_k8s_pod_restarts_total"]
    by_namespace = {gauge.labels["namespace"]: gauge for gauge in restarts}
    assert set(by_namespace) == {"ns-a", "ns-b"}
    assert by_namespace["ns-a"].value == 6.0  # 2 + 3 + 1
    assert by_namespace["ns-b"].value == 10.0
    # EXACT label set carries env, cluster, AND namespace.
    assert by_namespace["ns-a"].labels == {"env": "dev", "cluster": "c1", "namespace": "ns-a"}


# --- Warning events → IncidentSignal -----------------------------------------------


def test_warning_event_becomes_incident_with_exact_field_mapping() -> None:
    """A `Warning` event normalizes to an `IncidentSignal` with the spec's field mapping."""
    first = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    last = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
    event = _FakeEvent(
        uid="evt-uid-1",
        event_type="Warning",
        reason="BackOff",
        message="Back-off restarting failed container",
        namespace="app",
        kind="Pod",
        count=7,
        first_timestamp=first,
        last_timestamp=last,
    )
    api = _FakeCoreV1Api(events=[event])
    signals = _source(api, env="dev", cluster="c1").fetch(_WINDOW)
    incidents = [s for s in signals if isinstance(s, IncidentSignal)]
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.id == "evt-uid-1"
    # title = reason + message.
    assert "BackOff" in incident.title
    assert "Back-off restarting failed container" in incident.title
    assert incident.level is IncidentLevel.WARNING
    assert incident.first_seen == first
    assert incident.last_seen == last
    assert incident.count == 7
    # EXACT incident label set: {env, cluster, namespace, kind}.
    assert incident.labels == {
        "env": "dev",
        "cluster": "c1",
        "namespace": "app",
        "kind": "Pod",
    }


def test_non_warning_events_are_not_incidents() -> None:
    """`Normal`-type events are not incidents (only `Warning` events surface)."""
    normal = _FakeEvent(
        uid="evt-normal",
        event_type="Normal",
        reason="Scheduled",
        message="Successfully assigned",
        namespace="app",
        kind="Pod",
        count=1,
        first_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        last_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    api = _FakeCoreV1Api(events=[normal])
    signals = _source(api, env="dev", cluster="c1").fetch(_WINDOW)
    assert [s for s in signals if isinstance(s, IncidentSignal)] == []


# --- env stamped on EVERY signal ---------------------------------------------------


def test_env_is_stamped_on_every_emitted_signal() -> None:
    """The model invariant: every emitted signal carries the configured `env` label."""
    pods = [_FakePod("ns", "Pending", [_crashloop_container(1)])]
    event = _FakeEvent(
        uid="e1",
        event_type="Warning",
        reason="R",
        message="M",
        namespace="ns",
        kind="Pod",
        count=1,
        first_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        last_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    api = _FakeCoreV1Api(nodes=[object()], pods=pods, events=[event])
    signals = _source(api, env="staging", cluster="c1").fetch(_WINDOW)
    assert signals, "the fetch produced at least one signal"
    for signal in signals:
        assert signal.labels.get("env") == "staging"


# --- cluster label distinguishes observed vs own -----------------------------------


def test_cluster_label_distinguishes_observed_from_own() -> None:
    """The `cluster` label carries the configured cluster name (observed vs Panoptes' own)."""
    api = _FakeCoreV1Api(nodes=[object()])
    own = _source(api, env="dev", cluster="panoptes-self").fetch(_WINDOW)
    observed = _source(api, env="dev", cluster="consumer-prod").fetch(_WINDOW)
    own_gauge = _metrics_by_name(own)["panoptes_k8s_node_count"][0]
    observed_gauge = _metrics_by_name(observed)["panoptes_k8s_node_count"][0]
    assert own_gauge.labels["cluster"] == "panoptes-self"
    assert observed_gauge.labels["cluster"] == "consumer-prod"


# --- health() reports unreachable without raising ----------------------------------


def test_health_unreachable_when_api_down_does_not_raise() -> None:
    """An unreachable API server → `health()` reports `reachable=False`, raising nothing."""
    api = _FakeCoreV1Api(raise_on=RuntimeError("connection refused"))
    source = _source(api, env="dev", cluster="c1")
    health = source.health()
    assert health.reachable is False
    # The detail names the source/cluster but not a verbatim leaked message.
    assert "kubernetes" in health.detail.lower()


def test_health_reachable_when_api_responds() -> None:
    """A responsive API server → `health()` reports `reachable=True`."""
    api = _FakeCoreV1Api(nodes=[object(), object()])
    health = _source(api, env="dev", cluster="c1").health()
    assert health.reachable is True


# --- empty-cluster zero gauges -----------------------------------------------------


def test_empty_cluster_emits_zero_node_pending_crashloop_gauges() -> None:
    """An empty cluster still emits the cluster-wide gauges at 0 (never silent-absent).

    node_count / pods_pending / pods_crashloop are cluster-wide, so they emit even at
    zero so a dashboard shows `0` rather than `no data`. pod_restarts_total is
    per-namespace, so with no pods there is genuinely no namespace to emit for.
    """
    signals = _source(_FakeCoreV1Api(), env="dev", cluster="c1").fetch(_WINDOW)
    by_name = _metrics_by_name(signals)
    assert by_name["panoptes_k8s_node_count"][0].value == 0.0
    assert by_name["panoptes_k8s_pods_pending"][0].value == 0.0
    assert by_name["panoptes_k8s_pods_crashloop"][0].value == 0.0
    # No pods → no per-namespace restart gauge.
    assert "panoptes_k8s_pod_restarts_total" not in by_name
