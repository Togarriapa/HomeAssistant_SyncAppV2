# HomeAssistant_SyncAppV2

HomeAssistant_SyncAppV2 is a Home Assistant App whose primary purpose is to make a real Home Assistant installation comprehensively understandable and safely adjustable by both humans and AI assistants.

The target feedback loop is:

**Home Assistant → private GitHub Repo B → AI analysis/edit → candidate branch → Detect → Fetch → Stage → Validate → Backup → Apply → Verify → Rollback if necessary → logs/runtime state → AI iteration**

The application source lives in this repository (Repo A). The user's actual Home Assistant representation lives in a separate **private** repository (Repo B).

## Current implementation status

Version 0.1.0 provides the passive application foundation only: strict options, protected persistent lifecycle state, exclusive process locking, interrupted-run detection, structured logs, automated tests and native amd64/aarch64 container CI.

Synchronization, Repo B initialization, runtime inventory, guarded deployment, backup/rollback and recovery scheduling remain incremental milestones. The product contract below governs those increments.

See [architecture](docs/architecture.md), [roadmap](docs/roadmap.md), [development instructions](docs/development.md), [app documentation](syncapp/DOCS.md), MVP issue #7 and P0 product-contract issue #10.

---

# Authoritative product contract

## 1. Complete-tree visibility

Repo B's configuration representation must expose the **complete configured Home Assistant tree** with byte-preserving file contents so an AI can reason about the actual installation rather than a curated subset.

This includes, when present under the configured Home Assistant tree:

- configuration YAML and packages;
- automations, scripts and scenes;
- dashboards and Lovelace resources;
- helpers, blueprints and custom components;
- hidden `.storage` state;
- integration/config-entry, entity, device, area, floor and label registries;
- secrets and credentials;
- certificates and keys;
- Recorder database, WAL and related state;
- generated/runtime/cache/state files;
- binary files;
- every other file that belongs to the configured Home Assistant tree.

**Logs are the sole intentional exception from the main complete-tree representation.** Logs remain visible through the dedicated `logs` branch for diagnostics and deployment correlation.

A file must not be omitted merely because it is sensitive, binary, generated, database-backed, runtime-oriented, hidden, high-risk or inconvenient to diff.

This requirement applies to Repo B only. Repo A credentials, SyncApp deploy keys, internal operation journals and protected app state remain isolated from the synchronized Home Assistant tree.

## 2. Private-repository requirement

Repo B intentionally contains sensitive material and therefore must be private. SyncApp must verify repository identity and privacy before transfers. Git content transfers should use the configured SSH deploy-key path; any metadata token is separate and must never become the transport credential for Repo B content.

Secrets must not be copied into Repo A, diagnostic logs or unrelated generated artifacts.

## 3. AI operability

The synchronized system must let an AI determine:

- which entities, devices, integrations/config entries, areas, floors and labels exist;
- current states and services;
- automation/script/scene/dashboard definitions;
- relationships and dependencies between resources;
- relevant historical Recorder information;
- startup/runtime/deployment errors and warnings;
- what changed in a proposed deployment;
- whether the change produced its intended effect;
- what failed when it did not.

Dedicated normalized `runtime` and `database` branches may make analysis efficient, but they are **additional views**, not excuses to omit the underlying complete-tree files from the main representation.

## 4. Bidirectional adjustment without blind trust

Complete visibility does not mean arbitrary remote bytes are trusted. Every GitHub → Home Assistant mutation is a controlled transaction:

**Detect → Fetch → Stage → Validate → Backup → Apply → Verify → Rollback if necessary**

The transaction must preserve these invariants:

1. Never run `git pull`, checkout or merge directly into the live Home Assistant configuration directory.
2. Fetch and stage candidates outside the live tree.
3. Determine the exact candidate commit and changed path/byte set.
4. Bind validation evidence to the exact staged bytes later applied.
5. Detect local/remote divergence and fail closed rather than silently merging.
6. Perform path/content/risk-aware validation before live mutation.
7. Create a confirmed recoverable Home Assistant backup and/or integrity-bound preimage before apply.
8. Apply only the validated candidate bytes.
9. Reload/restart only what is required and wait for Home Assistant to become healthy.
10. Verify affected resources plus system health and inspect new errors/warnings.
11. Promote successful state to known-good `main` and record deployment metadata.
12. Roll back automatically when apply or verification fails.
13. Remember deterministic rejected candidate identities so they do not retry forever.

Sensitive or critical paths require stronger validation and deployment policy. They are not blanket-excluded solely because of file class.

## 5. Repository B branch model

### `main`

`main` represents the last known-good **complete Home Assistant tree**, excluding only logs that are routed to `logs`.

Local Home Assistant changes are synchronized to `main` after a stable, integrity-checked snapshot. Successful remote candidates are promoted to `main` only after validation, backup, apply and verification succeed.

