# Guarded deployment promotion

Known-good promotion is the only path that may move Repo B `main` after a live
candidate deployment. It consumes the immutable deployment-finalization record
and never infers success from individual observation rows.

## Authority and durable intent

Only an integrity-valid finalization with outcome `success` authorizes a
promotion plan. Schema v23 journals that plan before any GitHub request. It
binds the deployment ID, trusted private repository identity, prior `main` SHA,
exact candidate SHA, verified backup slug, finalization digest, deterministic
known-good tag, phase, bounded block classification, and timestamps.

Missing, failed, incomplete, tampered, rebound, or stale authority fails closed.
A completed plan replays without credentials or network access. Deterministic
candidate, `main`, or tag divergence is durably blocked so Retrigger cannot
blindly repeat the rejected operation. Transport, timeout, GitHub, and storage
availability failures remain incomplete and retryable under the existing work
backoff policy.

## Ref safety and recovery

Immediately before publication the transport re-proves that Repo B is the
configured private repository with the recorded numeric identity, then reads
the exact `candidate`, `main`, and known-good tag refs. The candidate must still
equal the observed SHA. `main` may be only the recorded baseline or the exact
candidate, and the tag may be only absent or already point to the exact
candidate.

The transport never force-updates a ref. A missing `main` advancement uses the
GitHub ref API with `force: false`; a missing lightweight tag is created once.
Every write response and the final remote state are checked against the exact
authorized candidate. If execution stops between the branch and tag writes,
the durable plan recognizes the safe partial state and publishes only the
missing ref. If both refs already match, recovery records completion without a
second write.

Concurrent divergence, a moved candidate, or a conflicting tag is never
merged, overwritten, or deleted. Promotion performs no Home Assistant,
Supervisor, backup, Apply, restart, observation, or rollback mutation and does
not erase rejected-candidate history.
