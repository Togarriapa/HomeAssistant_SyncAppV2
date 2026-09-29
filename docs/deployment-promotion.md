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

A proof-bound deploy-key alternative now exists for later runtime adoption. It
reconciles the same safe states through fresh SSH reference proofs and publishes
both missing refs in one atomic, non-force Git push. It is intentionally not
selected by the production lane yet; see
[Repo B deploy-key promotion transport](deploy-key-promotion-transport.md).

## Production recovery

Retrigger claims only exact `candidate_promote` work created atomically by a
successful immutable finalization. The executor reconstructs the canonical
candidate-bound plan and re-proves prepared deployment and finalization before
the promotion primitive can read GitHub. A completed durable promotion then
completes the exact work item in a separate guarded transaction; a crash in
between replays without credentials or network access.

Stale running work is recovered under the shared bounded backoff policy, and at
most one promotion is considered after every earlier candidate lane is idle.
Transport and timeout failures remain retryable. Invalid credentials, authority,
evidence, repository identity, or ref divergence block the unchanged work. The
recovery result contains only fixed status/action fields and never returns the
repository, candidate SHA, tag, credential, response body, or nested error.
