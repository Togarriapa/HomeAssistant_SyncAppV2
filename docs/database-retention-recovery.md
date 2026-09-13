# Recorder retention recovery

Recorder retention is a dedicated durable work lane named `database_retention`. It is independent from routine snapshot publication while remaining sequenced immediately after that publication in the normal Recorder service and Retrigger cycle.

## Identity and discovery

Discovery reads complete database history from a freshly verified private Repo B `database` head and computes the configured retention plan using explicit UTC time. The durable key is a SHA-256 digest over the pinned repository ID, case-normalized target, exact head, branch, retention-days setting, and complete retained/prunable partition. Tokens, paths and raw commit identities are not exposed by runtime inventory.

Rediscovering the same exact head and plan returns the existing StateStore item. A newly published snapshot or a changed retention outcome produces a new identity. Discovery itself does not write Git refs or Home Assistant data.

## Execution and recovery

StateStore remains the sole locking and lifecycle boundary. Each pass claims at most one `database_retention` item, and Retrigger recovers work left `running` by an interrupted service before it can be reclaimed. The original item creation time is retained as deterministic policy evidence while StateStore controls attempt count and exponential backoff.

Execution reconstructs trusted history evidence, requires the current `database` head to match the last successful generated-database baseline, performs the fresh prewrite repository/head proof, and creates a non-persisted replacement authorization. When pruning is required, the exact history is fetched into a per-attempt mode-0700 staging repository. Authentication uses ephemeral askpass files; credentials are removed before the repository can leave staging preparation. The authorization and rebuilt artifact exist only in memory and disposable staging state.

The previously reviewed replacement transport then proves the rebuilt commit bytes and performs only an exact-head-leased update of `refs/heads/database`. After success, the synchronization baseline is advanced to the rebuilt head while preserving the latest snapshot identity. A no-op plan completes without staging credentials or ref mutation.

Immediately before the leased update, StateStore durably records a non-privileged publication intent containing only the work key, pinned repository identity, expected and replacement head SHAs, latest snapshot identity, and timestamp. It never stores the token, staging path, authorization, replacement artifact, or exception text. If the process is interrupted after GitHub accepts the update but before local completion, the next retry verifies the live private-repository head: the exact replacement advances the local baseline and completes without another push, the unchanged expected head safely rebuilds the same deterministic replacement, and any third head is blocked as stale.

## Failure policy

- Transient GitHub, network, local Git, timeout and unavailable-staging failures enter bounded StateStore retry/backoff.
- Invalid credentials, evidence, staging configuration and repository identity are deterministic and blocked.
- Invalid history, forged evidence, missing generated-database baseline and authorization failures are blocked.
- A moved head or work identity mismatch is stale and blocked; later discovery may create work only from newly verified evidence.
- A rejected lease-protected update is blocked and never continuously retriggered.

All lifecycle outcomes automatically appear in bounded `analysis/recovery.json` aggregates by work kind, status, attempt count, readiness and backoff. The report structurally excludes work keys, tokens, staging paths and raw exception text.

## Safety boundary

Database retention remains Home Assistant to GitHub only. It never restores Recorder data into Home Assistant and cannot mutate `main`, `candidate`, `runtime` or `logs`. Candidate fetching, isolated staging, validation, backup, controlled apply, observation, promotion and rollback remain unchanged.
