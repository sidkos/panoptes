"""Backwards-compatible re-export shim for the source config-field extractors.

The typed config-field extractors (`require_str_field`, `optional_str_field`,
`require_str_list_field`) used to be defined here, scoped to the sources plane. They
were lifted to the plane-neutral `core.validation` module so stores / notifiers /
future adapters can share them without a wrong cross-plane dependency. This shim
re-exports them so the existing source imports (`from core.sources._config import
...`) keep working unchanged; new code should import from `core.validation` directly.
"""

from core.validation import (
    optional_str_field,
    require_str_field,
    require_str_list_field,
)

__all__ = [
    "optional_str_field",
    "require_str_field",
    "require_str_list_field",
]
