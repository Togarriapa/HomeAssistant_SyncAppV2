# Crash-safe single-operation live Apply writer

The sole product specification for this capability is the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Authority boundary

`apply_live_operation()` may execute one exact, ordered `LiveApplyOperation` only after re-validating the complete producer-issued chain: prepared deployment, durable Apply intent, deterministic Apply plan, isolated Stage evidence and live preconditions. Scalar paths, hashes or caller-provided bytes do not grant authority.

The writer intentionally does not reload or restart Home Assistant, accept an observation result, promote or tag Git history, restore a backup, or orchestrate rollback. Those remain later controlled-deployment stages.

## Journal and recovery ordering

Before any filesystem mutation, the writer durably records `mutation_started` for the exact operation. Failure to persist that transition prevents mutation.

A restart or process interruption while `mutation_started` is durable is classified as `reconcile_uncertain`. Retrigger must not execute the operation again blindly. This applies both before a write and after a write whose postcondition or durable `mutation_verified` transition was not completed.

After the exact postcondition is proven, the writer persists `mutation_verified`. Replaying that exact verified operation is idempotent and performs no second mutation. A durable `blocked` operation remains terminal.

## Filesystem safety

Every target is resolved beneath the verified Home Assistant root through safe relative path components. Root and nested parent directory device/inode identities are captured immediately before the final proof and rechecked at mutation time. Symlinks, traversal, `.git`, special files and parent replacement races fail closed.

Candidate file bytes are read only from the verified isolated Stage and are rechecked against the authorized Git object identity before use.

- Added files use a private, flushed temporary file and a hard-link no-clobber commit.
- Modified files use a reversible atomic exchange. The displaced object must match the exact authorized baseline object ID and mode. A proven mismatch is safely reversed and blocked; an unproven reversal remains uncertain.
- Deleted files are atomically displaced to a private tombstone and verified before removal. Safe restoration converts a mismatch to a deterministic block; restoration or cleanup uncertainty requires reconciliation.
- Mode-only changes operate through a descriptor that is verified against the baseline object and mode. Any live-name change after mutation begins is uncertain.

The containing directory is synchronized after committed filesystem changes. Postcondition verification proves the exact expected absence, bytes and mode before durable success.

## Diagnostics

Failures expose bounded, generic diagnostics. Candidate contents, Home Assistant secrets, credentials and raw nested exception text are neither logged nor persisted by this boundary.

Retrigger Work remains enabled. Its deployment consumer must honor durable recovery classification and never reinterpret `mutation_started` as retry authority.
