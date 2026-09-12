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

This evidence step is still side-effect free. It performs no Git/network operation, ref mutation, history rewrite, filesystem deletion, database restore, or Home Assistant mutation. A future mutation-capable retention transport must be a separate reviewed increment and must re-prove its target and current head immediately before destructive history maintenance.

Candidate Fetch/Stage, validation, backup, Apply, reload/restart, observation, promotion, tagging and rollback remain separate controlled deployment capabilities. The Retrigger Work Cron Job remains enabled and independent.
