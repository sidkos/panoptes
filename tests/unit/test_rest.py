"""Unit tests for the shared `core.rest` REST plumbing module.

`core.rest.RestClient` owns the httpx-client construction seam, `raise_for_status`,
and the **both-branch** failure surfacing that four adapters (the VictoriaMetrics
store + the sentry / http-health / grafana adapters) previously reimplemented. This
module tests that shared contract ONCE so the per-adapter tests can stay focused on
their own URL building / parsing / derived metrics:

- `get_json` GETs, parses, and returns the decoded JSON on a 2xx;
- `get_json` forwards `params`/`headers`/`timeout` to the underlying client;
- a 5xx-with-body raises a `PanoptesError` whose message carries the trimmed
  upstream response body (the rejected-field detail the bare status code omits) —
  failure branch (a), response present;
- a connection error raises a `PanoptesError` naming the failure WITHOUT crashing on
  the absent `.response` — failure branch (b), no response object;
- the surfaced body is trimmed to the single shared `_BODY_TRIM_CHARS` budget;
- the injected-client seam is honored (an explicitly injected client is used), and
  the default (no-injection) client is intercepted globally by `respx`.

All httpx is mocked with `respx`, which patches the transport globally — so the
default `httpx.Client()` the `RestClient` constructs is intercepted without an
injected client, mirroring the existing adapter test style.
"""

import httpx
import pytest
import respx
from core.errors import PanoptesError
from core.rest import _BODY_TRIM_CHARS, RestClient

_URL = "http://upstream.internal/api/v1/thing"


@respx.mock
def test_get_json_returns_decoded_json_on_2xx() -> None:
    respx.get(_URL).mock(return_value=httpx.Response(200, json={"hello": "world"}))
    client = RestClient()

    payload = client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    assert payload == {"hello": "world"}


@respx.mock
def test_get_json_forwards_params_and_headers() -> None:
    captured: dict[str, str] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=[])

    respx.get(_URL).mock(side_effect=_record)
    client = RestClient()

    client.get_json(
        _URL,
        prefix="thing fetch failed",
        identifier=_URL,
        params={"environment": "dev"},
        headers={"Authorization": "Bearer tok"},
    )

    assert captured["environment"] == "dev"
    assert captured["auth"] == "Bearer tok"


@respx.mock
def test_get_json_5xx_with_body_raises_and_surfaces_body() -> None:
    rejection_body = "field 'env' rejected: cannot be empty"
    respx.get(_URL).mock(return_value=httpx.Response(500, text=rejection_body))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    # Failure branch (a): response present — the upstream body is surfaced verbatim,
    # alongside the status code and the caller's prefix + identifier.
    assert rejection_body in message
    assert "thing fetch failed" in message
    assert "500" in message
    assert _URL in message


@respx.mock
def test_get_json_connection_error_raises_without_response() -> None:
    respx.get(_URL).mock(side_effect=httpx.ConnectError("boom"))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    # Failure branch (b): no `.response` to read — must NOT crash on the missing
    # response; the message still names the failure via the exception text.
    assert "boom" in message
    assert "thing fetch failed" in message
    assert _URL in message


@respx.mock
def test_failure_body_is_trimmed_to_shared_budget() -> None:
    # A body longer than the shared trim budget is truncated to `_BODY_TRIM_CHARS`.
    oversized_body = "x" * (_BODY_TRIM_CHARS + 500)
    respx.get(_URL).mock(return_value=httpx.Response(500, text=oversized_body))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    # The trimmed slice appears; the full oversized body does not.
    assert "x" * _BODY_TRIM_CHARS in message
    assert oversized_body not in message


@respx.mock
def test_failure_body_redacts_bearer_token() -> None:
    """A reflected `Authorization: Bearer <token>` in the body is REDACTED (F4).

    A Sentry/proxy that echoes request headers into its error body would otherwise
    land the raw bearer token in operator logs AND the surfaced PanoptesError. The
    `_format_failure` body must replace the token with `[REDACTED]`.
    """
    leaky_body = "rejected request with header Authorization: Bearer secrettoken123 — denied"
    respx.get(_URL).mock(return_value=httpx.Response(403, text=leaky_body))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    assert "secrettoken123" not in message, "the raw bearer token must not leak"
    assert "[REDACTED]" in message
    # The surrounding diagnostic context (status + the non-secret prose) survives.
    assert "403" in message
    assert "denied" in message


