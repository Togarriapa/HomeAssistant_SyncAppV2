# Database history replacement safety boundary

This document records the narrow Recorder/database retention mutation boundary derived from the initial V2 README at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Scope

The `database` branch is one-way Home Assistant → Repo B snapshot history. Retention may prune old snapshot history, but retention never grants authority to deploy database content back into Home Assistant and never grants authority over `main`, `candidate`, `runtime`, or `logs`.

`build_database_history_replacement()` and `replace_database_history()` implement only the history-replacement transport required after a trusted retention plan has already been produced and freshly authorized. Service orchestration and durable Retrigger scheduling are separate increments.

## Generated replacement artifact

A caller cannot nominate an arbitrary replacement Git commit SHA. The builder consumes an exact `DatabaseHistoryReplacementAuthorization` and an isolated SyncApp staging Git repository containing the authorized commit objects.

For every retained snapshot commit, oldest to newest, the builder:

1. reads the exact commit object named by the authorization;
2. verifies its original linear parent is exactly the parent implied by the authorized retained/pruned history;
3. preserves the snapshot tree and ordinary commit metadata/message;
4. removes obsolete signature/merge metadata that would no longer be valid after ancestry changes;
5. rewrites only the parent link, making the oldest retained commit a new root and each newer retained commit point to the previously rebuilt retained commit;
6. writes only generated commit objects into the staging repository object database, without changing any local or remote ref.

The resulting head is returned inside a `DatabaseHistoryReplacementArtifact` bound to the staging repository, Repo B target and repository ID, `database` branch, expected pre-mutation head and retained commit set. The object itself is not treated as a security boundary: callers can forge Python objects, so publication authority is established again from Git object content immediately before mutation.

## Mutation-time proof and atomic publication

Before any remote mutation, the transport independently re-reads both the authorized original retained commits and the proposed rebuilt commit chain from the staging Git object database. It requires every proposed commit tree to equal the corresponding authorized snapshot tree, requires the original retained ancestry to match the authorization, requires the rebuilt history to remain linear, and requires the oldest retained snapshot to be a new root. A valid-looking arbitrary SHA or a forged `DatabaseHistoryReplacementArtifact` therefore cannot gain publication authority.

After that local proof, immediately before publication, the transport re-fetches authenticated GitHub metadata through `fetch_trusted_branch_head()` and therefore re-proves that Repo B is private, has the pinned repository ID, and still has the exact authorized `database` head.

Only then may the artifact be published. The push targets exactly `refs/heads/database` and uses `--force-with-lease=refs/heads/database:<expected-head>`. A concurrent branch update therefore fails closed. The transport never writes `main`, `candidate`, `runtime`, `logs`, Home Assistant configuration, or Recorder files.

A retention no-op returns without requiring an artifact, token, staging repository or Git/GitHub mutation.

## Failure classification for Retrigger integration

All externally surfaced messages are sanitized. `DatabaseHistoryReplacementTransportError.kind` provides stable machine-readable policy information for the later durable Retrigger increment:

- `invalid`: malformed/forged authorization or artifact, invalid staging state, missing/corrupt authorized commit objects, invalid repository configuration/identity, or other deterministic evidence failure. Do not continuously retrigger.
- `stale`: the authenticated `database` head moved after authorization. Re-plan from fresh evidence; do not publish the old artifact.
- `transient`: local transport/timeout errors or GitHub transport, throttling, or server failures. This class is eligible for controlled bounded retry/backoff.
- `rejected`: Git refused the final lease-protected push. Treat the artifact as unpublished and require policy-aware investigation/re-proof before another attempt.

Only `transient` sets `retryable=True`. This deliberately prevents deterministic invalid/stale/rejected work from becoming an infinite Retrigger loop.

## Candidate deployment isolation

This retention transport does not alter candidate staging, configuration validation, backup creation, deployment observation, promotion, rollback, rejected-candidate SHA tracking, or any other candidate deployment safeguard from the initial V2 README. The recurring Retrigger mechanism remains enabled; integration of this transport into its durable work lanes is intentionally deferred to a separate task.
