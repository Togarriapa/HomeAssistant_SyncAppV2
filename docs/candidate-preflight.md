# Routine candidate preflight

The routine candidate service advances one freshly pending candidate action at a
time. It runs inside the state-owning service process after candidate detection,
on a one-second monotonic cadence. Startup waits one full interval before the
first tick, and a delayed tick coalesces missed intervals instead of creating a
catch-up queue.

## Normal-work admission

Each tick may claim at most one due `candidate` work item whose status is exactly
`pending` and whose attempt count is below the shared maximum. Claiming is atomic.
The pinned Repo B identity and exact orchestration phase/action pair must match.
Work left `running`, `retry`, `blocked`, completed, exhausted, or already at
`semantically_validated/prepare_backup` is ineligible for this routine path.
An absent orchestration is registered only for a fresh detection and can begin
only at `detected/fetch_stage`.

The normal path uses the existing typed executors in this order:

1. `detected/fetch_stage`
2. `staged/analyze`
3. `integrity_verified/analyze_dependencies`
4. `dependencies_analyzed/classify_risk`
5. `risk_classified/validate`
6. `static_validated/validate_semantics`

Every successful executor persists its own checkpoint and exact successor. The
service returns the still-pending work item through the canonical defer transition
so a later tick claims the next action with a fresh attempt budget. The semantic
executor stops at `semantically_validated/prepare_backup`. Backup creation is a
separate gated operation.

## Failures and transport

Typed transient failures use the existing bounded retry/backoff policy and remain
for the separate Retrigger recovery path. Static and semantic deterministic
rejections atomically block the exact candidate and work item; routine processing
does not defer them. Fatal persistence or authority errors use fixed sanitized
service diagnostics.

Candidate Fetch/Stage uses exactly the configured token or proof-bound deploy-key
ingress. Selected SSH ingress receives no token and has no token-Git fallback.
Integrity analysis independently uses the configured GitHub token to verify the
candidate against trusted `main`. Dependency collection uses the Supervisor Core
token. Credentials and repository/commit identities are not included in routine
service results or diagnostics.

## Authority boundary

All fetch, analysis and validation workspaces are protected app-owned directories
outside `/homeassistant`. The normal processor does not invoke interrupted-work
recovery, retry, backup, Apply, reload/restart, observation, promotion, tagging or
rollback executors. The read-only Home Assistant mount and its permissions remain
unchanged. Retrigger continues to own recovery of interrupted and eligible retry
work, while the existing separate deployment gates remain responsible for every
post-validation action.
