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

import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.errors import PanoptesError


def redact_url_userinfo(url: str) -> str:
    """Return `url` with any `user:pass@` userinfo stripped (so an embedded credential is hidden).

    A source health-probe SUCCESS detail (`describe_health`) embeds the configured endpoint URL
    verbatim. If an operator configured `https://user:token@host/...`, the verbatim URL would leak
    the credential through the MCP-visible rollup. This rebuilds the URL without the userinfo —
    `https://host/...` — so the success branch carries no secret. A URL with no userinfo is
    returned unchanged. The failure branch is already class-name-only and never embeds the URL.
    """
    split = urlsplit(url)
    # `hostname` drops userinfo (and lowercases the host); rebuild netloc as host[:port] only.
    if "@" not in split.netloc:
        return url
    host = split.hostname or ""
    netloc = f"{host}:{split.port}" if split.port is not None else host
    return urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))


# Trim length for the surfaced upstream response body (~800 chars): enough to carry the
# rejected-field/token detail without dumping an unbounded error page into logs. This is
# the SINGLE shared budget the four adapters previously each declared for themselves.
_BODY_TRIM_CHARS = 800

# Default per-request transport timeout (seconds). Every httpx call the RestClient makes
# is bounded by this unless the caller overrides it, so a hung upstream can never stall a
# request indefinitely — and the collector's per-source fetch worker thread always
# terminates instead of blocking the `ThreadPoolExecutor` teardown (F5).
_DEFAULT_TIMEOUT_SECONDS = 30.0

# Redaction marker substituted in place of any surfaced credential.
_REDACTED = "[REDACTED]"

# Redaction patterns for the surfaced upstream body (F4 / F2b). A Sentry/proxy that
# reflects request headers (or a request URL / JSON body) into its error body could
# otherwise land a credential in operator logs AND the MCP client. The patterns below are
# applied IN ORDER; each `re.sub` replaces the matched span wholesale with `[REDACTED]`,
# so a later pattern never re-scans an already-redacted span (this is what fixes the old
# `Bearer X` double-`[REDACTED]` bug — the Authorization-header rule consumes the whole
# value first, leaving nothing for the standalone-bearer rule to re-hit).
#
# F2b coverage gaps the old two-regex pair missed:
# - `Authorization: Token <x>` / `Authorization: Basic <b64>` — the old single `\S+`
#   surfaced the real value; the header rule now redacts the WHOLE value to end-of-line
#   (or a comma, the multi-header separator).
# - base64url tokens — the old bearer char class `[A-Za-z0-9._\-]+` missed `+`/`/`/`=`.
# - standalone `Token <x>` / secret header names / `?token=`/`?api_key=` query params /
#   `"api_key"`/`"token":"..."` JSON fields — none were covered at all.

# (1) Any `Authorization:`/`Authorization=` header — redact its value (an OPTIONAL scheme
# word `Bearer`/`Token`/`Basic`/… plus the single credential token). The credential token
# is matched as `[^\s,]+` — one run of non-whitespace, NON-COMMA chars — so it stops at a
# comma, the multi-header separator (e.g. `Authorization: Bearer X, X-Other: Y`): the comma
# and the following header survive in the redacted message rather than being swallowed. (A
# plain `\S+` would consume a trailing comma too; over-redacting it is harmless since the
# value is fully `[REDACTED]`, but stopping at the comma preserves the multi-header
# structure the surfaced diagnostic relies on — F3e.) Runs FIRST and consumes the scheme
# token, so the standalone-scheme rule (2) never double-fires on `Authorization: Bearer X`
# (the old `[REDACTED] [REDACTED]` bug). Redacting a single token (not the whole rest of
# line) keeps trailing diagnostic prose like `— denied`; a credential is always one token,
# so this captures Bearer/Token/Basic values fully.
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization)\s*[:=]\s*(?:[A-Za-z]+\s+)?[^\s,]+")

# (2) A standalone `Bearer <token>` / `Token <token>` scheme token NOT prefixed by an
# Authorization header (already redacted by rule 1). The char class is base64url-complete
# (`+`/`/`/`=` included) so a reflected base64url credential is fully consumed.
_SCHEME_TOKEN_RE = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-+/=]+")

# (3) Other secret-bearing header NAMES (api-key / x-api-key / x-auth-token) — redact the
# single value token, mirroring the Authorization rule (trailing prose preserved).
_SECRET_HEADER_RE = re.compile(r"(?i)\b(x-api-key|api-key|x-auth-token)\s*[:=]\s*\S+")

# (4) `?token=`/`?api_key=`/`&token=` query-string credentials — redact the value up to
# the next `&`/whitespace/quote.
_QUERY_CRED_RE = re.compile(r"(?i)([?&](?:token|api_key))=[^&\s\"']+")

# (5) `"api_key": "..."` / `"token": "..."` JSON credential fields — redact the quoted
# value only (keep the field name so the message still says WHICH field leaked).
_JSON_CRED_RE = re.compile(r'(?i)("(?:api_key|token)"\s*:\s*)"[^"]*"')