@pytest.mark.parametrize(
    ("leaky_body", "secret"),
    [
        # Non-Bearer Authorization schemes the old single-`\S+` pattern surfaced.
        ("Authorization: Basic dXNlcjpwYXNz was rejected", "dXNlcjpwYXNz"),
        ("denied: Authorization: Token abc123secret here", "abc123secret"),
        # Standalone scheme tokens (no Authorization header prefix).
        ("got header Token abc123secret in request", "abc123secret"),
        # Base64url bearer (the old char class missed `+`/`/`/`=`).
        ("Authorization: Bearer tok+with/slash= denied", "tok+with/slash="),
        ("reflected bearer tok+with/slash= in body", "tok+with/slash="),
        # Other secret-bearing header names.
        ("X-Api-Key: k3ysecret rejected", "k3ysecret"),
        ("api-key: k3ysecret rejected", "k3ysecret"),
        ("X-Auth-Token: authsecret denied", "authsecret"),
        # Query-string credentials.
        ("upstream url ?token=secretval failed", "secretval"),
        ("upstream url ?api_key=secretval failed", "secretval"),
        # JSON-field credentials.
        ('echoed body {"api_key":"secretjson"} rejected', "secretjson"),
        ('echoed body {"token":"secretjson2"} rejected', "secretjson2"),
    ],
)
@respx.mock
def test_failure_body_redacts_non_bearer_credentials(leaky_body: str, secret: str) -> None:
    """Every credential-bearing shape is redacted, not just `Bearer <token>` (F2b).

    The old redaction consumed only ONE `\\S+` after `Authorization:` (so `Basic`/`Token`
    surfaced the real value) and a bearer char class that missed base64url chars. The
    reworked redaction must surface `[REDACTED]` and NEVER the secret for every shape:
    non-Bearer Authorization schemes, standalone scheme tokens, base64url bearers, other
    secret headers, query-string creds, and JSON credential fields.
    """
    respx.get(_URL).mock(return_value=httpx.Response(403, text=leaky_body))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    assert secret not in message, f"the secret {secret!r} must not leak"
    assert "[REDACTED]" in message


@respx.mock
def test_failure_body_authorization_redaction_stops_at_comma_separator() -> None:
    """The Authorization-value redaction stops at the comma multi-header separator (F3e).

    The redaction comment documents the rule as stopping at "a comma, the multi-header
    separator". With a plain `\\S+` the value run swallowed a trailing comma; anchoring it
    to `[^\\s,]+` makes the documented behavior real, so a comma-separated following header
    survives intact in the surfaced message (the secret is still fully redacted).
    """
    leaky_body = "rejected: Authorization: Bearer secrettoken123, X-Other: keepme denied"
    respx.get(_URL).mock(return_value=httpx.Response(403, text=leaky_body))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    # The credential is fully redacted...
    assert "secrettoken123" not in message
    assert "[REDACTED]" in message
    # ...and the comma separator + the following header survive (not swallowed by `\\S+`).
    assert ", X-Other: keepme" in message


@respx.mock
def test_failure_body_bearer_redacts_cleanly_without_double_marker() -> None:
    """`Authorization: Bearer x` redacts once, not `[REDACTED] [REDACTED]` (F2b).

    The old code ran the Authorization-header pattern AND the standalone-bearer pattern
    over the same span, double-firing on `Authorization: Bearer x`. The reworked
    redaction redacts the whole Authorization header value first and does not re-scan it.
    """
    respx.get(_URL).mock(
        return_value=httpx.Response(403, text="Authorization: Bearer secrettok123 was denied")
    )
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    assert "secrettok123" not in message
    assert "[REDACTED]" in message
    # No double-redaction artifact from two patterns firing over the same span.
    assert "[REDACTED] [REDACTED]" not in message
    # Surrounding non-secret context survives.
    assert "denied" in message


