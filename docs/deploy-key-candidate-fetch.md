# Repo B deploy-key candidate fetch transport

SyncApp now has a proof-bound SSH primitive for acquiring one exact Repo B
`candidate` commit into isolated Git metadata. It replaces token-backed HTTPS
inside this explicit primitive only; the production Fetch/Stage lane remains on
its existing transport until the deploy-key initialization and runtime authority
can supply a reviewed durable proof without an unsafe fallback.

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

This task adds no production fallback. Existing Fetch/Stage orchestration still
uses the token-backed path because no durable runtime deploy-key authorization or
explicit Repo B initialization control exists yet. Switching that lane before
those controls would make enrolled state ambiguous and could cause deterministic
failures to loop or tempt an HTTPS fallback.

A later reviewed integration must supply the exact proof/generation authority,
preserve the existing journal-before-network checkpoint, bounded Retrigger
backoff, deterministic candidate blocking, offline replay, stage verification,
and all downstream validation, backup, Apply, observation, promotion, and
rollback safeguards. It must fail closed when deploy-key initialization is not
complete; it must never silently return to token-backed Git transport.

No live SSH request, GitHub enrollment/removal, Repo B write, Home Assistant or
Supervisor mutation, deployment, promotion, or rollback is performed by this
primitive.
