# Pre-Apply Repo B freshness gate

The sole product specification for this gate is the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

`reprove_preapply_repo_heads()` is a read-only safety gate between prepared candidate/backup evidence and any future Apply authorization. A persisted `PreparedDeployment` and a previously re-proven `CandidateBackupEvidence` are prerequisites, not deployment permission.

## Contract

The gate:

- validates the exact persisted `PreparedDeployment` and requires the supplied backup evidence to match it before GitHub I/O;
- uses the existing trusted private-Repo-B branch-head boundary, pinned to the immutable repository ID already bound to the prepared deployment;
- reads exactly `main` and `candidate`;
- requires `main` to remain exactly at the prepared baseline SHA;
- requires `candidate` to remain exactly at the prepared candidate SHA;
- rejects missing branches, moved heads, repository-identity drift, public or mismatched repositories, malformed metadata, missing credentials and transport/API verification failures;
- re-validates the prepared/backup binding after GitHub I/O so in-process evidence replacement cannot be accepted;
- returns immutable evidence bound to Repo B target and repository ID, baseline SHA, candidate SHA, backup slug, Stage manifest SHA-256, runtime SHA-256, risk level and Home Assistant Core version;
- exposes only sanitized failure messages and never propagates GitHub response bodies, credentials or nested exception text.

A moved `main` or `candidate` head is deterministic stale evidence. It must fail closed and must not be treated as endlessly retryable work by a future recovery layer.

## Explicit non-authority

Successful freshness re-proof still does **not** authorize Apply. This gate performs no write to `/homeassistant`, no reload/restart, no Supervisor backup creation or restore, no Git ref mutation, no promotion or tagging, no observation and no rollback.

A later controlled transaction must independently establish the authority and lifecycle for Apply, reload/restart, observation, promotion on success, and rollback/rejection on failure while preserving all validation and backup safeguards required by the initial V2 README.

The recurring Retrigger Work mechanism remains enabled and is not changed by this gate.