@respx.mock
def test_send_returns_response_on_2xx() -> None:
    # `send` is the generic seam used by adapters whose request shape is not a plain
    # GET (a POST with a content body, or a two-step Retry-After flow): it applies
    # raise_for_status + the shared failure surfacing around a caller-supplied call.
    route = respx.post(_URL).mock(return_value=httpx.Response(204))
    client = RestClient()

    response = client.send(
        lambda http: http.post(_URL, content=b"payload"),
        prefix="thing post failed",
        identifier=_URL,
    )

    assert route.called
    assert response.status_code == 204


@respx.mock
def test_send_5xx_with_body_raises_and_surfaces_body() -> None:
    rejection_body = "import rejected: bad line"
    respx.post(_URL).mock(return_value=httpx.Response(500, text=rejection_body))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.send(
            lambda http: http.post(_URL, content=b"payload"),
            prefix="thing post failed",
            identifier=_URL,
        )

    assert rejection_body in str(excinfo.value)


@respx.mock
def test_send_connection_error_raises_without_response() -> None:
    respx.post(_URL).mock(side_effect=httpx.ConnectError("refused"))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.send(
            lambda http: http.post(_URL, content=b"payload"),
            prefix="thing post failed",
            identifier=_URL,
        )

    message = str(excinfo.value)
    assert "refused" in message
    assert _URL in message


@respx.mock
def test_get_json_read_timeout_raises_panoptes_error_not_hang() -> None:
    """A read-timeout on a GET surfaces a PanoptesError (F5) rather than hanging.

    httpx raises `ReadTimeout` (an `httpx.HTTPError` with no `.response`); the shared
    `send` must wrap it in a PanoptesError via `_format_failure`.
    """
    respx.get(_URL).mock(side_effect=httpx.ReadTimeout("upstream too slow"))
    client = RestClient()

    with pytest.raises(PanoptesError) as excinfo:
        client.get_json(_URL, prefix="thing fetch failed", identifier=_URL)

    message = str(excinfo.value)
    assert "thing fetch failed" in message
    assert _URL in message


def test_default_client_has_a_bounded_timeout() -> None:
    """The RestClient's default httpx client carries a NON-None concrete timeout (F5).

    Without a bounded timeout a hung upstream stalls the collector's per-source fetch
    worker thread, which then blocks the `ThreadPoolExecutor` teardown at cycle end.
    A default-constructed RestClient must therefore bound every request: the underlying
    client's default timeout is concrete (not `httpx.Timeout(None)`).
    """
    client = RestClient()
    timeout = client.http.timeout
    # Every phase of the request is bounded (no None component).
    assert timeout.connect is not None
    assert timeout.read is not None
    assert timeout.write is not None
    assert timeout.pool is not None


def test_constructor_timeout_override_is_applied() -> None:
    """An explicit `default_timeout` overrides the built-in default on the client (F5)."""
    client = RestClient(default_timeout=5.0)
    assert client.http.timeout.read == 5.0


def test_close_closes_underlying_client() -> None:
    """`RestClient.close()` closes the underlying httpx client (F2c socket hygiene).

    A long-lived `RestClient` (e.g. the VM store's, alive for the process lifetime)
    leaves an unclosed socket → a `ResourceWarning` at teardown. `close()` must close
    the underlying httpx client so the socket is released.
    """
    client = RestClient()
    assert client.http.is_closed is False
    client.close()
    assert client.http.is_closed is True


def test_context_manager_closes_client_on_exit() -> None:
    """`RestClient` is a context manager that closes its client on exit (F2c)."""
    with RestClient() as client:
        assert client.http.is_closed is False
        inner = client
    assert inner.http.is_closed is True


def test_injected_client_is_used() -> None:
    # The injected-client seam: a RestClient built with an explicit client exposes it,
    # mirroring the adapter constructor seams that delegate their client to RestClient.
    injected = httpx.Client()
    client = RestClient(injected)

    assert client.http is injected
