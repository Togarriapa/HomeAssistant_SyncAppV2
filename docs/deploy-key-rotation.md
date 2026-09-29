# Repo B deploy-key rotation

SyncApp provides a narrow, explicit staged workflow for replacing one protected
Repo B deploy-key generation without silently discarding the previous working
credential. Rotation is not connected to startup or Retrigger and never runs on
a schedule. Every workflow is bound to one canonical request UUID, the configured
private repository target and numeric repository ID, and the exact active key
generation.

## Lifecycle

| Phase | Durable meaning | Permitted next action |
| --- | --- | --- |
| `preparing` | Rotation intent is journaled before replacement generation. | Reconcile or finish generation for the same request. |
| `prepared` | A protected candidate exists and its public enrollment metadata is available. | Enroll that exact public key, then explicitly verify it. |
| `verified` | The candidate passed the identity-bound, host-pinned, read-only access proof. | Explicitly activate it. |
| `activating` | Activation intent is durable; one or both directory renames may have completed. | Re-run activation to reconcile the exact interruption point. |
| `activated` | The verified candidate is active and the previous generation is retained. | Keep both GitHub keys until later transport/finalization review. |
| `blocked` | A deterministic generation, authentication, identity, host-key, or proof failure occurred. | Investigate; unchanged automatic or explicit verification replay is rejected. |

Preparation journals intent before invoking `ssh-keygen`. It creates the candidate
in a fixed protected sibling directory and returns only its canonical Ed25519
public key, SHA-256 fingerprint, and opaque generation identity. Private bytes and
private filesystem paths never enter results, options, logs, the rotation record,
or diagnostics. Replaying the same request is idempotent; a competing UUID or a
changed repository identity is rejected.

## Manual enrollment prerequisite

After `prepared`, enroll the returned public key in the exact Repo B through the
repository's **Settings → Deploy keys** control. Keep the existing GitHub deploy
key enrolled. The later production transport will require write permission for
non-force publication, but this rotation increment intentionally performs only a
read-only `git ls-remote --refs` proof. A successful proof establishes identity
and read access; it does **not** establish write permission.

Verification reuses the access-test boundary described in
[Repo B deploy-key access test](deploy-key-access-test.md). It first re-proves the
private repository's stable numeric identity with token-authenticated metadata,
then verifies GitHub's packaged Ed25519 host-key pin and opens exactly one bounded,
noninteractive SSH observation with the candidate key. The returned proof must
match the candidate generation, fingerprint, target, and repository ID exactly.

Known temporary network failures leave the workflow `prepared`, allowing a later
explicit controlled retry. Deterministic failures are persisted as `blocked` and
cannot loop through Retrigger. Raw command output, credentials, key content, ref
names, object IDs, and exception text are not persisted or returned.

## Activation and recovery

Activation first persists `activating`, then performs two protected same-parent
renames:

1. move the exact previous active generation to the fixed retained location;
2. move the exact verified candidate to the active location;
3. persist `activated` after both directories and their generation identities are
   re-verified.

Each rename is followed by a parent-directory `fsync`. Replaying activation safely
recognizes all three valid states: before either rename, between the two renames,
and after both renames but before the completion record. Any other combination,
generation rebinding, tampering, unsafe ownership/mode/link count, or malformed
record fails closed.

The old generation remains protected and available for a later reviewed rollback
or finalization workflow. Do not remove the prior key from GitHub after local
activation. The read-only
[deploy-key reference transport](deploy-key-reference-transport.md) can consume
an exact proof after re-inspecting the active generation; it does not authorize
candidate fetch or repository writes. Removal, cleanup of the retained
generation, write-capability verification, production transport integration,
runtime/UI wiring, and explicit Repo B initialization remain separate tasks
under story #388.

Rotation grants no synchronization, deployment, promotion, rollback, or Retrigger
authority. Existing token-backed Git transport and every validation, backup,
observation, locking, deterministic-blocking, and rollback safeguard remain
unchanged.
