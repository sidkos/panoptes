"""Panoptes core — the consumer-agnostic monitoring meta-layer.

This package KNOWS NOTHING about any consumer. Consumer packs are injected at
runtime through the registries below; ``core`` never imports from ``examples/``
(enforced structurally by ``tests/unit/test_core_purity_guard.py``).
"""

from core.registry import DASHBOARD_PROVIDERS, NOTIFIERS, SOURCES, STORES

__all__ = ["DASHBOARD_PROVIDERS", "NOTIFIERS", "SOURCES", "STORES"]
