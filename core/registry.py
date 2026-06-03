"""Typed per-plane adapter registries.

Phase 0 stub: a minimal generic ``Registry[T]`` holding a plane ``kind`` label and
a name->class table, plus the four module-level registries the rest of ``core``
binds adapters onto. The full ``register`` decorator and fail-fast ``build`` land
in Phase 1; this skeleton exists only so the import test can assert the four
registries exist and are distinct instances keyed by plane.
"""


class Registry[T]:
    """A name -> adapter-class table scoped to a single plane.

    ``kind`` is the plane discriminator (``source``/``store``/``notifier``/
    ``dashboard``) used in fail-fast error messages once Phase 1 adds ``build``.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._adapters: dict[str, type[T]] = {}


# The four plane registries. Phase 1 re-types each to its plane Protocol; the
# Phase 0 element type is ``object`` because the Protocols do not exist yet.
SOURCES: Registry[object] = Registry("source")
STORES: Registry[object] = Registry("store")
NOTIFIERS: Registry[object] = Registry("notifier")
DASHBOARD_PROVIDERS: Registry[object] = Registry("dashboard")
