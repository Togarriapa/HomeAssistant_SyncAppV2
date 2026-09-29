# Repo B deploy-key non-force publication transport

SyncApp now has a proof-bound SSH primitive for publishing one immutable
`PublicationIntent` from an isolated `GitWorkspace`. The primitive is available
for later initialization/runtime integration but is not connected to the active
Local, Runtime, Logs, Database, Candidate, promotion, or retention lanes.

## Exact authority and pre-mutation proof

`push_publication_intent_with_deploy_key()` accepts only the existing immutable
publication intent, its isolated workspace, an exact `DeployKeyAccessProof`, and
the current protected key directory. The intent continues to carry the pinned
Repo B target and numeric repository ID, exact local commit, exact destination
branch, and either an exact remote baseline or explicit branch-absence
expectation.

Before any push the transport obtains a fresh
[proof-bound reference snapshot](deploy-key-reference-transport.md). The
snapshot must match the repository, fingerprint, and generation UUID. A normal
publication requires exactly one destination ref at the authorized baseline; an
initial publication requires the destination ref to be absent. Missing,
duplicate, moved, malformed, or rebound evidence fails deterministically before
mutation.

The isolated workspace is then re-proven: its accepted content digest, branch,
Git repository, and exact `HEAD` must still match the immutable intent. The
protected key is inspected again immediately before push, closing the authority
if rotation, replacement, fingerprint drift, generation drift, unsafe storage,
or host-pin drift occurred after reference observation.

## Descriptor-only non-force push

The only remote mutation command is the exact non-force refspec:

`<authorized-commit>:refs/heads/<authorized-branch>`

The remote is the credential-free SSH URL for the configured repository. The
private key is passed to OpenSSH only through an inherited descriptor. No GitHub
token, askpass file, credential helper, reusable private-key path, or private key
byte is placed in argv, environment, state, logs, exceptions, or artifacts.

System/global Git configuration, hooks, file transport, terminal prompts,
ambient SSH identities, password/keyboard authentication, forwarding, and local
commands remain disabled. The process group uses the bounded timeout and output
limits from the reference transport. Local Git configuration is restricted to
the exact machine identity and repository-core keys created by SyncApp, so URL
rewrite and push-policy keys cannot redirect or expand the operation. Submodule
recursion is explicitly disabled. No `--force`, force refspec, delete, mirror, or
arbitrary destination is permitted.

After the command the workspace is re-proven and references are read again
through the same proof-bound transport. Success is returned only when exactly
one destination ref equals the authorized local commit. An unchanged baseline
or still-absent initialized branch is an uncertain, retryable outcome so the
existing publication recovery can reconcile it. A conflicting ref is a
deterministic divergence and cannot be retriggered indefinitely.

## Runtime adoption boundary

This primitive does not alter the existing token-backed production publication
or promotion transports. Explicit initialization must first establish durable,
reviewed deploy-key authority and runtime status. Later adoption must preserve
publication preflight, immutable intent, fast-forward ancestry, post-push proof,
durable baseline completion, locking, idempotency, Retrigger backoff, and every
Candidate deployment safeguard. When initialized deploy-key authority is absent
or invalid, the runtime must fail closed: it must never silently fall back to token-backed Git transport.

No live SSH request, GitHub enrollment/removal, Repo B write, Home Assistant or
Supervisor mutation, deployment, promotion, retention rewrite, or rollback is
performed by this development increment.
