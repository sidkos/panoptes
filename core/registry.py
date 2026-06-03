"""Typed per-plane adapter registries.

Each plane (source / store / notifier / dashboard) owns a `Registry[T]` generic
over its plane Protocol, so mypy `--strict` verifies a registered class structurally
satisfies the plane at registration time. Adapters self-register with the
`register("<type>")` decorator keyed on their `type` discriminator; the config
loader later calls `build("<type>", config)` to instantiate one from a config block.

**Construction convention (locked for Phases 2-6):** `build` instantiates a
registered class with the config block as the single positional argument —
`cls(config)`. Every adapter's `__init__` therefore takes exactly one parameter, a
`Mapping[str, str | int | bool | list[str]]`. This keeps construction uniform across
planes; an adapter pulls its own typed fields out of the mapping internally.
"""

from collections.abc import Callable, Mapping

from core.errors import UnknownAdapterError
from core.planes.dashboard import DashboardProvider
from core.planes.notifier import Notifier
from core.planes.source import Source
from core.planes.store import Store

# The config-block value type: scalars + string lists, mirroring what the YAML
# loader produces after `${VAR}` interpolation. No `Any` — the union is exhaustive
# for the v0.1 config schema.
ConfigValue = str | int | bool | list[str]
ConfigBlock = Mapping[str, ConfigValue]


class Registry[T]:
    """A name -> adapter-factory table scoped to a single plane.

    `kind` is the plane discriminator (`source`/`store`/`notifier`/`dashboard`)
    surfaced in fail-fast `UnknownAdapterError` messages.

    The adapter table holds each adapter as its `Callable[[ConfigBlock], T]` factory
    shape — a registered adapter *class* whose `__init__(self, config)` matches this
    shape satisfies it directly. Modeling the table this way (rather than `type[T]`)
    lets `build` instantiate with `factory(config)` and stay precisely typed under
    mypy `--strict`: `type[T]` would bind the constructor to `T`'s upper bound
    (`object`), which takes no args, and would reject the config argument.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._adapters: dict[str, Callable[[ConfigBlock], T]] = {}

    def register(
        self, adapter_type: str
    ) -> Callable[[Callable[[ConfigBlock], T]], Callable[[ConfigBlock], T]]:
        """Decorator: record the adapter factory under `adapter_type`, unchanged.

        The decorated symbol is returned unchanged (`@REGISTRY.register("x")` is
        transparent), so adapters remain directly importable and testable. The
        decorated value is the adapter *class* — typed here by its factory shape
        `Callable[[ConfigBlock], T]` (its one-arg config constructor), which is what
        `build` invokes. `adapter_type` (not the builtin name `type`) avoids shadowing
        `type` inside the annotations.
        """

        def _decorator(factory: Callable[[ConfigBlock], T]) -> Callable[[ConfigBlock], T]:
            self._adapters[adapter_type] = factory
            return factory

        return _decorator

    def build(self, adapter_type: str, config: ConfigBlock) -> T:
        """Instantiate the adapter registered under `adapter_type` from `config`.

        Fails fast with `UnknownAdapterError` (listing the available types) when
        `adapter_type` is not registered — the operator sees which types are valid.
        """
        factory = self._adapters.get(adapter_type)
        if factory is None:
            raise UnknownAdapterError(self.kind, adapter_type, self.available())
        # Locked convention: single positional config-block argument (module docstring).
        return factory(config)

    def available(self) -> list[str]:
        """The registered adapter types for this plane (sorted, for stable messages)."""
        return sorted(self._adapters.keys())


# The four plane registries, typed to their plane Protocol so registration is
# structurally checked by mypy `--strict`.
SOURCES: Registry[Source] = Registry("source")
STORES: Registry[Store] = Registry("store")
NOTIFIERS: Registry[Notifier] = Registry("notifier")
DASHBOARD_PROVIDERS: Registry[DashboardProvider] = Registry("dashboard")
