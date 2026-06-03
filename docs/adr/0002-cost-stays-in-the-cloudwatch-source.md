# 2. Cost stays inside the CloudWatch Source (one Source per assume-role seam)

- Date: 2026-06-04
- Status: Accepted (carries the v0.2/v0.3 decision)

## Context

The CloudWatch Source spans three upstreams — metrics (`GetMetricData`), logs
(`FilterLogEvents`), and cost (Cost Explorer `GetCostAndUsage` + `DescribeBudget`, added in
v0.3). A deepening review asked whether the cost path should be split into a separate
`cloudwatch-cost` Source, since the 758-LOC module's deletion test reads as ambiguous
(deep, or a grab-bag?).

All three paths share **one** assume-role / STS credential seam: the same per-env
`PanoptesReadRole` ARN, ExternalId, and session. The credential machinery is four private
methods (`_resolve_credentials`, `_assume_role`, `_sts`, `_session`).

## Decision

Keep the cost path **inside** `CloudWatchSource`. Extract only the *cadence* concern into a
`_PollGate` value type (the actual friction — `_last_cost_read_at` + `_cost_due()` buried
among metric/log normalization). Do **not** split into a separate Source.

`capabilities()` stays `{METRIC, LOG}` — cost gauges are `MetricSignal`s discriminated by
the `panoptes_cost_*` name prefix; adding a `COST` `SignalKind` would make `capabilities()`
config-dependent, which the Source Protocol does not support.

## Consequences

- One assume-role/STS seam → no duplicated credential machinery, and no second
  `health()` firing a second STS probe per cycle for one logical AWS identity.
- No breaking config change (cost stays opt-in via `cost_budget_name` in the single
  cloudwatch block).
- A future split must first solve the shared-credential problem (e.g. a credential-provider
  object shared across two Sources) before it is warranted; absent that, this ADR closes it.
