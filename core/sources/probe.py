"""The concentrated Source health-probe seam — one deep place for the probe discipline.

Every Source's `health()` reachability check shares the SAME discipline: run a cheap
read-only probe, convert ANY failure to `reachable=False` WITHOUT raising (so one source's
outage never crashes the collector cycle), and surface ONLY a generic failure summary —
never the raw exception message, which can carry a role ARN / bearer token / endpoint into
the MCP-visible `describe_health` rollup. This module OWNS that discipline so the five
covered sources (`prometheus`, `loki`, `sentry`, `kubernetes`, `cloudwatch`) delegate here
and shrink to a label + a probe lambda. (`http-health` is the deliberate exception: down IS
its signal — it maps a transport failure to `up=0`, not `reachable=False`, and its detail
carries latency — so it keeps its own hand-written probe.)

This is a LEAF module by design: it imports ONLY `datetime`, `collections.abc.Callable`, and
`core.model.SourceHealth` — no registry, config, signal, or adapter imports — so it sits at
the bottom of the dependency graph and any source can depend on it without a cycle.

The invariants this module owns (each codified below):

- **No-raise.** ANY exception from `probe()` becomes `SourceHealth(reachable=False, ...)`.
  The catch is `Exception`, NOT `BaseException` — `KeyboardInterrupt`/`SystemExit` MUST
  propagate so a shutdown signal is never swallowed into a misleading "unreachable".
- **No-`str(exc)`-leak (the SECURITY invariant).** The failure `detail` contains ONLY
  `source_label` + `type(exc.__cause__ or exc).__name__` — NEVER `str(exc)`. The exception
  message can echo a credential/endpoint; the class name cannot. `exc.__cause__ or exc` names
  the UNDERLYING cause so a `PanoptesError` wrapping an httpx/transport error surfaces the
  actionable transport class (e.g. `ConnectError`), not the generic wrapper.
- **Timestamp captured BEFORE the probe.** `checked_at` is read once, before `probe()` runs,
  and is the SAME value on both the reachable and unreachable branches (a slow/raising probe
  must not move the recorded instant past its own start).
"""

from collections.abc import Callable
from datetime import UTC, datetime

from core.model import SourceHealth


def probe_health(
    source_label: str,
    probe: Callable[[], object],
    *,
    checked_at: datetime | None = None,
    success_detail_factory: Callable[[object], str] | None = None,
) -> SourceHealth:
    """Run a read-only reachability `probe`, converting it to a leak-free `SourceHealth`.

    The single seam every covered Source's `health()` delegates to. It captures the timestamp
    BEFORE invoking `probe`, runs it, and:

    - on success → `SourceHealth(reachable=True, detail=success_detail_factory(result))`
      (default detail: ``f"{source_label} reachable"``);
    - on ANY `Exception` → `SourceHealth(reachable=False, detail=<generic failure summary>)`,
      where the summary is ``f"{source_label} unreachable (auth/transport error: {cls})"`` and
      `cls` is `type(exc.__cause__ or exc).__name__` — the class name ONLY, never `str(exc)`
      (the security invariant).

    `BaseException` (`KeyboardInterrupt`/`SystemExit`) is intentionally NOT caught — a shutdown
    signal propagates rather than being masked as an unreachable probe.

    Args:
        source_label: A short, NON-SECRET label naming the source in the detail (e.g.
            ``"prometheus endpoint"``, ``"cloudwatch credential resolution"``). It is echoed
            verbatim into the detail, so it MUST NOT contain a secret/credential.
        probe: The cheap read-only reachability call. Its return value is passed to
            `success_detail_factory` on success; any exception it raises is caught and
            summarized (its message is NEVER surfaced).
        checked_at: The timestamp to stamp on the result. Defaults to `datetime.now(UTC)`,
            captured BEFORE `probe` runs; the SAME value is used for both branches.
        success_detail_factory: Builds the reachable-branch detail from the probe's return
            value (e.g. an issue/node count). Defaults to ``f"{source_label} reachable"``.

    Returns:
        A `SourceHealth` — `reachable=True` with the success detail, or `reachable=False` with
        the generic, leak-free failure summary. Never raises an ordinary `Exception`.
    """
    # Capture the timestamp BEFORE the probe runs (the same instant is used on both branches,
    # so a slow/raising probe cannot move the recorded `checked_at` past its own start).
    observed_at = checked_at if checked_at is not None else datetime.now(UTC)
    try:
        result = probe()
    except Exception as exc:
        # No-raise + no-str(exc)-leak: convert ANY ordinary exception to reachable=False, and
        # surface ONLY the underlying cause's CLASS NAME (never the message, which can carry a
        # role ARN / bearer token / endpoint). `__cause__ or exc` names the wrapped transport
        # cause when a PanoptesError wraps one. BaseException is NOT caught — it propagates.
        cause = exc.__cause__ or exc
        return SourceHealth(
            reachable=False,
            detail=f"{source_label} unreachable (auth/transport error: {type(cause).__name__})",
            checked_at=observed_at,
        )
    detail = (
        success_detail_factory(result)
        if success_detail_factory is not None
        else (f"{source_label} reachable")
    )
    return SourceHealth(reachable=True, detail=detail, checked_at=observed_at)
