"""Unit tests for the plane-neutral `core.validation` config-field extractors.

These helpers previously lived in `core/sources/_config.py` (scoped to the sources
plane). They were lifted to `core.validation` so any plane — sources, stores,
notifiers, future adapters — can share the same typed extraction + uniform
`PanoptesError` shape without a wrong cross-plane dependency. The cases below pin
the happy / missing / wrong-type / empty behavior of each helper directly against
the new module, independent of any one adapter.
"""

from typing import cast

import pytest
from core.errors import PanoptesError
from core.registry import ConfigValue
from core.validation import (
    optional_str_field,
    require_str_field,
    require_str_list_field,
)


def test_require_str_field_returns_present_non_empty_string() -> None:
    assert require_str_field({"url": "http://vm:8428"}, "url", "victoriametrics") == (
        "http://vm:8428"
    )


def test_require_str_field_missing_key_raises_naming_adapter_and_field() -> None:
    with pytest.raises(PanoptesError) as excinfo:
        require_str_field({}, "url", "victoriametrics")
    message = str(excinfo.value)
    assert "victoriametrics" in message
    assert "url" in message


def test_require_str_field_empty_string_raises() -> None:
    with pytest.raises(PanoptesError):
        require_str_field({"url": ""}, "url", "victoriametrics")


def test_require_str_field_wrong_type_raises() -> None:
    with pytest.raises(PanoptesError):
        require_str_field({"url": 8428}, "url", "victoriametrics")


def test_optional_str_field_returns_value_when_present() -> None:
    assert optional_str_field({"profile": "prod"}, "profile") == "prod"


def test_optional_str_field_absent_key_returns_none() -> None:
    assert optional_str_field({}, "profile") is None


def test_optional_str_field_empty_string_collapses_to_none() -> None:
    assert optional_str_field({"profile": ""}, "profile") is None


def test_optional_str_field_non_string_collapses_to_none() -> None:
    assert optional_str_field({"profile": 7}, "profile") is None


def test_require_str_list_field_returns_copy_of_string_list() -> None:
    source = ["a", "b"]
    result = require_str_list_field({"log_groups": source}, "log_groups", "cloudwatch")
    assert result == ["a", "b"]
    # A fresh list is returned, not the caller's reference.
    assert result is not source


def test_require_str_list_field_non_list_raises() -> None:
    with pytest.raises(PanoptesError) as excinfo:
        require_str_list_field({"log_groups": "not-a-list"}, "log_groups", "cloudwatch")
    message = str(excinfo.value)
    assert "cloudwatch" in message
    assert "log_groups" in message


def test_require_str_list_field_non_string_element_raises() -> None:
    # A list with a non-string element is invalid at runtime even though the static
    # `ConfigValue` union nominally permits only `list[str]`; the runtime check is
    # exactly what guards against a malformed YAML interpolation. `cast` the mixed
    # list to `ConfigValue` so mypy stays satisfied without `Any` while still
    # exercising the runtime non-string-element branch.
    malformed_value = cast(ConfigValue, ["ok", 3])
    with pytest.raises(PanoptesError):
        require_str_list_field({"log_groups": malformed_value}, "log_groups", "cloudwatch")
