# Repo B deploy-key reference transport

SyncApp now has a reusable read-only SSH metadata transport for a previously
verified Repo B deploy-key generation. This boundary is deliberately narrower
than candidate fetch, publication, promotion, or initialization: it reads the
canonical branch/tag reference set and grants no write or deployment authority.

## Authority chain

`read_repo_b_deploy_key_references()` requires all of the following inputs:

1. an exact `DeployKeyAccessProof` returned by the identity-bound access test;
2. the caller's configured Repo B `owner/repository` and pinned numeric ID;
3. the protected key directory containing the same fingerprint and generation
   UUID as the proof; and
4. protected work, executable, and packaged known-host boundaries.

The function rejects a malformed proof, a target or numeric-ID mismatch, an
unsafe/incomplete key, and a key generation changed after the proof. It
re-inspects the protected generation immediately before SSH. A proof for one
repository or generation therefore cannot authorize another transport call.

GitHub deploy keys authenticate Git's SSH protocol; they do not authenticate
the GitHub REST API. The earlier access test intentionally retains the existing
token-authenticated REST request solely to establish the private repository's
stable numeric identity before producing the proof. Reference transport itself
receives no token and performs no REST request. Replacing the provisioning API
credential, if desired, requires a separately reviewed GitHub App or equivalent
identity mechanism rather than pretending a deploy key has REST capability.

## Confined execution

The private key is opened with no-follow semantics, verified as a single-link
`0600` regular file owned by the App user, and passed to OpenSSH only through an
inherited read-only descriptor. The persistent private-key path and private
bytes never appear in the command, SSH URL, environment, result, exception,
log, durable identity, or artifact.

The transport uses only
`ssh://git@github.com/<owner>/<repository>.git` and the packaged, byte-exact
GitHub Ed25519 host pin. System/global Git and SSH configuration, credential
helpers, alternate identities, prompts, password/keyboard authentication,
forwarding, local commands, file transport, and hooks are disabled. Execution
uses the same bounded process-group runner as the access proof: timeout,
stdout/stderr limits, and forced group termination apply before any output is
accepted.

## Reference snapshot

Successful output is parsed into an immutable `DeployKeyReferenceSnapshot`
bound to the exact target, numeric repository ID, fingerprint, and generation
UUID. It contains a tuple of canonical `DeployKeyReference` values plus a
SHA-256 observation digest. Only sorted, unique `refs/heads/*` and
`refs/tags/*` records with lowercase SHA-1 or SHA-256 object IDs are accepted.

Empty repositories are valid. Symbolic `HEAD`, peeled refs, pull refs,
duplicates, unsorted records, control characters, malformed separators,
noncanonical hashes, missing final newlines, excessive ref counts, and
oversized output fail closed. DNS, route, connection reset/refusal, and timeout
remain transient; proof, key, authentication, host-pin, and reference evidence
failures are deterministic. Raw stderr and untrusted response content are never
included in errors.

## Integration boundary

The separate [deploy-key candidate fetch transport](deploy-key-candidate-fetch.md)
now composes this metadata read with a second exact-generation check and a
descriptor-bound acquisition of one candidate commit. Production Fetch/Stage,
and the separate
[deploy-key publication transport](deploy-key-publication-transport.md) compose
the same proof with exact-generation revalidation for one candidate acquisition
or one non-force branch push. Production Fetch/Stage, publication, and promotion
paths remain unchanged until durable initialization/runtime authority exists.
Those paths must preserve non-force updates, fresh remote-state checks,
candidate validation, backup, deployment observation, rollback, locking,
idempotency, and deterministic blocking. No startup or Retrigger path invokes
these deploy-key transports automatically yet.

No live Repo B, Home Assistant, Supervisor, enrollment, key-removal, deployment,
promotion, or rollback operation is performed by this boundary.
