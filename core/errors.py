"""Panoptes core exception hierarchy.

All Panoptes-raised errors subclass `PanoptesError` so callers (the collector, the
MCP tools, the config loader) can catch the family with one `except`. Messages are
built with f-strings (Panoptes convention — never `%`-formatting), and each error
carries the structured fields that make it actionable: `UnknownAdapterError` lists
the available adapter types so an operator can self-correct a typo; `MissingEnvVarError`
names the unset variable; `CapabilityError` carries the human-readable detail a
read-only tool surfaces instead of returning a silent-empty result.
"""

from collections.abc import Iterable


class PanoptesError(Exception):
    """Base class for every error Panoptes raises deliberately."""


class UnknownAdapterError(PanoptesError):
    """Raised when a config block names an adapter `type` no registry knows.

    The message lists the available types for the plane so a misconfiguration is
    self-correcting (spec ADAPTERS §3 "no adapter for type X" fails fast).
    """

    def __init__(self, plane: str, type: str, available: Iterable[str]) -> None:
        # Sort for a deterministic, scannable message regardless of registration order.
        self.plane = plane
        self.type = type
        self.available = sorted(available)
        available_list = ", ".join(self.available) if self.available else "(none registered)"
        super().__init__(
            f"No {plane} adapter registered for type '{type}'. "
            f"Available {plane} types: {available_list}."
        )


class MissingEnvVarError(PanoptesError):
    """Raised when `${VAR}` interpolation references an unset environment variable.

    The message names the missing variable so the operator knows exactly what to
    export (spec `## Configuration` — secrets live in env, never inlined).
    """

    def __init__(self, var: str) -> None:
        self.var = var
        super().__init__(
            f"Required environment variable '{var}' is referenced in config "
            f"but is not set in the process environment."
        )


class CapabilityMismatchError(PanoptesError):
    """Raised when a source's declared `provides:` disagrees with `capabilities()`.

    A config-time fail-fast: `capabilities()` is authoritative, so a declared
    `provides:` token set that does not match it — or an unknown `provides:` token —
    is an operator error the loader surfaces by name. Subclasses `PanoptesError` (not
    stdlib `ValueError`) so a caller wrapping `load_config` in `except PanoptesError`
    catches a config mismatch alongside every other deliberate Panoptes error, rather
    than having it escape the family (spec ADAPTERS §3 — fail fast within the
    `PanoptesError` hierarchy).
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class CapabilityError(PanoptesError):
    """Raised when a required source/capability is not configured or available.

    Surfaced explicitly by read-only stores/tools instead of returning a silent
    empty result (spec `## API Surface` — "Fail explicitly … never a silent empty
    result").
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)
