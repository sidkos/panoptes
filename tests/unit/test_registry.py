"""Phase 1 unit tests for the typed adapter registry (`core/registry.py`).

Covers (spec `## API Surface` / playbook Phase 1 table):
- `register` records the adapter class and returns it unchanged;
- `build(type, config)` constructs the adapter from a config block;
- an **unknown type fails fast** with `UnknownAdapterError` whose message lists the
  available types;
- a registered class satisfying the plane Protocol type-checks (the typed fake
  adapter below is annotated as the plane Protocol, so mypy `--strict` enforces
  structural conformance at type-check time).

No Phase 2-6 adapters exist yet, so a self-contained typed fake source is used.
"""

from collections.abc import Mapping
from datetime import UTC

import pytest
from core.errors import UnknownAdapterError
from core.model import CanonicalSignal, SignalKind, SourceHealth, TimeWindow
from core.planes.source import Source
from core.registry import Registry

ConfigBlock = Mapping[str, str | int | bool | list[str]]


class _FakeSource:
    """A minimal in-memory adapter that structurally satisfies `Source`.

    Adapter construction convention (Phase 1, followed by all later phases):
    the registry instantiates a registered class with its config block as the
    single positional argument — `cls(config)`.
    """

    type = "fake"
    # Default outage-fetch opt-out (most sources skip fetch when unreachable — F3a).
    fetch_when_unreachable = False

    def __init__(self, config: ConfigBlock) -> None:
        self.config = config

    def capabilities(self) -> set[SignalKind]:
        return {SignalKind.METRIC}

    def fetch(self, window: TimeWindow) -> list[CanonicalSignal]:
        return []

    def health(self) -> SourceHealth:
        from datetime import datetime

        return SourceHealth(reachable=True, detail="ok", checked_at=datetime.now(UTC))


def _typed_fake_satisfies_source_protocol() -> Source:
    """Compile-time assertion: `_FakeSource` is a structural `Source`.

    The annotated return type makes mypy `--strict` verify conformance; if
    `_FakeSource` ever drifts from the Protocol, type-check fails here.
    """
    return _FakeSource({})


def test_register_returns_class_unchanged() -> None:
    registry: Registry[Source] = Registry("source")
    returned = registry.register("fake")(_FakeSource)
    assert returned is _FakeSource


def test_build_constructs_registered_adapter() -> None:
    registry: Registry[Source] = Registry("source")
    registry.register("fake")(_FakeSource)
    adapter = registry.build("fake", {"url": "http://x", "timeout": 30})
    assert isinstance(adapter, _FakeSource)
    assert adapter.config["url"] == "http://x"
    assert adapter.capabilities() == {SignalKind.METRIC}


def test_build_unknown_type_fails_fast_and_lists_available() -> None:
    registry: Registry[Source] = Registry("source")
    registry.register("fake")(_FakeSource)
    registry.register("other")(_FakeSource)
    with pytest.raises(UnknownAdapterError) as excinfo:
        registry.build("nope", {})
    message = str(excinfo.value)
    assert "nope" in message
    assert "source" in message
    # The available types must appear so the operator can self-correct.
    assert "fake" in message
    assert "other" in message


def test_protocol_conformance_fixture_constructs() -> None:
    source = _typed_fake_satisfies_source_protocol()
    assert source.type == "fake"
