# Architecture

## Product boundary

HomeAssistant_SyncAppV2 has two distinct trust domains:

- **Repo A** — this application source repository. It may be public and must never contain real Home Assistant credentials, Repo B private keys, or copied user configuration.
- **Repo B** — the user's private Home Assistant repository. It intentionally contains the complete configured Home Assistant tree, including sensitive and runtime-oriented files. Logs are the sole intentional exception from its main complete-tree representation and are routed to the dedicated `logs` branch.

SyncApp-owned credentials, deploy keys, operation journals and protected service state live under app-owned storage, outside the synchronized Home Assistant tree.

## Current implemented foundation

The current service implements:

Supervisor options → strict configuration validation → exclusive state store → passive lifecycle.

Validation happens before the state directory is created. Version 0.1.0 intentionally requests no Home Assistant filesystem/API privilege and performs no network synchronization. `config.py` validates bounded options; `state.py` owns SQLite and the lifetime process lock; `__main__.py` owns lifecycle and sanitized logging.

`/data/syncapp` is app-owned. `instance.lock` is a stable inode held with nonblocking `flock` for the service lifetime. `state.sqlite3` uses transactional schema versioning and fails closed on unsupported/corrupt state. This foundation is the recovery substrate for later synchronization work, not the final product boundary.

## Target data plane

The target loop is:

```text
Live Home Assistant tree
        |
        | stable byte-preserving snapshot
        v
Isolated local staging/snapshot store
        |
        +-----------------------> Repo B main
        |                          (last known-good complete tree)
        |
        +-----------------------> runtime/database/logs generated views

Repo B candidate
        |
        v
Detect -> Fetch -> Stage -> Validate -> Backup -> Apply -> Verify
                                                    |         |
                                                    |         +--> success -> promote main
                                                    |
                                                    +------------> failure -> rollback
```

No Git working tree used for candidate retrieval may be the live Home Assistant configuration directory.

## Complete-tree representation

The configured Home Assistant tree is represented without blanket file-class exclusions. Hidden `.storage`, secrets, credentials, certificates, keys, databases/WAL files, generated/runtime/cache files, binaries and other Home Assistant-owned files remain visible when they exist under the configured tree.

The only main-tree routing exception is **logs**, which remain available through the dedicated logs branch.

Dedicated `runtime` and `database` branches are analytical/retention views and do not replace the underlying files on `main`.

Path safety remains strict despite complete visibility: traversal, unsafe symlink behavior, ambiguous hardlinks and staging escape must fail closed. Sensitive content may be synchronized to private Repo B while still being excluded from application logs and Repo A fixtures.

## Remote mutation transaction

Every GitHub → Home Assistant change follows the same state machine:

1. **Detect** — observe a new immutable candidate identity and confirm there is work.
2. **Fetch** — fetch exact Git objects into app-controlled storage with repository identity/privacy verification.
3. **Stage** — materialize the exact candidate tree outside live configuration and produce deterministic path/content integrity metadata.
4. **Validate** — run structural, Home Assistant and risk-aware checks against those exact staged bytes. A later apply must prove it is using the same bytes.
5. **Backup** — create/confirm a recoverable Home Assistant backup plus an integrity-bound preimage/plan sufficient for deterministic rollback.
6. **Apply** — modify only the planned live paths and only from the validated staging snapshot.
7. **Verify** — prove Home Assistant health and affected-resource behavior, including relevant error/warning observation.
8. **Rollback if necessary** — restore the pre-change state when apply or verification fails, then verify rollback health and persist the terminal outcome.

A process crash at any phase must be recoverable from durable operation records without replaying an unsafe step blindly.

## Risk model

Risk influences validation and deployment strategy, not whether a file is visible.

Examples:

- automation/script/scene edits may support targeted validation/reload;
- core configuration and `.storage` changes require stronger checks and may require controlled restart;
- secrets/certificates/keys require private handling and must not leak into diagnostics;
- database mutation is critical and remains blocked until a safe, tested mutation strategy exists;
- unknown file classes fail closed for automatic mutation until their validation/apply semantics are defined, while remaining visible in Repo B.

This distinction is fundamental: **visibility is complete; mutation permission is evidence-based.**

## Durable operation state

Future schema migrations must record enough information to resume safely:

- operation ID/idempotency key;
- operation type;
- immutable remote repository ID/ref/candidate SHA;
- baseline known-good SHA;
- phase and attempt count;
- staged snapshot identity/integrity manifest;
- validation evidence;
- backup ID/preimage identity;
- planned live-path mutation set;
- apply/verify/rollback outcomes;
- terminal blocked/rejected reason;
- timestamps needed for bounded retry policy.

Recovery executes only while the lifetime app lock is held.

## AI context plane

The AI should not need to reverse-engineer every registry relationship repeatedly. Generated runtime artifacts should normalize entities, devices, integrations/config entries, areas/floors/labels, services, states, Supervisor/system data, topology/dependencies and deployment outcomes.

Generated context is derived data. The complete Home Assistant tree remains the source-of-truth representation for visibility, while runtime artifacts provide efficient interpretation and verification.

## Delivery sequence

1. Repository-wide product-contract alignment and regression guard.
2. Repo B identity/privacy/deploy-key initialization boundary.
3. Complete-tree stable snapshot engine with sole log-routing exception.
4. Durable operation journal/recovery semantics.
5. Runtime inventory/context collection sufficient for AI analysis.
6. Candidate fetch/stage/integrity and conflict detection.
7. Validation adapters and risk policy.
8. Backup/preimage transaction.
9. Guarded apply, verification and automatic rollback.
10. Known-good promotion, rejected-SHA handling and deployment reporting.
11. Database/runtime/log retention views and resilience hardening.
12. Physical Home Assistant OS validation before production-ready claims.

See `docs/roadmap.md` and issues #7, #8, #9 and #10.
