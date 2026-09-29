# Repo B deploy-key promotion and known-good tag transport

SyncApp has a proof-bound SSH primitive for reconciling one immutable
`DeploymentPromotion`. It advances `main` and creates the deployment's
deterministic known-good tag without a GitHub token. The primitive is available
for later initialization/runtime integration; it does not replace the active
REST promotion path in this increment.

## Exact authority and reference proof

`read_promotion_remote_state_with_deploy_key()` accepts the exact private Repo B
identity, protected key generation proof and deterministic promotion tag. It
uses the bounded deploy-key reference transport to obtain a fresh authenticated
snapshot. The snapshot must be bound to the same target, numeric repository ID,
key fingerprint and generation UUID. It must contain exactly one canonical
`candidate` ref and exactly one canonical `main` ref; the exact known-good tag
may be absent or appear once.

`publish_promotion_refs_with_deploy_key()` additionally binds that evidence to
the integrity-valid deployment promotion: the candidate must still equal the
authorized commit, `main` may be only the recorded baseline or candidate, and
the tag may be only absent or candidate. Missing, duplicate, malformed,
unsorted, rebound, stale or conflicting evidence fails deterministically before
mutation. A state in which both refs already equal the candidate is an
idempotent no-op that performs no fetch or push.

## Isolated atomic publication

For incomplete authorized state, the exact candidate object is acquired through
the proof-bound SSH candidate-fetch primitive into a private workspace disjoint
from Home Assistant source. Its repository-core configuration, exact candidate
object and internal fetch ref are re-proven. URL rewrites and every additional
local configuration key are rejected.

Immediately before mutation, SyncApp reads the refs again and opens a new
descriptor-bound session, which revalidates the protected key generation and
packaged GitHub host-key pin. Changed state closes the authority. The only
permitted refspecs copy the internal candidate-fetch ref to a missing
`refs/heads/main` and/or the exact missing known-good tag. When both are missing,
one `git push --atomic` transaction carries both refspecs. Partial crash-recovery
states carry only the missing refspec. No `--force`, force refspec, delete,
mirror, arbitrary destination, GitHub token, askpass helper, reusable private-key
path or submodule recursion is permitted.

The Git process is noninteractive, bounded as a process group, pinned to the
credential-free repository SSH URL, and receives the private key only through an
inherited descriptor. Hooks, system/global configuration, credential helpers,
file transport, ambient identities, passwords, keyboard authentication,
forwarding and local SSH commands remain disabled.

## Reconciliation and cleanup

After the push, another proof-bound snapshot must show candidate, `main` and the
known-good tag all at the exact authorized commit. An unchanged or safe partial
state is an uncertain transient outcome so the durable promotion plan can
reconcile it under normal backoff. Any moved candidate, divergent branch or
conflicting tag is deterministic and must be blocked rather than continuously
retried.

The isolated fetched workspace is removed on every success and failure path.
Cleanup is restricted to a resolved child with the candidate-workspace prefix
under the configured private workspace root; malformed transport results cannot
expand that deletion boundary.

## Runtime adoption boundary

The active production promotion remains the existing REST implementation until
an explicit, reviewed initialization authority selects the SSH transport and
exposes that choice through runtime/deployment diagnostics. Adoption must retain
the durable promotion journal, locking, exact work identity, controlled
backoff, deterministic blocking, final-state proof and every validation,
backup, deployment observation and rollback safeguard. Missing or invalid
deploy-key authority must fail closed and must never silently fall back to a token-backed Git transport.

No live SSH request, Repo B ref mutation, Home Assistant or Supervisor mutation,
deployment, promotion, rollback or key enrollment is performed by this
development increment.
