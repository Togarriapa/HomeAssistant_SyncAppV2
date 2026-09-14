# Candidate Apply authorization boundary

The sole product specification for this gate is the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

`authorize_candidate_apply()` is the final side-effect-free trust boundary before a later task may attempt live candidate mutation. It consumes only one exact persisted `PreparedDeployment`, its exact already re-proven `CandidateBackupEvidence`, and exact `PreApplyFreshnessEvidence` produced after fresh Repo B identity/head verification.

## Exact binding

Authorization requires equality across:

- deployment identity;
- private Repo B target and immutable repository ID;
- known-good `main` baseline SHA;
- candidate SHA;
- Supervisor backup slug;
- isolated Stage manifest SHA-256;
- runtime evidence SHA-256;
- deployment risk level;
- Home Assistant Core version.

Any mismatch fails closed. The privileged authorization object cannot be normally constructed directly; the reviewed producer is the intended authority boundary.

## Ephemeral evidence only

Apply authorization is intentionally ephemeral. It is not persisted as durable deployment authority and it does not classify a candidate as successful, observed, known-good, promoted, or safe to retry indefinitely.

A future Apply implementation must freshly re-verify the isolated Stage bytes against the authorized Stage manifest immediately before the first live write. It must then preserve the remaining initial-README lifecycle: controlled Apply, appropriate reload/restart, health observation, result recording, promotion only after success, and rollback/rejection on failure.

## Explicit non-authority

This gate performs no filesystem write to `/homeassistant`, no Supervisor mutation, no backup creation or restore, no Home Assistant reload/restart, no Git network/ref mutation, no promotion/tagging, no observation and no rollback.

Deterministic evidence mismatch is a blocked condition rather than an endlessly retryable transient failure. The recurring Retrigger Work mechanism remains enabled and is not changed by this gate.
