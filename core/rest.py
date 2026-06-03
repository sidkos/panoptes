"""Shared REST plumbing — the one deep module the HTTP adapters delegate to.

Four adapters speak HTTP to an upstream (the VictoriaMetrics store + the sentry /
http-health / grafana adapters). Before this module each one reimplemented the same
plumbing: an injectable `httpx.Client` seam, `raise_for_status`, and a two-branch
failure formatter that surfaces the upstream response body (trimmed) on an HTTP-status
error vs. falling back to the exception text on a connection/transport error. The
`_format_failure` body was VERBATIM in two of them.

`RestClient` owns exactly that shared plumbing and nothing adapter-specific. Each
adapter keeps only what genuinely differs — its URL building, request shape, JSON/line
parsing, and derived metrics — and delegates the client construction + failure surfacing
here. The interface is deliberately small:

- `get_json(url, *, prefix, identifier, params, headers, timeout)` — the common case
  (GET → `raise_for_status` → parse JSON), returning the decoded payload or raising a
  `PanoptesError` whose message carries the surfaced upstream body.
- `send(call, *, prefix, identifier)` — the generic seam for a request that is NOT a
  plain GET: the caller supplies a function `call(http) -> httpx.Response` (a POST with
  a content body, or a multi-step flow such as sentry's Retry-After retry), and this
  applies `raise_for_status` + the same shared failure surfacing around it, returning
  the raw response so the caller parses it however it needs.
- `http` — the underlying `httpx.Client`, exposed so an adapter whose flow genuinely
  needs the raw client (e.g. inspecting a 429 status before deciding to retry) can drive
  it directly and still funnel the final `raise_for_status` through `send`.

**Why the body is surfaced.** `str(httpx.HTTPStatusError)` carries only URL + status
code; the actionable detail (which field/token/value the upstream rejected) lives in the
response text. Surfacing the trimmed body makes a 4xx/5xx diagnosable in one cycle (the
REST-client convention). The single shared `_BODY_TRIM_CHARS` budget caps it so a
hostile/huge error page can never dump unbounded text into logs.

httpx is mocked in tests with `respx`, which patches the transport globally, so a default
`httpx.Client()` constructed here is intercepted without an injected client. The client is
still an explicit constructor seam: an adapter that wants to inject its own client (or a
test that wants explicit control) passes one through, and `RestClient` threads it down.
"""

from collections.abc import Callable, Mapping

import httpx

from core.errors import PanoptesError

# Trim length for the surfaced upstream response body (~800 chars): enough to carry the
# rejected-field/token detail without dumping an unbounded error page into logs. This is
# the SINGLE shared budget the four adapters previously each declared for themselves.
_BODY_TRIM_CHARS = 800


class RestClient:
    """A thin, shared wrapper over `httpx.Client` with one-place failure surfacing.

    Owns the client-construction seam plus the `raise_for_status` + both-branch failure
    formatting that every HTTP adapter needs. Adapters hold a `RestClient` and delegate
    their transport to it, keeping only their own URL/request/parse logic.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Accept an optional injected httpx client; default to a fresh one.

        Under `respx` the default `httpx.Client()` is intercepted globally, so production
        code can pass none and tests need not inject one — the seam exists for explicit
        control (an adapter threading its own injected client, or a test wanting a
        specific client instance).
        """
        self._client = client if client is not None else httpx.Client()

    @property
    def http(self) -> httpx.Client:
        """The underlying httpx client, for adapters whose flow drives it directly."""
        return self._client

    def get_json(
        self,
        url: str,
        *,
        prefix: str,
        identifier: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> object:
        """GET `url`, raise-for-status, and return the decoded JSON payload.

        Args:
            url: The absolute endpoint to GET.
            prefix: A short human prefix for the failure message (e.g. "VM query failed").
            identifier: The endpoint/identifier surfaced in the failure message.
            params: Optional query parameters forwarded to httpx.
            headers: Optional request headers forwarded to httpx (e.g. Authorization).
            timeout: Optional per-request timeout (seconds) forwarded to httpx.

        Returns:
            The decoded JSON (the caller validates its shape).

        Raises:
            PanoptesError: any 4xx/5xx (with the surfaced upstream body) or transport
                error (with the exception text) — never a raw httpx exception.
        """

        def _call(http: httpx.Client) -> httpx.Response:
            return http.get(url, params=params, headers=headers, timeout=_resolve_timeout(timeout))

        response = self.send(_call, prefix=prefix, identifier=identifier)
        return response.json()

    def send(
        self,
        call: Callable[[httpx.Client], httpx.Response],
        *,
        prefix: str,
        identifier: str,
    ) -> httpx.Response:
        """Run a caller-supplied request, raise-for-status, and surface failures uniformly.

        `call(http)` produces the `httpx.Response` (a plain GET, a POST with a content
        body, or the final response of a multi-step flow). This applies `raise_for_status`
        and wraps BOTH failure branches in a `PanoptesError`, returning the raw response on
        success so the caller parses it however it needs.

        Args:
            call: A function taking the underlying httpx client and returning a response.
            prefix: A short human prefix for the failure message.
            identifier: The endpoint/identifier surfaced in the failure message.

        Returns:
            The successful (2xx, post-`raise_for_status`) httpx response.

        Raises:
            PanoptesError: an HTTP-status error (body surfaced) or a connection/transport
                error (exception text), via the shared `_format_failure`.
        """
        try:
            response = call(self._client)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 4xx/5xx — the response body names the rejected field/token/value.
            raise PanoptesError(_format_failure(prefix, identifier, exc)) from exc
        except httpx.HTTPError as exc:
            # Connection/timeout — no response body to surface; the helper copes.
            raise PanoptesError(_format_failure(prefix, identifier, exc)) from exc
        return response


def _resolve_timeout(timeout: float | None) -> httpx.Timeout | float:
    """Map an optional caller timeout to httpx's `timeout=` argument.

    `None` defers to httpx's own default (the adapters that don't time-bound a request
    pass none); a concrete value is forwarded verbatim so a probe can bound its wait.
    """
    if timeout is None:
        return httpx.Timeout(None)
    return timeout


def _format_failure(prefix: str, identifier: str, exc: httpx.HTTPError) -> str:
    """Build a diagnosable failure message, appending the upstream body when present.

    Handles both branches (the single home for what was VERBATIM-duplicated across the
    VM store and the sentry source):

    - **response present** (an `HTTPStatusError`): append the trimmed response body, which
      carries the rejected field/value the bare status code omits.
    - **no response** (a connection/timeout error such as `httpx.ConnectError`): there is
      nothing to read, so fall back to the exception's own message — and critically, do
      not touch `exc.response` (it is absent and would crash).
    """
    response = getattr(exc, "response", None)
    if response is not None:
        body = response.text[:_BODY_TRIM_CHARS]
        return (
            f"{prefix} ({identifier}): HTTP {response.status_code}. Upstream response body: {body}"
        )
    # Connection error — no response object; surface the underlying exception text.
    return f"{prefix} ({identifier}): {exc}"
