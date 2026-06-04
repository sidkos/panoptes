"""The `kubernetes` source — a cluster's API server into derived gauges + incidents.

`fetch` does three read-only list calls against the cluster API and normalizes them
(spec § Data Model — additions / § New source adapter):

- **`list nodes`** → one `panoptes_k8s_node_count` gauge (labels `{env, cluster}`).
- **`list pods`** → `panoptes_k8s_pods_pending` (count of `Pending`-phase pods) +
  `panoptes_k8s_pods_crashloop` (count of `CrashLoopBackOff` containers), both
  `{env, cluster}`, plus one `panoptes_k8s_pod_restarts_total` gauge PER namespace
  (summed container restartCounts, labels `{env, cluster, namespace}`) so a Grafana
  `by (namespace)` panel renders a real per-namespace breakdown.
- **`list events`** → each `Warning`-type event becomes an `IncidentSignal`
  (`id`=event UID, `title`=reason+message, `level`=WARNING, `first_seen`/`last_seen`=
  the event timestamps, `count`=the event count, labels `{env, cluster, namespace,
  kind}`).

Capability set: `{METRIC, INCIDENT}` — no TRACE (the v0.2 TRACE-absence invariant holds).

The `cluster` label distinguishes an OBSERVED consumer cluster from Panoptes' OWN
self-observed cluster (spec § Data Model — both read the same way; the label is what
keeps the two series apart in the store).

**Injectable client seam (the cloudwatch `sts_client`-seam pattern).** The official
kubernetes client is wrapped behind the `_CoreV1ApiClient` Protocol and obtained via the
overridable `api_client` constructor param (default `None`, so the registry's `cls(config)`
still works); a unit test injects a FAKE api object so the normalization is exercised with
no live cluster. The real client is built lazily on first use (in-cluster SA token when
running inside a pod, else the configured kubeconfig context) — so importing this module
never drags the `kubernetes` SDK into the pure config loader or the unit run.

This source is read-only w.r.t. the observed cluster: only `list_node` /
`list_pod_for_all_namespaces` / `list_event_for_all_namespaces` are called — all
`list_*`, none in the no-write guard's mutation-verb set (so no guard change is needed).

`kubernetes` ships no `py.typed` (Risk K5), so it is in the mypy `ignore_missing_imports`
override; the client surface is confined behind the local Protocols below so
`disallow_any_explicit` stays satisfied without any `Any` leaking into the source.
"""

from datetime import UTC, datetime
from typing import Protocol

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
from core.sources.probe import probe_health
from core.validation import require_str_field

# Derived-metric names (spec § Data Model — the `panoptes_` prefix avoids PromQL
# collisions with native upstream metric names, same convention as v0.1).
_METRIC_NODE_COUNT = "panoptes_k8s_node_count"
_METRIC_POD_RESTARTS_TOTAL = "panoptes_k8s_pod_restarts_total"
_METRIC_PODS_PENDING = "panoptes_k8s_pods_pending"
_METRIC_PODS_CRASHLOOP = "panoptes_k8s_pods_crashloop"

# The pod phase + container waiting-reason the gauges key on.
_PHASE_PENDING = "Pending"
_REASON_CRASHLOOP = "CrashLoopBackOff"

# The event `type` that surfaces as an incident (Normal events are routine, not incidents).
_EVENT_TYPE_WARNING = "Warning"


# --- The minimal client surface the source depends on (Risk K5 — Any-free seam) -----
# These Protocols pin EXACTLY the attributes the source reads off the real kubernetes
# V1* objects, so the untyped (`ignore_missing_imports`) SDK surface never leaks `Any`
# into the source. The fakes in the unit test satisfy these structurally.


class _HasItems[ItemT](Protocol):
    """A `V1*List` envelope — only its `.items` attribute is read."""

    items: list[ItemT]


class _CoreV1ApiClient(Protocol):
    """The exact read methods the source calls on the kubernetes `CoreV1Api`.

    Returns are typed as `object`-item lists because the source narrows each item via
    the attribute Protocols below (duck-typed reads) rather than importing the V1* types
    (which carry no stubs). All three are `list_*` reads — read-only by construction.
    """

    def list_node(self) -> _HasItems[object]: ...

    def list_pod_for_all_namespaces(self) -> _HasItems[object]: ...

    def list_event_for_all_namespaces(self) -> _HasItems[object]: ...


