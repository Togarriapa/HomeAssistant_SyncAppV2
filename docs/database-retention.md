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

The planner performs no filesystem deletion, Git operation, network operation, Repo B ref mutation, history rewrite, or Home Assistant/Recorder mutation. Any cleanup or history-maintenance transport remains a separate future increment and must be independently guarded and re-audited before gaining mutation authority.

Candidate Fetch/Stage, validation, backup, Apply, reload/restart, observation, promotion, tagging and rollback remain separate controlled deployment capabilities. The Retrigger Work Cron Job remains enabled and independent.
