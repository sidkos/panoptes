# 1. Composition over inheritance for plug-plane adapters

- Date: 2026-06-04
- Status: Accepted

## Context

An architecture-deepening review proposed an `HttpSource` **base class** for the four
httpx Source adapters (prometheus, loki, sentry, http-health) to concentrate their shared
RestClient wiring + `env`-stamp + health scaffolding. A base/mixin was also one option for
concentrating the health-probe discipline across the six sources.

The four plug-planes (Source/Store/Notifier/Dashboard) are `@runtime_checkable Protocol`s,
not base classes. Adapters are concrete classes constructed by the registry via a single
positional `cls(config)` (+ injectable client kwargs). The codebase uses composition and
duck-typing throughout; there is no adapter inheritance anywhere.

## Decision

Concentrate shared adapter discipline in **free functions and held value types**, never a
base class for adapters:

- `core/sources/probe.py::probe_health(...)` — a free function the sources delegate to.
- `core/sources/cloudwatch.py::_PollGate` — a value type the source holds.
- `QueryContext.read_gauge` / `read_series` — methods on the context the tools call.

A `HttpSource` base class is **rejected**: it would add inheritance the plug-plane design
deliberately avoids, and most of its value is already captured (health by `probe_health`,
transport by the existing `RestClient`). The residual `env`-stamp is a one-line pattern not
worth a base. If transport sharing is ever wanted, use composition (a held `HttpScrape`
helper), not a base class.

## Consequences

- Adapters stay independent, registry-constructed, and mypy-`strict` under the
  Protocol contract — no hierarchy to reason about.
- Shared discipline (incl. the no-`str(exc)`-leak security invariant) is concentrated and
  unit-testable in isolation, without coupling the adapters.
- A future review re-proposing an `HttpSource`/adapter base class should be closed by this
  ADR unless it brings a force these free-function/composition seams cannot.
