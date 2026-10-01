# Repo B deploy-key generation

SyncApp has a narrow, network-free boundary for creating and inspecting one
Ed25519 deploy-key generation. This is the first increment of the deploy-key
lifecycle in story #388; it does not enroll, rotate, or use the key.

`ensure_repo_b_deploy_key()` accepts an explicit final key directory whose
parent must already be owned by the current App user with mode `0700`. Before it
invokes the packaged `/usr/bin/ssh-keygen`, it durably writes a private
generation journal. Key generation happens in a sibling `0700` temporary
directory. SyncApp verifies the generated private/public correspondence,
canonical Ed25519 public-key encoding, file ownership, link counts, exact
permissions, bounded sizes, hashes, and SHA-256 fingerprint before it atomically
publishes the complete directory.

The final generation contains exactly:

| Entry | Mode | Purpose |
| --- | --- | --- |
| `private_key` | `0600` | App-only OpenSSH private key; never returned or logged |
| `public_key` | `0644` | Canonical enrollment-safe Ed25519 public key |
| `manifest.json` | `0600` | Generation identity, public metadata, and integrity hashes |

The returned immutable value contains only algorithm `ssh-ed25519`, the public
key, its `SHA256:` fingerprint, and an opaque generation UUID. It contains no
private bytes or private-key path. Repeated calls validate the protected files
and replay that public metadata without invoking `ssh-keygen` or changing the
generation.

An interruption before atomic publication leaves the journal and private
temporary directory in place and later calls fail closed. SyncApp does not
silently repair, delete, overwrite, or rotate this evidence. An interruption
after the complete directory is atomically published is reconciled only when
the journal UUID exactly matches the verified manifest, after which the stale
journal is removed durably.

Symlinks, hard links, special files, unexpected entries, unsafe modes or
ownership, malformed or oversized material, noncanonical public keys,
integrity mismatches, and command failures produce sanitized errors. Their
contents are not included in diagnostics.

Generation performs no network operation and grants no Git or GitHub authority.
The separate [read-only Repo B access test](deploy-key-access-test.md) combines
this protected generation with token-authenticated repository-identity proof;
it does not change the generation boundary. The explicit operator control,
rotation, initialization and independently selected transport lanes now compose
this primitive without broadening it. GitHub enrollment/removal remains manual.
Candidate validation, backup, observation, promotion, rollback, locking, and
Retrigger safeguards are unchanged.
