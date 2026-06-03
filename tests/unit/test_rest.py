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


def test_injected_client_is_used() -> None:
    # The injected-client seam: a RestClient built with an explicit client exposes it,
    # mirroring the adapter constructor seams that delegate their client to RestClient.
    injected = httpx.Client()
    client = RestClient(injected)

    assert client.http is injected
