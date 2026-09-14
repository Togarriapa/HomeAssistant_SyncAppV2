# Candidate Stage pre-write re-proof

Specification source: the initial V2 root `README.md` at `71d284ce447d79b044e332c9bc01ae801dc91947`.

`reprove_stage_for_apply()` is a side-effect-free integrity gate. It accepts one producer-issued Apply authorization and the isolated candidate Stage bound to that authorization.

The gate requires exact equality for the private Repo B target, immutable repository ID, candidate commit SHA, and Stage manifest SHA-256. It then calls the existing `verify_candidate_stage()` verifier, which checks the Stage boundary, canonical manifest bytes and digest, entry metadata, safe paths, and staged file contents against the recorded evidence.

Authorization and Stage bindings are snapshotted before verification and checked again afterwards. Binding drift during verification fails closed.

Successful verification returns immutable, producer-confined `StagePrewriteEvidence` containing the deployment ID, repository identity, candidate SHA, and Stage manifest digest. This is ephemeral evidence for one exact later deployment attempt. It is not persisted authority and does not mark the candidate successful, observed, known-good, or promoted.

This gate makes no changes to the running Home Assistant configuration, Supervisor state, backups, Git refs, or Home Assistant process lifecycle. The later deployment stages remain responsible for controlled application, restart or reload where required, observation, result recording, promotion only after success, and rollback on failure.

Deterministic Stage integrity or binding failures are blocked rather than retried indefinitely. The recurring Retrigger Work mechanism remains enabled and unchanged.