@SOURCES.register("kubernetes")
class KubernetesSource:
    """Reads a cluster's nodes/pods/events into derived gauges + Warning-event incidents."""

    type = "kubernetes"

    # An unreachable cluster means the API server / SA token is unusable, so a fetch is
    # pointless and its signals must NOT reach the store — keep the collector's default
    # skip-on-unreachable behavior (F3a, same as cloudwatch/sentry).
    fetch_when_unreachable = False

    def __init__(
        self,
        config: ConfigBlock,
        api_client: _CoreV1ApiClient | None = None,
    ) -> None:
        """Read `env`/`cluster` from config; accept an injectable CoreV1Api seam.

        The `api_client` param defaults to `None`, so the registry's single-positional
        `cls(config)` still constructs the source; a test injects a fake api so the
        normalization runs with no live cluster. A real run leaves it `None` and the
        client is built lazily from the in-cluster SA token / kubeconfig on first use.
        """
        # `env` is mandatory: stamped on every emitted signal (the model invariant).
        self._env = require_str_field(config, "env", self.type)
        # `cluster` is mandatory: it is THE label that distinguishes an observed cluster
        # from Panoptes' own self-observed cluster (spec § Data Model). Without it the two
        # clusters' series would collide in the store.
        self._cluster = require_str_field(config, "cluster", self.type)
        # The injected seam (None in production; a fake api in tests).
        self._api_client = api_client

    def capabilities(self) -> set[SignalKind]:
        """kubernetes emits derived gauges + Warning-event incidents (no TRACE)."""
        return {SignalKind.METRIC, SignalKind.INCIDENT}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        """Read nodes/pods/events from the cluster and normalize all three feeds.

        Returns the cluster-wide gauges (node count, pending, crashloop), the per-namespace
        restart gauges, and one `IncidentSignal` per `Warning` event. `window` is part of
        the Source Protocol; the kubernetes feed reflects CURRENT cluster state (it is not
        sliced by the window, same as the sentry issues feed).
        """
        api = self._api()
        # A single sample timestamp for every gauge this cycle (the read is a point-in-time
        # snapshot of cluster state), taken once so all gauges share one timestamp.
        sampled_at = datetime.now(UTC)
        signals: list[CanonicalSignal] = []
        signals.extend(self._node_gauges(api, sampled_at))
        signals.extend(self._pod_gauges(api, sampled_at))
        signals.extend(self._event_incidents(api))
        return signals

    def health(self) -> SourceHealth:
        """Probe reachability by issuing the same `list_node` read, delegating to the seam.

        The no-raise + no-`str(exc)`-leak discipline lives in
        `core.sources.probe.probe_health`. The kubernetes client surfaces failures as
        `ApiException`, `urllib3` transport errors, or socket errors depending on the failure
        mode; the seam's broad `except Exception` converts ANY of them to `reachable=False`
        with a generic class-name-only detail (never a verbatim `str(exc)` that could echo a
        token/endpoint through the MCP-visible `describe_health` rollup, F3c) rather than
        letting one crash the collector cycle. On success the detail reports the node count.
        """
        return probe_health(
            f"kubernetes cluster '{self._cluster}'",
            lambda: self._api().list_node().items,
            success_detail_factory=lambda nodes: (
                f"kubernetes cluster '{self._cluster}' returned "
                f"{len(nodes) if isinstance(nodes, list) else 0} node(s)"
            ),
        )

    def _base_labels(self) -> dict[str, str]:
        """The {env, cluster} label pair stamped on every cluster-wide gauge."""
        return {"env": self._env, "cluster": self._cluster}

    def _node_gauges(self, api: _CoreV1ApiClient, sampled_at: datetime) -> list[MetricSignal]:
        """Emit the single `panoptes_k8s_node_count` gauge (labels {env, cluster}).

        Emitted even at zero so a dashboard renders `0` rather than `no data` for an
        empty (or freshly-drained) cluster.
        """
        node_count = len(api.list_node().items)
        return [
            MetricSignal(
                name=_METRIC_NODE_COUNT,
                value=float(node_count),
                timestamp=sampled_at,
                labels=self._base_labels(),
            )
        ]

    def _pod_gauges(self, api: _CoreV1ApiClient, sampled_at: datetime) -> list[MetricSignal]:
        """Derive the pending/crashloop cluster gauges + the per-namespace restart gauges.

        One pass over the pod list tallies: pending-phase pods, CrashLoopBackOff
        containers (cluster-wide), and summed container restartCounts grouped by namespace.
        The two cluster-wide gauges emit even at zero; the per-namespace restart gauge emits
        only for namespaces that actually have pods (an absent namespace series is the
        correct "nothing here" answer for a per-namespace breakdown).
        """
        pending_count = 0
        crashloop_count = 0
        restarts_by_namespace: dict[str, int] = {}

        for pod in api.list_pod_for_all_namespaces().items:
            namespace = self._pod_namespace(pod)
            phase = self._pod_phase(pod)
            if phase == _PHASE_PENDING:
                pending_count += 1
            for container in self._container_statuses(pod):
                restarts_by_namespace[namespace] = restarts_by_namespace.get(
                    namespace, 0
                ) + self._restart_count(container)
                if self._is_crashloop(container):
                    crashloop_count += 1

        gauges: list[MetricSignal] = [
            MetricSignal(
                name=_METRIC_PODS_PENDING,
                value=float(pending_count),
                timestamp=sampled_at,
                labels=self._base_labels(),
            ),
            MetricSignal(
                name=_METRIC_PODS_CRASHLOOP,
                value=float(crashloop_count),
                timestamp=sampled_at,
                labels=self._base_labels(),
            ),
        ]
        # One restart gauge per namespace (sorted for deterministic emission order).
        for namespace, total in sorted(restarts_by_namespace.items()):
            gauges.append(
                MetricSignal(
                    name=_METRIC_POD_RESTARTS_TOTAL,
                    value=float(total),
                    timestamp=sampled_at,
                    labels={**self._base_labels(), "namespace": namespace},
                )
            )
        return gauges

    def _event_incidents(self, api: _CoreV1ApiClient) -> list[IncidentSignal]:
        """Map each `Warning`-type event to an `IncidentSignal` (Normal events skipped)."""
        incidents: list[IncidentSignal] = []
        for event in api.list_event_for_all_namespaces().items:
            if self._event_type(event) != _EVENT_TYPE_WARNING:
                continue
            incidents.append(self._to_incident(event))
        return incidents

    def _to_incident(self, event: object) -> IncidentSignal:
        """Normalize one `Warning` event into an `IncidentSignal` (spec field mapping).

        `id`=event UID, `title`=reason + message, `level`=WARNING, `first_seen`/`last_seen`
        =the event timestamps, `count`=the event count, labels={env, cluster, namespace,
        kind}. A missing timestamp collapses to epoch-UTC so one malformed event cannot
        abort the whole fetch (the sentry `_parse_timestamp` discipline).
        """
        uid = self._event_uid(event)
        reason = self._event_reason(event)
        message = self._event_message(event)
        namespace = self._event_namespace(event)
        kind = self._event_kind(event)
        count = self._event_count(event)
        first_seen = self._event_timestamp(event, "first_timestamp")
        last_seen = self._event_timestamp(event, "last_timestamp")
        return IncidentSignal(
            id=uid,
            # title = reason + message (the human-readable "what happened").
            title=f"{reason}: {message}",
            level=IncidentLevel.WARNING,
            first_seen=first_seen,
            last_seen=last_seen,
            count=count,
            labels={
                "env": self._env,
                "cluster": self._cluster,
                "namespace": namespace,
                "kind": kind,
            },
        )

    # --- duck-typed attribute reads off the V1* objects ----------------------------
    # The kubernetes V1* types ship no stubs, so each field is read via a typed helper
    # that narrows the `object` attribute to the expected type with a safe fallback —
    # no `Any`, no crash on a malformed/None field.

    @staticmethod
    def _str_attr(obj: object, name: str, default: str) -> str:
        """Read a string attribute off an object, defaulting if absent/None/non-string."""
        value = getattr(obj, name, None)
        return value if isinstance(value, str) else default

    @staticmethod
    def _int_attr(obj: object, name: str, default: int) -> int:
        """Read an int attribute off an object, defaulting if absent/None/non-int.

        `bool` is an `int` subclass; it is excluded so a stray boolean never counts as 1.
        """
        value = getattr(obj, name, None)
        if isinstance(value, bool):
            return default
        return value if isinstance(value, int) else default

    def _pod_namespace(self, pod: object) -> str:
        metadata = getattr(pod, "metadata", None)
        return self._str_attr(metadata, "namespace", "default")

    def _pod_phase(self, pod: object) -> str:
        status = getattr(pod, "status", None)
        return self._str_attr(status, "phase", "")

    @staticmethod
    def _container_statuses(pod: object) -> list[object]:
        """The pod's container-status list, or empty when absent (pending pods have none)."""
        status = getattr(pod, "status", None)
        container_statuses = getattr(status, "container_statuses", None)
        return list(container_statuses) if isinstance(container_statuses, list) else []

    def _restart_count(self, container: object) -> int:
        return self._int_attr(container, "restart_count", 0)

    def _is_crashloop(self, container: object) -> bool:
        """True when the container's waiting-state reason is `CrashLoopBackOff`."""
        state = getattr(container, "state", None)
        waiting = getattr(state, "waiting", None)
        if waiting is None:
            return False
        return self._str_attr(waiting, "reason", "") == _REASON_CRASHLOOP

    def _event_type(self, event: object) -> str:
        return self._str_attr(event, "type", "")

    def _event_uid(self, event: object) -> str:
        metadata = getattr(event, "metadata", None)
        # A missing UID collapses to empty rather than crashing; the incident is still
        # emitted (the operator sees the warning) — losing the dedup key is preferable to
        # dropping the whole incident.
        return self._str_attr(metadata, "uid", "")

    def _event_namespace(self, event: object) -> str:
        metadata = getattr(event, "metadata", None)
        return self._str_attr(metadata, "namespace", "default")

    def _event_reason(self, event: object) -> str:
        return self._str_attr(event, "reason", "")

    def _event_message(self, event: object) -> str:
        return self._str_attr(event, "message", "")

    def _event_kind(self, event: object) -> str:
        involved = getattr(event, "involved_object", None)
        return self._str_attr(involved, "kind", "")

    def _event_count(self, event: object) -> int:
        # A k8s event without a `count` defaults to 1 occurrence.
        return self._int_attr(event, "count", 1)

    @staticmethod
    def _event_timestamp(event: object, name: str) -> datetime:
        """Read a `first_timestamp`/`last_timestamp` aware datetime, defaulting to epoch.

        The kubernetes client returns aware `datetime`s for event timestamps; a missing or
        non-datetime value collapses to epoch-UTC so one malformed event cannot abort the
        fetch (the sentry timestamp discipline).
        """
        value = getattr(event, name, None)
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return datetime.fromtimestamp(0, tz=UTC)

    def _api(self) -> _CoreV1ApiClient:
        """Return the injected api client, or lazily build the real CoreV1Api.

        The real path loads the in-cluster SA token when running inside a pod, else the
        configured kubeconfig context, then constructs a `CoreV1Api`. Imported lazily so
        the heavy `kubernetes` SDK stays out of this module's static import graph (the unit
        run + the pure config loader never import it).
        """
        if self._api_client is not None:
            return self._api_client
        self._api_client = _build_real_api_client()
        return self._api_client


def _build_real_api_client() -> _CoreV1ApiClient:
    """Build a real kubernetes `CoreV1Api`, preferring the in-cluster SA token.

    Loads the in-cluster config (the pod's bound ServiceAccount token, IAM.md §A — RBAC,
    not IAM) when running inside a cluster; falls back to the local kubeconfig context for
    development / an observed cluster reached via a per-env kubeconfig. The import is local
    so the SDK is never pulled into the unit run.
    """
    # Local import: keeps the kubernetes SDK out of the module's static import graph so the
    # pure config loader + unit tests (which inject a fake api) never import it. The SDK is
    # in the mypy `ignore_missing_imports` override (Risk K5 — it ships no py.typed).
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        # `load_incluster_config` raises when the in-cluster SA files are absent (i.e. when
        # running outside a pod); fall back to the developer/observed-cluster kubeconfig.
        # Catch broadly: the absence is signalled by a ConfigException, but the fallback is
        # correct for any in-cluster-load failure mode.
        config.load_kube_config()
    built: _CoreV1ApiClient = client.CoreV1Api()
    return built
