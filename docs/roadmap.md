# Roadmap

This roadmap is subordinate to the P0 product contract in issue #10 and the repository README.

## MVP definition

The MVP closes a safe AI feedback loop around a real Home Assistant installation. It is complete only when an AI can inspect the actual installation, propose a change, have SyncApp stage and validate the exact candidate, create a recoverable backup, apply the change safely, verify the result from runtime state/logs, and recover automatically when the change fails.

The mandatory remote-update state machine is:

**Detect → Fetch → Stage → Validate → Backup → Apply → Verify → Rollback if necessary**

Complete visibility and guarded mutation are separate concerns: Repo B must expose the complete configured Home Assistant tree, while automatic mutation remains risk- and evidence-gated.

## P0 — Contract alignment

**Goal:** remove stale assumptions before new synchronization code is introduced.

- Make complete-tree visibility explicit.
- Preserve logs as the sole intentional exception from the main representation.
- Preserve Repo A/app-state credential isolation.
- Remove blanket exclusions based on secrets, `.storage`, databases, generated/runtime files, caches, certificates/keys or binary file class.
- Replace narrow "only automations/scripts/scenes may deploy" wording with risk-aware mutation policy.
- Add regression checks/tests preventing the stale contract from returning.
- Re-run tests, lint, format, type checking, Bandit and native container CI.

**Gate:** contract-only PR is green and reviewed before synchronization implementation starts.

## P1 — Repo B initialization and trust boundary

**Goal:** deliberate, authenticated, private-repository setup.

- Generate and protect Ed25519 deploy keys locally.
- Pin GitHub SSH host identity.
- Verify repository owner/name, stable repository ID and private visibility.
- Keep transport credentials out of Git URLs, Repo B and logs.
- Add explicit Initialize Repo action; never overwrite a conflicting nonempty repository silently.
- Persist initialization state idempotently across crashes/lost acknowledgements.

Tracked primarily by issue #9.

## P2 — Complete-tree local snapshot engine

**Goal:** Home Assistant → Repo B visibility with exact bytes.

- Mount/read the configured Home Assistant tree only after the foundation safety boundary is ready.
- Walk the complete tree without file-class filtering.
- Include hidden `.storage`, secrets, certificates/keys, database/WAL, generated/runtime/cache and binary files.
- Route only logs away from main.
- Reject unsafe links/path traversal/ambiguous filesystem semantics.
- Produce deterministic content/path manifest and stable snapshot identity.
- Handle concurrent mutation with retry/consistency rules.
- Commit/push through isolated Git storage and remote-ref lease checks.

Tracked primarily by issue #8.

## P3 — Durable work journal and retrigger recovery

**Goal:** make every operation restart-safe and idempotent.

- Versioned schema migration from the passive foundation.
- Durable phases, attempt count, immutable operation identity and terminal blockers.
- Bounded backoff for transient faults.
- Explicit administrative retry for blocked work.
- Lost-push-acknowledgement and crash recovery tests.
- Recovery only under the lifetime process lock.

## P4 — Runtime inventory for AI analysis

**Goal:** efficiently describe the running system without replacing complete-tree fidelity.

- Entities, devices, integrations/config entries, areas, floors, labels.
- Services and current states.
- Supervisor/app/system/hardware data available through supported APIs.
- Automation/script/scene/dashboard inventory.
- Dependency/topology graph and problem summaries.
- Runtime manifest tying current state to known Git/deployment identities.

## P5 — Candidate Detect / Fetch / Stage

**Goal:** establish the immutable remote-candidate boundary.

- Detect exact candidate SHA.
- Fetch raw Git objects into app-controlled storage.
- Materialize candidate outside live Home Assistant configuration.
- Bind path/content integrity manifest to the staged snapshot.
- Detect local/remote divergence and stop automatic deployment on conflicts.
- Do not execute candidate hooks, filters or code during staging.

## P6 — Validation and risk policy

**Goal:** determine whether exact staged bytes are safe enough to apply.

- Structural/path integrity checks.
- Home Assistant configuration validation using the running target version where supported.
- Risk classification based on changed resources/paths and required restart scope.
- Targeted semantic checks for automations/scripts/scenes/dashboards/helpers and other supported resources.
- Stronger policy for `.storage`, secrets, certificates/keys, custom components, core configuration and databases.
- Unknown/unsupported mutation classes fail closed but remain visible in Repo B.

## P7 — Backup and preimage

**Goal:** make every live mutation recoverable.

- Request and confirm a Home Assistant backup before apply.
- Persist backup identity against candidate SHA.
- Preserve integrity-bound preimage/planned mutation information.
- Test backup failures, timeouts, crash recovery and backup-result ambiguity.

## P8 — Apply / Verify / Rollback

**Goal:** safely complete the transaction.

- Apply only the exact validated staged bytes and planned paths.
- Reload/restart the minimum required Home Assistant scope.
- Verify Home Assistant/Supervisor health.
- Verify affected resources and inspect newly introduced warnings/errors.
- Roll back automatically on apply/verification failure.
- Verify rollback health and persist terminal outcome.
- Mark deterministic rejected candidate SHAs.

## P9 — Promotion and AI feedback

**Goal:** close the iteration loop.

- Promote successful candidate state to known-good main.
- Record deployment result, candidate SHA, main SHA, backup ID, HA version and affected resources.
- Publish runtime/deployment result artifacts and logs sufficient for an AI to judge the outcome.
- Tag known-good states where useful.

## P10 — Retention and operational hardening

**Goal:** keep analytical data useful and bounded.

- Consistent Recorder snapshot/analysis views.
- Runtime generated-view updates.
- 30-day log retention including Git ancestry management.
- Resource usage limits suitable for Raspberry Pi-class hardware.
- Failure injection across network, GitHub, Supervisor, filesystem and process-crash boundaries.

## Release gate

No production-ready claim until all critical CI is green and the supported installation/backup/restore/deployment/rollback flow has been exercised on a test Home Assistant OS system. Container CI is necessary but not sufficient for physical HAOS certification.
