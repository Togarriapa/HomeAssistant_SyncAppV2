# Repo B initialization execution

SyncApp can execute one previously journaled Repo B initialization authority by
publishing exactly one routed Home Assistant configuration snapshot to an empty
repository's `main` branch. The executor is an explicit API boundary; ordinary
startup, Local synchronization and Retrigger scheduling do not call it yet.

## Preconditions

The caller supplies the canonical authorization request UUID, read-only Home
Assistant source, private snapshot/workspace roots, exact deploy-key access proof
and protected key directory. Execution requires:

- an integrity-checked `authorized` record for the request;
- the same pinned target and numeric repository identity;
- the authority's exact public-key fingerprint, generation UUID, zero-ref count
  and empty-observation digest;
- no synchronization baseline; and
- a freshly observed repository containing no branch or tag refs.

Missing or changed authority never falls back to token transport or implicit
initialization.

## Durable preparation and publication

Schema v37 adds one integrity-protected execution row. Before remote mutation,
the executor captures only paths routed to `main`, verifies the immutable
snapshot, creates an isolated `0700` Git workspace and produces a deterministic
initial commit using the durable preparation timestamp. It journals the exact
snapshot ID, commit SHA, repository/key binding, phase and attempt count before
entering `publishing`.

Publication delegates to the existing descriptor-only deploy-key transport with
the stricter `require_repository_empty` policy. That transport revalidates the
protected key generation, GitHub Ed25519 host pin, isolated workspace and local
commit, re-reads the complete ref set immediately before mutation, and rejects
any existing ref. It sends one non-force `commit:refs/heads/main` refspec with
credentials, hooks, prompts, file transport and submodule recursion disabled.

After exact remote confirmation, one `BEGIN IMMEDIATE` transaction records the
`main` synchronization baseline and moves both the execution and authority to
`completed`. Every temporary snapshot and workspace is removed on success,
failure and interruption unwinding.

## Restart reconciliation and retry

If the process stops after journaling but before publication, the same source and
preparation timestamp reproduce the exact commit. If it stops after the remote
accepted the push but before local completion, the next execution completes only
when the entire remote ref set is exactly `refs/heads/main` at the journaled
commit. It never pushes that reconciled commit twice.

Transient transport failures enter `retry` with exponential delays of 2, 4, 8,
16, 32 and then at most 60 minutes. A maximum of eight attempts is enforced.
Changed source/key evidence, invalid authority, unexpected refs, deterministic
transport rejection and exhausted attempts become terminal blocks and also close
the parent authority as `execution_blocked`. They cannot remain discoverable as
active initialization work.

Completed and blocked records replay without filesystem or network access, but
their execution, authority and (for completion) baseline bindings are rechecked.
Malformed phases, timestamps, identities, digests or rebinding fail closed.

## Observability and secret boundary

Runtime inventory exposes only execution phase counts, total/maximum attempts and
latest update time. Its query never selects request UUID, repository identity,
snapshot ID, commit SHA, fingerprint, generation UUID, record digest, source path
or key path. Durable execution state contains no source bytes, ref inventory,
credential, private-key material or private-key path.

This increment initializes only an explicitly requested repository. It does not
adopt deploy-key transport for routine candidate fetch, Local/database/runtime/log
publication, promotion, rollback or any Home Assistant/Supervisor mutation.