# Applied in declaration order; each carries its own replacement template.
_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_AUTH_HEADER_RE, rf"\1: {_REDACTED}"),
    (_SCHEME_TOKEN_RE, rf"\1 {_REDACTED}"),
    (_SECRET_HEADER_RE, rf"\1: {_REDACTED}"),
    (_QUERY_CRED_RE, rf"\1={_REDACTED}"),
    (_JSON_CRED_RE, rf'\1"{_REDACTED}"'),
)


class RestClient:
    """A thin, shared wrapper over `httpx.Client` with one-place failure surfacing.

    Owns the client-construction seam plus the `raise_for_status` + both-branch failure
    formatting that every HTTP adapter needs. Adapters hold a `RestClient` and delegate
    their transport to it, keeping only their own URL/request/parse logic.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        default_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Accept an optional injected httpx client; default to a fresh, time-bounded one.

        Under `respx` the default `httpx.Client()` is intercepted globally, so production
        code can pass none and tests need not inject one — the seam exists for explicit
        control (an adapter threading its own injected client, or a test wanting a
        specific client instance).

        `default_timeout` (seconds) bounds EVERY request the client makes unless a
        per-call timeout overrides it (F5): a no-timeout httpx call against a hung
        upstream would otherwise stall the collector's per-source fetch worker thread and
        block the `ThreadPoolExecutor` teardown at cycle end. The bound is applied to the
        constructed default client; an injected client keeps its own configured timeout
        (the injector owns it).
        """
        self._client = client if client is not None else httpx.Client(timeout=default_timeout)

    @property
    def http(self) -> httpx.Client:
        """The underlying httpx client, for adapters whose flow drives it directly."""
        return self._client

    def close(self) -> None:
        """Close the underlying httpx client, releasing its connection pool (F2c).

        A long-lived `RestClient` (the VM store's, alive for the process lifetime) would
        otherwise leak an unclosed socket that surfaces as a `ResourceWarning` at
        interpreter/teardown. Idempotent: closing an already-closed client is a no-op.
        """
        self._client.close()

    def __enter__(self) -> "RestClient":
        """Enter the context manager, returning self (the client is already open)."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Exit the context manager, closing the underlying httpx client (F2c)."""
        self.close()

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
            # A caller-supplied timeout overrides the client default; otherwise defer to
            # the client's own (bounded) default by not passing a per-request timeout — so
            # `None` never unbounds the request (F5).
            if timeout is not None:
                return http.get(url, params=params, headers=headers, timeout=timeout)
            return http.get(url, params=params, headers=headers)

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


def _format_failure(prefix: str, identifier: str, exc: httpx.HTTPError) -> str:
    """Build a diagnosable failure message, appending the upstream body when present.

    Handles both branches (the single home for what was VERBATIM-duplicated across the
    VM store and the sentry source):

    - **response present** (an `HTTPStatusError`): append the trimmed response body, which
      carries the rejected field/value the bare status code omits.
    - **no response** (a connection/timeout error such as `httpx.ConnectError`): there is
      nothing to read, so fall back to the exception's own message — and critically, do
      not touch `exc.response` (it is absent and would crash).

    `identifier` is sanitized via `redact_url_userinfo` before interpolation: a caller often
    passes an endpoint URL there (the VM store / sentry / grafana-ping call sites), and a
    `https://user:pass@host` URL would otherwise leak its embedded credential into operator
    logs and the MCP-visible error on EVERY failure (MAJOR-2). Note this strips only `user:pass@`
    userinfo — a path-embedded token (e.g. a Slack `hooks.slack.com/services/.../<token>` URL)
    is NOT a URL credential `redact_url_userinfo` can see, so a caller whose identifier IS such a
    secret must pass a NON-secret identifier instead (SlackNotifier does).
    """
    safe_identifier = redact_url_userinfo(identifier)
    response = getattr(exc, "response", None)
    if response is not None:
        # Trim first (bound the work + the log), then redact any reflected bearer token /
        # Authorization header value so a header-echoing upstream can never leak a secret
        # into operator logs or the MCP client (F4).
        body = _redact_secrets(response.text[:_BODY_TRIM_CHARS])
        return (
            f"{prefix} ({safe_identifier}): HTTP {response.status_code}. "
            f"Upstream response body: {body}"
        )
    # Connection error — no response object; surface the underlying exception text (also
    # redacted defensively, in case a transport error message carries a reflected header).
    return f"{prefix} ({safe_identifier}): {_redact_secrets(str(exc))}"


def _redact_secrets(text: str) -> str:
    """Replace any reflected credential in a surfaced body with a marker (F4 / F2b).

    Applies the ordered `_REDACTION_RULES` so a header-reflecting (or URL-/JSON-echoing)
    upstream cannot leak the request's own secret into operator logs or the MCP client.
    The Authorization-header rule runs FIRST and consumes the WHOLE header value, so the
    standalone-scheme rule never double-fires on it (no `[REDACTED] [REDACTED]`). Covers
    Bearer/Token/Basic Authorization values, base64url tokens, standalone scheme tokens,
    `api-key`/`x-api-key`/`x-auth-token` headers, `?token=`/`?api_key=` query params, and
    `"api_key"`/`"token":"..."` JSON fields.
    """
    redacted = text
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted
