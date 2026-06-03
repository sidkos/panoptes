"""Unit tests for the generic `fan_out_over_envs` env="all" helper.

The `env="all"` fan-out contract — iterate every enabled env, fetch per-env, and on a
`CapabilityError` capture a per-env error marker instead of wholesale-failing — was
reimplemented in `search_incidents` and `search_logs`. `fan_out_over_envs` owns that
iterate-and-mark contract ONCE, generic over the per-env result type (PEP 695). The
two tools pass only their per-env `fetch_one` and project the generic results into their
own `IncidentFanOut`/`LogFanOut` TypedDicts.

This tests the generic helper directly (happy path + partial-result on a raising env),
so the per-tool fan-out tests in `test_mcp_query.py` stay focused on the projection.
"""

from core.config import ResolvedConfig, ResolvedEnvironment
from core.errors import CapabilityError, PanoptesError
from core.mcp.context import QueryContext
from core.mcp.tools_query import fan_out_over_envs


def _context(environments: dict[str, ResolvedEnvironment]) -> QueryContext:
    """A minimal `QueryContext` carrying only the environments the helper iterates.

    The helper now consumes the `QueryContext` seam, so a fan-out test no longer needs
    to drive `fan_out_over_envs` through the raw `ResolvedConfig` — it builds the
    smallest config the context needs (just the envs) and wraps it once.
    """

    class _NullStore:
        type = "null"

        def write(self, signals: list[object]) -> None:  # pragma: no cover - unused
            return None

        def query(self, query: object) -> list[object]:  # pragma: no cover - unused
            return []

    config = ResolvedConfig(
        environments=environments,
        store=_NullStore(),  # type: ignore[arg-type]
        notifiers=[],
        dashboard_packs=[],
        slos=[],
        mcp={},
    )
    return QueryContext(config)


def _env(name: str, *, enabled: bool = True) -> ResolvedEnvironment:
    return ResolvedEnvironment(name=name, enabled=enabled, sources=[])


def test_fan_out_collects_one_result_per_enabled_env() -> None:
    """The helper iterates every ENABLED env and carries each `fetch_one` result."""
    context = _context({"dev": _env("dev"), "stage": _env("stage")})

    results = fan_out_over_envs(context, lambda environment: f"data-for-{environment.name}")

    by_env = {result.env: result for result in results}
    assert set(by_env) == {"dev", "stage"}
    assert by_env["dev"].data == "data-for-dev"
    assert by_env["dev"].error is None
    assert by_env["stage"].data == "data-for-stage"
    assert by_env["stage"].error is None


def test_fan_out_skips_disabled_envs() -> None:
    """A disabled env is inert — it is not iterated (mirrors `_enabled_envs`)."""
    context = _context({"dev": _env("dev"), "stage": _env("stage", enabled=False)})

    results = fan_out_over_envs(context, lambda environment: environment.name)

    assert {result.env for result in results} == {"dev"}


def test_fan_out_marks_a_raising_env_with_an_error_not_a_wholesale_fail() -> None:
    """An env whose `fetch_one` raises CapabilityError yields a per-env error marker.

    The other envs still return their data — the call is a partial result, not a
    wholesale failure.
    """
    context = _context({"dev": _env("dev"), "stage": _env("stage")})

    def _fetch_one(environment: ResolvedEnvironment) -> str:
        if environment.name == "stage":
            raise CapabilityError("stage cannot answer this query")
        return f"data-for-{environment.name}"

    results = fan_out_over_envs(context, _fetch_one)

    by_env = {result.env: result for result in results}
    # dev answered with data + no error.
    assert by_env["dev"].data == "data-for-dev"
    assert by_env["dev"].error is None
    # stage carries the explicit per-env error marker, with no data.
    assert by_env["stage"].data is None
    assert by_env["stage"].error == "stage cannot answer this query"


def test_fan_out_marks_a_down_live_source_panoptes_error_not_a_wholesale_fail() -> None:
    """A configured-but-DOWN source raises a bare PanoptesError (not CapabilityError).

    F2: a live source failure (e.g. Sentry 5xx) raises the documented base
    `PanoptesError`. The fan-out must mark just that env down — NOT fail the whole
    multi-env call. The other envs still return their data (partial result).
    """
    context = _context({"dev": _env("dev"), "stage": _env("stage")})

    def _fetch_one(environment: ResolvedEnvironment) -> str:
        if environment.name == "stage":
            # A configured-but-down upstream surfaces a bare PanoptesError (the
            # REST-client failure type), NOT a CapabilityError.
            raise PanoptesError("stage source returned HTTP 503")
        return f"data-for-{environment.name}"

    results = fan_out_over_envs(context, _fetch_one)

    by_env = {result.env: result for result in results}
    # dev still answered — one down env does not wholesale-fail the call.
    assert by_env["dev"].data == "data-for-dev"
    assert by_env["dev"].error is None
    # stage is marked down with its error detail, no data.
    assert by_env["stage"].data is None
    assert by_env["stage"].error == "stage source returned HTTP 503"