### `candidate`

`candidate` contains user- or AI-proposed changes. A commit existing here is never trusted automatically. It must pass the full guarded deployment transaction.

### `runtime`

`runtime` is a generated AI-friendly view of the running installation: inventories, current states, services, topology/dependencies, health summaries and deployment outcomes. It is not a direct deployment source.

Suggested structure:

```text
runtime/
├── manifest.json
├── homeassistant/
│   ├── entities.json
│   ├── devices.json
│   ├── integrations.json
│   ├── areas.json
│   ├── floors.json
│   ├── labels.json
│   ├── services.json
│   └── states.json
├── supervisor/
├── hardware/
├── analysis/
│   ├── topology.json
│   ├── dependencies.json
│   ├── unavailable_entities.json
│   ├── orphan_entities.json
│   ├── orphan_devices.json
│   └── integration_health.json
└── deployments/
```

### `database`

`database` may hold consistent, retention-managed Recorder snapshots or normalized historical-analysis artifacts. It is an additional analytical view; Recorder files that live inside the configured Home Assistant tree remain part of the complete-tree representation on `main`.

Remote database mutation is critical-risk work and must fail closed unless a specific safe validation/backup/apply/verify strategy exists.

### `logs`

`logs` contains Home Assistant, Supervisor, SyncApp and deployment diagnostics. Logs are intentionally routed away from `main` and retained on a bounded policy, initially 30 days. Retention must consider Git ancestry growth as well as the current tree and must not claim forensic secure deletion.

---

# Local → GitHub synchronization

A synchronization cycle should:

1. Detect a stable change.
2. Debounce rapidly changing files where required for consistency.
3. Acquire the operation lock.
4. Create a stable snapshot outside the live tree.
5. Reject unsafe path/link semantics.
6. Preserve exact bytes, including binary content and line endings.
7. Record deterministic integrity metadata.
8. Route logs to the `logs` branch and keep every other Home Assistant-tree file in the complete-tree representation.
9. Compare against the last synchronized snapshot.
10. Commit only meaningful changes.
11. Push atomically with remote-ref lease protection.
12. Persist resulting commit/operation state.
13. Update normalized runtime synchronization metadata.

Git operations must not execute inside the live Home Assistant tree.

---

# Conflict handling

If the live Home Assistant tree diverges locally while a different remote candidate is pending, SyncApp must stop automatic deployment, preserve both states, record the conflict and require deterministic resolution. Binary files and Home Assistant internal state must never be opportunistically text-merged.

---

# Backup, verification and rollback

Git history is not the only recovery mechanism. Before live mutation, SyncApp must create a recoverable Home Assistant backup and preserve enough integrity-bound preimage information for deterministic rollback.

Post-apply verification must go beyond syntax validation. It should verify Home Assistant and Supervisor health, relevant integrations, changed resources, startup/runtime errors and declared/derived assertions where feasible.

Every deployment result should record the candidate SHA, resulting known-good SHA if successful, Home Assistant version, backup identifier, changed resources, timestamps, validation evidence and verification outcome.

---

# Retrigger Work and resilience

Normal work should be event-driven where practical. A separate recovery/retrigger process handles interrupted or transiently failed work. All operations must be idempotent and persisted with explicit phases, attempts and terminal failure states.

Transient failures such as network/GitHub/API availability may retry with bounded backoff. Deterministic failures such as invalid candidates, failed validation, corrupt staged content or known rejected SHAs must block until the candidate changes or an explicit administrative retry occurs.

---

# MVP objective

The MVP is not merely "back up YAML to Git." It is the smallest safe system that closes the AI feedback loop:

1. verify and initialize private Repo B;
2. capture the complete Home Assistant tree with logs as the sole main-tree routing exception;
3. publish normalized runtime context so an AI can understand entities/devices/integrations/states and dependencies;
4. accept candidate changes through an isolated staging boundary;
5. validate exact staged bytes;
6. create a confirmed pre-change backup;
7. apply guarded changes;
8. verify Home Assistant and affected resources;
9. roll back on failure;
10. expose deployment results and logs for AI iteration;
11. recover interrupted work idempotently.

See [docs/roadmap.md](docs/roadmap.md) for delivery order and gates.

---

# Development and safety rules

- Use short-lived feature branches and pull requests.
- Start meaningful behavior changes with tests.
- Keep CI green before merge.
- Use strict failure-path and recovery tests for critical logic.
- Keep Repo A free of real Home Assistant secrets and credentials; fixtures must be synthetic.
- Keep SyncApp credentials/internal state outside Repo B's synchronized Home Assistant tree.
- Do not weaken the staged deployment, backup or rollback boundary to gain speed.
- Do not merge risky or failing changes.
- Document behavior when contracts change.

The project remains incremental: a passive foundation being merged does not imply synchronization or deployment is production-ready.
