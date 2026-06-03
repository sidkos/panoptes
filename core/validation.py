"""Plane-neutral typed config-block field extraction.

The registry hands every adapter — across *all* planes (source / store / notifier /
dashboard) — a flat `ConfigBlock` (`Mapping[str, str | int | bool | list[str]]`).
These helpers pull a single field out with a precise runtime type check, raising a
clear `PanoptesError` (naming the adapter + field) when the field is missing, the
wrong type, or empty — so a misconfiguration fails fast at construction rather than
surfacing as a confusing downstream error.

These extractors originally lived in `core/sources/_config.py`, scoped to the
sources plane. They were lifted here because the check is genuinely plane-neutral:
the VM store needs the same `url` validation, and a store importing from
`core.sources` would be a wrong cross-plane dependency. Centralizing the checks in a
plane-neutral module keeps every adapter DRY and the error messages uniform without
any cross-plane coupling.
"""

from core.errors import PanoptesError
from core.registry import ConfigBlock


def require_str_field(config: ConfigBlock, field: str, adapter_type: str) -> str:
    """Return a required non-empty string config field or raise `PanoptesError`."""
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise PanoptesError(
            f"The '{adapter_type}' adapter requires a non-empty string '{field}' in its "
            f"config block; got {value!r}."
        )
    return value


def optional_str_field(config: ConfigBlock, field: str) -> str | None:
    """Return a string config field if present and non-empty, else `None`.

    An absent key, an empty string, or a non-string value all collapse to `None` —
    the field is genuinely optional (e.g. `assume_role_arn`, `external_id`), so a
    missing or blank value simply means "not configured", not an error.
    """
    value = config.get(field)
    if isinstance(value, str) and value:
        return value
    return None


def require_str_list_field(config: ConfigBlock, field: str, adapter_type: str) -> list[str]:
    """Return a required `list[str]` config field or raise `PanoptesError`.

    Every element must be a string (the flat `ConfigBlock` value type permits a
    `list[str]`); a non-list value or a non-string element fails fast.
    """
    value = config.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PanoptesError(
            f"The '{adapter_type}' adapter requires a list-of-strings '{field}' in its "
            f"config block; got {value!r}."
        )
    return list(value)
