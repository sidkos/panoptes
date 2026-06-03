"""The MCP streamable-HTTP entrypoint — the hosted face over the SAME tool table.

`run_http(config)` builds the server via the IDENTICAL `build_server(config)` the stdio
path uses (spec § MCP server HTTP transport — "two faces, one store"), then serves it over
FastMCP's streamable-HTTP transport. The HTTP face cannot diverge from stdio because it is
the same registered tool table; only the transport arg differs.

**Bind note (load-bearing).** The server binds its CONTAINER port (default `0.0.0.0:8080`
inside the pod). Binding a non-loopback address inside the pod is acceptable because on
Kubernetes the network boundary is the `ClusterIP` Service + the nginx ingress (the only
public path) — NOT the server's listen address. The GitHub auth gate is enforced at the
ingress + oauth2-proxy (`github` provider, org/team allowlist), not by a private bind. The
Phase-7 Helm render test asserts the MCP Service is `ClusterIP` (never LoadBalancer/
NodePort) + the ingress forward-auth annotations — that render is where the boundary is
verified, not here. This server NEVER validates a token; the ingress is the gate.

**Unit-testable without binding a port.** The blocking server-runner is injected via the
optional `runner` seam (default = `server.run_http`), so a unit test passes a fake runner
to assert that `build_server(config)` was reused and that host/port were parsed — without
ever opening a socket. Production leaves `runner=None` and the real (blocking) bind runs.

IMPORTANT (FastMCP / PEP-563): this module must NOT add `from __future__ import
annotations` — it lives under `core/mcp/`, and deferred annotations break FastMCP's schema
generation for the tool returns the built server exposes.
"""

import logging
import os
from collections.abc import Callable

from core.config import ResolvedConfig
from core.mcp.server import PanoptesMcpServer, build_server

_LOGGER = logging.getLogger(__name__)

# The env vars selecting the bind address inside the pod, with their container defaults.
# `0.0.0.0` binds all interfaces (the pod's own network namespace); `8080` is the
# conventional container port the ClusterIP Service + ingress front (Phase 7).
_HOST_ENV_VAR = "PANOPTES_MCP_HOST"
_PORT_ENV_VAR = "PANOPTES_MCP_PORT"
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8080

# The blocking server-runner shape: given the built server + bind address, serve forever.
# Defaulted to the server's own `run_http`; injected as a fake in unit tests so no port is
# ever bound there.
HttpRunner = Callable[[PanoptesMcpServer, str, int], None]


def run_http(config: ResolvedConfig, *, runner: HttpRunner | None = None) -> None:
    """Serve the MCP over streamable-HTTP, reusing the SAME `build_server(config)`.

    Builds the server with the IDENTICAL builder the stdio path uses (two faces, one
    store), resolves the bind host/port from the environment (with container defaults),
    and hands them to the runner. The default runner is the server's real (blocking)
    `run_http`; a unit test injects a fake runner to assert the wiring without binding.

    Args:
        config: The resolved config (bound into every tool wrapper by `build_server`).
        runner: The blocking server-runner seam. Defaults to `server.run_http` (the real
            bind). A unit test passes a fake that records the server + bind args instead
            of serving, so no socket is opened.
    """
    server = build_server(config)
    host = os.environ.get(_HOST_ENV_VAR) or _DEFAULT_HOST
    port = _resolve_port(os.environ.get(_PORT_ENV_VAR))

    # Default to the server's real (blocking) bind; a test injects its own non-binding fake.
    serve = runner if runner is not None else _default_runner
    _LOGGER.info(
        f"starting MCP streamable-HTTP server on {host}:{port} "
        f"(the GitHub auth gate is the nginx ingress + oauth2-proxy, not this bind)"
    )
    serve(server, host, port)


def _default_runner(server: PanoptesMcpServer, host: str, port: int) -> None:
    """The production runner: serve the built server over streamable-HTTP (blocks)."""
    server.run_http(host, port)


def _resolve_port(raw_port: str | None) -> int:
    """Parse the `PANOPTES_MCP_PORT` env value, falling back to the default on a bad value.

    A non-numeric (or empty) value falls back to the container default rather than
    crashing the pod at startup — a misconfigured port string should not take the server
    down; the fallback is logged so the operator sees the offending value.
    """
    if not raw_port:
        return _DEFAULT_PORT
    try:
        return int(raw_port)
    except ValueError:
        _LOGGER.warning(
            f"Invalid {_PORT_ENV_VAR}={raw_port!r}; falling back to the default port "
            f"{_DEFAULT_PORT}."
        )
        return _DEFAULT_PORT
