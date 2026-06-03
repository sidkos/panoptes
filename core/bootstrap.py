"""Shared adapter-registration bootstrap for the runnable entrypoints.

Core adapters self-register on their plane registry via an `@REGISTRY.register(...)`
decorator AT IMPORT TIME, but nothing imports them eagerly — so the pure config
loader and the unit test runs never drag in `boto3`/`httpx`. Any entrypoint that
builds REAL adapters from a config (the collector CLI and the MCP server CLI) must
therefore import the adapter modules first, so their `type` fields resolve.

This is the single source of truth for that import-for-registration step, shared by
`core.collector.main` and `core.mcp.server.main` so the two entrypoints can never
drift out of sync (a missing module in one but not the other would make
`python -m core.mcp.server` raise `UnknownAdapterError` on a config the collector
resolves fine). Imported lazily by the callers (inside `main`) so the heavy
upstream-SDK dependencies stay out of the modules' static import graphs.
"""

import importlib

# Every core adapter module, imported purely for its registration side-effect. Adding
# a new core adapter? Add its module here so BOTH entrypoints can build it.
_CORE_ADAPTER_MODULES = (
    "core.notifiers.logging_notifier",
    "core.sources.cloudwatch",
    "core.sources.http_health",
    "core.sources.sentry",
    "core.stores.passthrough",
    "core.stores.victoriametrics",
)


def register_core_adapters() -> None:
    """Import each core adapter module so it self-registers on its plane registry.

    Idempotent: a re-import of an already-loaded module is a no-op, so calling this
    more than once in a process is harmless.
    """
    for module_path in _CORE_ADAPTER_MODULES:
        importlib.import_module(module_path)
