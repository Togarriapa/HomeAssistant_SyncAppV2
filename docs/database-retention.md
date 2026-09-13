# Recorder snapshot retention

This capability is derived only from the initial V2 root README at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The `database` branch is generated from Home Assistant and remains a one-way Home Assistant -> GitHub data source. Recorder snapshots are not a deployment source and this retention work grants no authority to modify or restore the live Recorder database from GitHub.

## Configuration contract

`recorder_retention_days` controls the intended historical Recorder snapshot retention window.

- Default: `7` days, matching the README's initial approximately seven-day policy.
- Allowed range: `1` through `365` days.
- The value must be a JSON integer; booleans, strings, floating-point values, zero, negative values and larger values are rejected.
- Invalid input is rejected without echoing the supplied value into the error text.

## Deterministic planning contract

Retention planning is a side-effect-free classification step and is restricted to the exact `database` branch.

- The caller supplies an explicit timezone-aware UTC reference time and the already validated retention-days value; the planner does not read the wall clock.
- Each snapshot is represented by an immutable identity and timezone-aware UTC creation timestamp.
- Evidence must be bounded, ordered newest-first, contain no duplicate or malformed identities, and contain no future timestamps.
- Snapshots at or newer than the cutoff are retained; older snapshots are classified as prunable.
- The newest valid snapshot is always retained, even when every supplied snapshot is older than the configured cutoff.
- The planner cannot target `main`, `candidate`, `runtime`, or `logs`.

## Trusted history evidence

Before a future cleanup or history-maintenance capability may rely on a retention plan, the plan must be bound to complete history evidence for one already verified private Repo B `database` head.

- The trusted branch head carries the configured repository target, pinned repository ID, exact `database` branch and expected head SHA.
- History is non-empty and its first record must equal that exact verified head.
- Evidence must describe a complete linear parent chain through the root. Merge commits, broken parent links and truncated history fail closed.
- History input is bounded to the same maximum evidence set used by the deterministic planner.
- Each validated commit becomes immutable snapshot evidence using its commit SHA and commit timestamp, then the existing retention planner determines the retained/prunable partition.
- The resulting trusted evidence keeps the repository identity and expected head alongside the retention plan so later capabilities cannot detach a plan from the history it classified.

## Read-only history collection

History collection re-verifies the configured private repository ID and exact `database` branch head before requesting commit metadata. It then reads from that immutable head SHA, never from the moving branch name.

- GitHub responses and the complete evidence set are bounded before retention planning.
- Only commit SHA, committer timestamp and parent SHA metadata are retained.
- Duplicate JSON fields, malformed responses, invalid timestamps and transport failures fail closed with sanitized errors.
- Complete-history validation and retention classification remain delegated to the trusted-evidence and deterministic-planning layers.

## Prewrite re-verification boundary

`reprove_database_history_prewrite()` is the trusted boundary that produces the prewrite proof consumed by replacement authorization. Immediately before authorization, it reuses the private Repo B verifier with the pinned repository ID and the exact `database` branch.

The freshly verified repository target, repository ID, branch and head SHA must still exactly match the immutable trusted history evidence. The evidence itself must remain internally consistent: both evidence and plan are `database` scoped, history is non-empty, and its first record equals the expected head. A moved head, changed repository identity, wrong branch, malformed evidence, or repository-verification failure fails closed. Verification failures are translated to sanitized domain errors so token or transport exception text is not exposed.

`TrustedDatabaseHistoryPrewrite` is therefore evidence produced by this explicit re-verification step; callers must not treat manual construction of matching fields as freshness evidence.

## Replacement authorization boundary

`authorize_database_history_replacement()` is a side-effect-free gate for a future cleanup transport. It accepts only immutable trusted database history plus a freshly re-proved identity/head proof for the same private repository and exact `database` head.

The gate revalidates the deterministic retention plan from the original explicit UTC reference time and retention-days value, requires the retained/prunable identities to form the exact complete history partition, requires the current head to remain the first retained commit, and fails closed on identity, branch or head divergence. A no-op plan is represented explicitly by `requires_replacement == false` and grants no replacement authority.

The prewrite verifier performs only repository/head verification, and authorization itself performs no network request, Git command, ref mutation, filesystem deletion, database restore, or Home Assistant mutation. Any future mutation transport must perform an atomic expected-head-bound update restricted to `database`; service integration remains separate reviewed work.

Candidate Fetch/Stage, validation, backup, Apply, reload/restart, observation, promotion, tagging and rollback remain separate controlled deployment capabilities. The Retrigger Work Cron Job remains enabled and independent.
