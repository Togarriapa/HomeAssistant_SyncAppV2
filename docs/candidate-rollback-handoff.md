# Candidate rollback handoff recovery

This branch isolates the remaining MVP terminal handoff described by parent story #336.

## Scope

A failed immutable candidate finalization must be converted into the existing durable rollback-recovery authority. The handoff itself must not execute a restore. The existing rollback recovery lane remains solely responsible for later restore and reconciliation work.

## Acceptance contract

- Claim only exact `candidate_rollback` work created by failed finalization.
- Reconstruct the canonical deployment-bound observation plan from durable evidence.
- Re-prove failed finalization before creating rollback authority.
- Reuse the existing rollback authorization primitive rather than adding a parallel authority model.
- Complete the handoff only after durable rollback authority exists.
- Replay completed authorization without external reads.
- Recover stale claims, retry transient unavailability with bounded backoff, and block deterministic mismatch.
- Preserve one candidate action per Retrigger cycle.
- Expose only aggregate work status in runtime information.
- Cover crash windows, replay, tampering, concurrency, ordering, and privacy with RED-before-GREEN tests.
- Pass repository quality and native architecture CI before merge.

## Safety boundary

This task creates only durable recovery authority. It does not execute a restore, Apply, restart, promotion, or any live Home Assistant mutation.
