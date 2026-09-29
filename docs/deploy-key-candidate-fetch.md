# Repo B deploy-key candidate fetch transport

SyncApp has a proof-bound SSH primitive for acquiring one exact Repo B
`candidate` commit into isolated Git metadata. It replaces token-backed HTTPS
inside the primitive and is selected by the production Fetch/Stage lane only when
the operator explicitly chooses deploy-key candidate ingress after initialization.

## Authority and fresh reference gate

`fetch_trusted_candidate_with_deploy_key()` requires the exact
`DeployKeyAccessProof`, configured repository target, pinned numeric repository
ID, expected candidate commit, protected key directory, private workspace root,
and live Home Assistant boundary.

Before creating a fetch workspace it invokes the
[proof-bound reference transport](deploy-key-reference-transport.md). The
returned immutable snapshot must match the proof's target, repository ID,
fingerprint, and generation UUID and must contain exactly one
`refs/heads/candidate` record at the expected object ID. A missing, moved,
duplicated, malformed, or rebound candidate reference fails deterministically
without creating an isolated repository.

The protected generation is inspected again immediately before fetch. Rotation,
replacement, incomplete state, fingerprint drift, or generation drift between
the reference observation and fetch therefore closes the authority instead of
silently switching credentials.

## Confined acquisition

The primitive creates a unique `0700` workspace disjoint from live
`/homeassistant`, initializes metadata only, and fetches exactly:

`refs/heads/candidate:refs/syncapp/candidate-fetch`

No checkout, merge, reset, pull, push, switch, live configuration read, or live
configuration write is performed. The Git remote is the credential-free SSH URL
for the configured repository. The private key is re-opened with the protected
key checks and passed to OpenSSH only as an inherited descriptor. No GitHub token,
askpass file, persistent private-key path, or private byte enters the command,
environment, result, exception, log, state, or artifact.

The transport reuses the packaged GitHub Ed25519 host pin and disables ambient
Git/SSH configuration, credential helpers, file transport, hooks, prompts,
alternate identities, password/keyboard authentication, forwarding, and local
commands. Its process group has fixed timeout and stdout/stderr limits. Network
timeouts and recognized temporary connectivity failures remain retryable;
authentication, proof, host-key, key-generation, reference, and fetched-object
failures are deterministic and sanitized.

After transport, the isolated object must resolve to the exact expected commit
and have Git object type `commit`. Every rejected, failed, timed-out, moved, or
interrupted attempt removes its partial workspace. A successful result contains
only the isolated metadata root and exact repository/branch/commit identities.

## Runtime integration boundary

The reviewed [candidate-ingress adoption](deploy-key-candidate-ingress-adoption.md)
supplies the exact proof/generation authority and preserves the journal-before-network
checkpoint, bounded Retrigger backoff, deterministic candidate blocking, offline
replay, stage verification, and downstream safeguards. Selection is explicit and
fails closed when initialization or key authority is incomplete; it
must never silently return to token-backed Git transport.

Ordinary publication, promotion and rollback are outside candidate-ingress
selection and retain their existing production transport until separate adoption.

No live SSH request, GitHub enrollment/removal, Repo B write, Home Assistant or
Supervisor mutation, deployment, promotion, or rollback is performed by this
primitive.
