# Recorder snapshot retention

This capability is derived only from the initial V2 root README at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The `database` branch is generated from Home Assistant and remains a one-way Home Assistant -> GitHub data source. Recorder snapshots are not a deployment source and this retention work grants no authority to modify or restore the live Recorder database from GitHub.

## Configuration contract

`recorder_retention_days` controls the intended historical Recorder snapshot retention window.

- Default: `7` days, matching the README's initial approximately seven-day policy.
- Allowed range: `1` through `365` days.
- The value must be a JSON integer; booleans, strings, floating-point values, zero, negative values and larger values are rejected.
- Invalid input is rejected without echoing the supplied value into the error text.

## Current increment

This increment adds only the validated configuration contract and Home Assistant App schema. It does not delete database snapshots, rewrite Git history, modify Repo B refs, or mutate Home Assistant.

Any later retention planner or cleanup transport must be implemented as a separate tested increment. It must use explicit timezone-aware UTC time and deterministic snapshot evidence, remain confined to generated `database` data, preserve a usable newest snapshot, fail closed on malformed or unexpected state, and be re-audited against the initial README before gaining mutation authority.

Candidate Fetch/Stage, validation, backup, Apply, reload/restart, observation, promotion, tagging and rollback remain separate controlled deployment capabilities. The Retrigger Work Cron Job remains enabled and independent.
