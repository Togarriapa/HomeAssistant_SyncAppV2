# HomeAssistant_SyncAppV2

## Implementation status

The initial **0.1.0 experimental foundation** implements strict app options,
protected persistent lifecycle state, exclusive process locking, restart
detection, structured logs and automated tests/CI. It is a passive service;
synchronization, candidate deployment, backup/rollback and the Retrigger Work
Cron Job are not implemented yet. The sections below define the full target.

See [app documentation](syncapp/DOCS.md), [development instructions](docs/development.md)
and [architecture and next increments](docs/architecture.md). Initial delivery is
tracked in [epic #1](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/1).

## Overview

HomeAssistant_SyncAppV2 is a Home Assistant App that provides a safe, automated and observable synchronization layer between a Home Assistant OS installation and a user-provided private GitHub repository.

The project is initially targeted at Home Assistant OS running on a Raspberry Pi 5 with 8 GB RAM, while its architecture should avoid unnecessary Raspberry Pi-specific dependencies wherever possible.

The primary purpose of the project is to make a Home Assistant installation fully understandable and safely maintainable by both a human administrator and an AI assistant.

The GitHub repository should expose enough information for an AI assistant to:

* Understand the complete Home Assistant configuration.
* Inspect integrations, entities, devices, areas, floors and services.
* Analyze automations, scripts, scenes and dashboards.
* Understand relationships and dependencies between Home Assistant resources.
* Inspect current runtime state.
* Analyze historical state through the Recorder database.
* Inspect Home Assistant, Supervisor and SyncApp logs.
* Detect unavailable, orphaned or problematic entities and devices.
* Identify configuration errors and operational problems.
* Suggest optimizations and improvements.
* Submit configuration changes.
* Observe the outcome of those changes.
* Safely iterate when further adjustments are required.

The system must not simply copy Git files directly into a running Home Assistant installation. Remote changes are treated as deployment candidates and must pass a controlled validation, backup, deployment, observation and rollback process.

---

# Project Repositories

The solution consists of two repositories.

## Repo A — HomeAssistant_SyncAppV2

This repository contains the application source code.

It is responsible for:

* Home Assistant App packaging.
* Git synchronization.
* Local snapshot management.
* GitHub change detection.
* Candidate deployment.
* Configuration validation.
* Dependency analysis.
* Deployment risk classification.
* Backup creation.
* Rollback and disaster recovery.
* Runtime inventory collection.
* Dependency/topology generation.
* Database snapshot management.
* Log collection and retention.
* Deployment observation.
* Health monitoring.
* Retry/recovery processing.
* Retrigger Work Cron Job.
* Application configuration.
* Automated tests.
* CI/CD workflows.
* Development and operational documentation.

Repo A must never depend on Repo B for its own GitHub authentication credentials or internal application state.

---

# Repo B — Home Assistant Setup Repository

Repo B is supplied by the user and must be a **private GitHub repository** because it intentionally contains the real Home Assistant configuration, including sensitive configuration and credentials.

The repository is divided into dedicated branches according to the type and lifecycle of information stored.

## `main`

`main` represents the **last known-good Home Assistant configuration**.

It contains the byte-for-byte file contents of the synchronized Home Assistant configuration and persistent configuration state, including applicable hidden `.storage` data.

Examples include:

* `configuration.yaml`
* `automations.yaml`
* `scripts.yaml`
* `scenes.yaml`
* `secrets.yaml`
* `.storage/`
* dashboards
* blueprints
* packages
* custom components
* integration configuration
* entity registry
* device registry
* area/floor/label registries
* other Home Assistant configuration files

Database and log data assigned to their dedicated branches are excluded from `main`.

"Byte-for-byte" means the contents of synchronized files must remain identical. Git is not intended to reproduce filesystem metadata such as inode information, ownership or original modification timestamps.

Local Home Assistant configuration changes are synchronized to `main`.

`main` must always represent configuration that has either originated from the running Home Assistant instance or successfully completed the controlled deployment process.

---

# `candidate`

`candidate` contains configuration changes proposed by a user or AI assistant.

Remote configuration changes must target this branch rather than being deployed directly from `main`.

A candidate commit is never considered trusted simply because it exists in Git.

Each candidate must pass through the deployment pipeline.

```
Candidate commit
      |
      v
Change detection
      |
      v
Integrity validation
      |
      v
Dependency analysis
      |
      v
Risk classification
      |
      v
Home Assistant validation
      |
      v
Pre-deployment backup
      |
      v
Apply candidate
      |
      v
Reload / Restart
      |
      v
Observation window
      |
 +----+----+
 |         |
```

SUCCESS   FAILURE
|         |
v         v
Promote   Rollback
to main   previous state

Successful candidates are promoted to `main`.

Failed candidates are retained for diagnostic purposes but their commit SHA must be marked as rejected so that the same deterministic failure is not automatically deployed repeatedly.

---

# `database`

The `database` branch contains consistent snapshots of the Home Assistant Recorder database.

Its synchronization direction is:

```
Home Assistant -> GitHub
```

The AI may read and analyze database information, but database modifications must not normally be deployed from GitHub back into Home Assistant.

Database snapshots must be created consistently so that a database is not copied into Git while an incomplete write produces an unusable snapshot.

Database snapshots should have configurable retention, initially defaulting to approximately seven days.

Large binary database files should be handled using an appropriate large-file strategy where required.

---

# `runtime`

The `runtime` branch provides a normalized, AI-friendly representation of the currently running Home Assistant system.

The branch is generated by HomeAssistant_SyncAppV2 and is not a deployment source.

Its synchronization direction is:

```
Home Assistant -> GitHub
```

Suggested structure:

```
runtime/
├── manifest.json
│
├── homeassistant/
│   ├── entities.json
│   ├── devices.json
│   ├── integrations.json
│   ├── areas.json
│   ├── floors.json
│   ├── labels.json
│   ├── services.json
│   └── states.json
│
├── supervisor/
│   ├── apps.json
│   ├── repositories.json
│   ├── backups.json
│   └── system.json
│
├── hardware/
│   ├── system.json
│   ├── storage.json
│   └── network.json
│
├── analysis/
│   ├── topology.json
│   ├── dependencies.json
│   ├── unavailable_entities.json
│   ├── unknown_entities.json
│   ├── orphan_entities.json
│   ├── orphan_devices.json
│   └── integration_health.json
│
└── deployments/
    └── <commit-sha>.json
```

## Runtime Manifest

`manifest.json` provides an immediate summary of the installation.

It should contain information such as:

* Home Assistant version.
* SyncApp version.
* Last successful synchronization.
* Last successful deployment.
* Current `main` commit.
* Current candidate commit.
* Entity count.
* Device count.
* Integration count.
* Automation count.
* Script count.
* Unavailable entity count.
* Warning count.
* Error count.
* Current system health.

---

# Dependency and Topology Model

The SyncApp should generate relationships between Home Assistant objects instead of requiring an AI to manually correlate registry IDs.

The topology should make relationships such as the following discoverable:

```
Area
  |
  +-- Device
  |     |
  |     +-- Integration
  |     |
  |     +-- Entity
  |
  +-- Automation
         |
         +-- Trigger Entity
         +-- Conditions
         +-- Action Entities
         +-- Scripts
         +-- Scenes
```

This allows an AI assistant to answer questions such as:

* Which automations depend on this entity?
* What will be affected if this device is removed?
* Which entities are no longer referenced?
* Which integrations are producing unavailable entities?
* Which automation caused a particular action?
* Which resources are associated with a particular room?

---

# `logs`

The `logs` branch contains operational and diagnostic information.

Its synchronization direction is:

```
Home Assistant -> GitHub
```

Suggested structure:

```
logs/
├── home-assistant/
├── supervisor/
├── syncapp/
└── deployments/
```

Logs must have a **30-day retention period**.

Because deleting a file from the current Git tree does not remove that information from historical Git commits, retention management must account for Git history as well as the working tree.

The log branch may therefore periodically rebuild/prune its generated history so that old log history does not grow without bounds.

The repository must not treat Git history rewriting as guaranteed forensic secure deletion.

---

# Synchronization

## Local -> GitHub

Local Home Assistant configuration changes must be detected and synchronized automatically.

A synchronization cycle should:

1. Detect a stable change.
2. Debounce rapidly changing files.
3. Acquire a synchronization lock.
4. Create a consistent staging snapshot.
5. Verify copied file integrity.
6. Compare the snapshot with the last synchronized state.
7. Create a commit only when meaningful differences exist.
8. Push the update.
9. Record the resulting Git commit.
10. Update runtime synchronization information.

Git operations must occur in a staging area rather than directly inside the live Home Assistant configuration directory.

---

# Remote -> Home Assistant

Remote changes must be treated as controlled deployments.

The deployment process must:

1. Detect a new `candidate` commit.
2. Verify repository and candidate integrity.
3. Determine exactly what changed.
4. Analyze dependencies.
5. Assign a deployment risk level.
6. Perform static/configuration validation.
7. Create a recoverable Home Assistant backup.
8. Record the backup ID against the candidate SHA.
9. Apply the proposed configuration.
10. Reload or restart the appropriate Home Assistant components.
11. Wait for Home Assistant to become healthy.
12. Observe startup and runtime behavior.
13. Inspect errors and warnings.
14. Validate affected integrations/entities when possible.
15. Record the deployment result.
16. Promote successful configuration to `main`.
17. Tag the known-good version.
18. Roll back automatically if validation fails.

---

# Deployment Risk Classification

Changes should be classified before deployment.

Example levels:

### Low Risk

* Automation changes.
* Script changes.
* Scene changes.
* Non-critical YAML changes.

### Medium Risk

* Integration configuration.
* Dashboard/configuration changes affecting multiple resources.

### High Risk

* `.storage` registry changes.
* Core Home Assistant configuration.
* Authentication-related configuration.
* Secrets.
* Custom components.

### Critical

* Database manipulation.
* System-level operations.
* Changes capable of preventing Home Assistant or Supervisor recovery.

Risk classification should influence validation requirements and deployment strategy.

---

# Deployment Observation

Passing syntax validation is not enough to classify a deployment as successful.

After deployment, SyncApp must observe the Home Assistant system for a configurable period.

The observation process should verify:

* Home Assistant starts successfully.
* Home Assistant API responds.
* Supervisor reports an acceptable state.
* Integrations initialize.
* No significant new startup errors appear.
* Changed resources are available.
* Affected entities have valid states where applicable.
* Relevant automations/scripts load.
* Declared or derived post-deployment assertions pass where possible.

Deployment results must be written to the runtime/deployment data and operational logs.

---

# Backup and Rollback

Git is configuration history, not the sole disaster-recovery mechanism.

A Home Assistant backup must be created before every configuration deployment capable of affecting the running system.

Two rollback levels should exist.

## Fast Configuration Rollback

Used when Home Assistant remains operational and only the changed configuration needs to be restored.

## Full Recovery Rollback

Used when a deployment causes serious startup, configuration or system failure.

This process restores the pre-deployment Home Assistant backup.

Every known-good deployment should also be associated with:

* Git commit SHA.
* Deployment identifier.
* Home Assistant version.
* Backup identifier.
* Timestamp.
* Validation result.

Known-good releases should be identifiable using Git tags.

---

# Conflict Handling

The system must detect concurrent divergence.

If Home Assistant changes locally after the last synchronization while a different remote candidate is also waiting for deployment, SyncApp must not blindly merge those states.

Instead it must:

1. Stop automatic deployment.
2. Preserve both versions.
3. Record the conflict.
4. Expose the conflict through runtime/log data.
5. Require conflict resolution before synchronization continues.

Binary files and internal Home Assistant storage must never be automatically text-merged.

---

# Retrigger and Recovery

Normal synchronization should be event-driven wherever practical.

A separate **Retrigger Work Cron Job** provides recovery for interrupted or transiently failed work.

It must identify:

* Interrupted local synchronization.
* Pending Git pushes.
* Temporarily unavailable GitHub operations.
* Unprocessed candidate commits.
* Interrupted runtime collection.
* Missed database snapshots.
* Failed log synchronization.
* Recoverable deployment orchestration operations.
* Stale SyncApp locks.

All work must be idempotent.

Transient errors may be retried using controlled backoff.

Permanent failures must not create infinite retry loops.

Examples:

```
Network unavailable         -> Retry
GitHub unavailable          -> Retry
API timeout                 -> Retry
HA temporarily starting     -> Retry

Invalid YAML                -> Block
Configuration check failed  -> Block
Known bad commit SHA        -> Block
Corrupt candidate           -> Block
```

A blocked candidate should only become eligible again when its commit changes or an explicit administrative retry is requested.

---

# Security

Repo B is intentionally private and trusted to contain the real Home Assistant configuration, including credentials.

The system therefore does not sanitize Home Assistant configuration before synchronization.

However:

* Repo B must remain private.
* GitHub credentials used by SyncApp must not be stored inside Repo B.
* Application credentials and internal state must reside in protected SyncApp storage.
* Permissions must follow least-privilege principles.
* Secrets must never appear in application logs unnecessarily.
* Deployment and rollback actions must be auditable.
* Sensitive information must not accidentally be copied into the public Repo A.

Repo A may be public because it contains application source code rather than the user's Home Assistant data.

---

# Project Objectives

## Primary Objectives

1. Provide a reliable representation of a Home Assistant installation in GitHub.

2. Preserve Home Assistant configuration with byte-for-byte file-content fidelity.

3. Support safe two-way synchronization of configuration.

4. Allow an AI assistant to comprehensively analyze the Home Assistant environment.

5. Expose runtime state in a normalized AI-readable format.

6. Expose device, entity, integration and automation relationships through dependency/topology data.

7. Provide historical analysis through Recorder database snapshots.

8. Provide operational troubleshooting through retained logs.

9. Allow AI/user-generated configuration changes without directly risking the production Home Assistant installation.

10. Automatically validate, backup, deploy, observe and either accept or roll back configuration changes.

11. Survive Raspberry Pi reboots, Home Assistant restarts, GitHub outages and temporary network failures.

12. Prevent infinite deployment/retry loops.

13. Maintain an auditable relationship between Git commits, deployments and Home Assistant backups.

14. Make the system sufficiently observable that an AI can determine whether a proposed change produced its intended technical result.

---

# Non-Functional Objectives

The application should be:

* Reliable.
* Idempotent.
* Recoverable.
* Testable.
* Observable.
* Auditable.
* Modular.
* Maintainable.
* Secure by design.
* Resource-conscious for Raspberry Pi hardware.
* Resilient to process and system restarts.
* Backwards-aware of Home Assistant API evolution.

Unsupported direct modification of the Home Assistant OS host should be avoided when supported Home Assistant App, Core and Supervisor interfaces are available.

---

# Software Development Methodology

The project must be developed using an **Agile methodology** with short, incremental delivery cycles.

Development should prioritize working, tested increments rather than implementing the entire architecture in a single release.

Every meaningful capability should originate from tracked requirements and acceptance criteria.

---

# GitHub as the Project Management Platform

GitHub Issues and GitHub Projects should act as the project's Jira-equivalent planning and delivery system.

The development hierarchy is:

```
Epic
  |
  +-- User Story
  |      |
  |      +-- Task
  |      +-- Task
  |
  +-- User Story
         |
         +-- Task
```

Separate issue classifications must also exist for:

* Defect
* Bugfix

Recommended labels:

```
type:epic
type:user-story
type:task
type:defect
type:bugfix
```

Additional useful labels should include:

```
priority:critical
priority:high
priority:medium
priority:low

component:sync
component:git
component:deployment
component:backup
component:rollback
component:runtime
component:database
component:logs
component:retrigger
component:security
component:testing
component:documentation

risk:low
risk:medium
risk:high
risk:critical
```

A GitHub Project board should provide at minimum:

```
Backlog
   ↓
Ready
   ↓
In Progress
   ↓
In Review
   ↓
Testing
   ↓
Done
```

`Blocked` should be represented explicitly when applicable.

Sub-issues and dependency relationships should be used to model work hierarchy and blocking relationships.

---

# User Stories

User Stories should use a consistent format:

```
As a <user/system>,
I want <capability>,
so that <business/technical value>.
```

Every User Story must contain measurable acceptance criteria before implementation begins.

Example:

```
As a Home Assistant administrator,
I want candidate configurations validated before deployment,
so that an invalid AI-generated configuration cannot
prevent my Home Assistant instance from starting.
```

Acceptance Criteria:

* Candidate is staged outside the live configuration directory.
* Home Assistant configuration validation executes successfully.
* Failed validation prevents deployment.
* Failure information is recorded.
* The failed SHA is blocked from automatic retry.
* Existing Home Assistant configuration remains unchanged.

---

# Test Driven Development

The project must follow a **Test Driven Development (TDD)** approach.

The expected development loop is:

```
RED
Write a failing test describing required behavior.
   ↓
GREEN
Implement the minimum code necessary to pass.
   ↓
REFACTOR
Improve the implementation while keeping tests green.
   ↓
REPEAT
```

Tests should be treated as part of the implementation rather than work performed after development.

Appropriate test levels include:

* Unit tests.
* Component tests.
* Git integration tests.
* Home Assistant API integration tests.
* Failure-injection tests.
* Recovery tests.
* Backup/rollback tests.
* Synchronization conflict tests.
* End-to-end deployment tests.

External systems should be isolated through clear interfaces so that Home Assistant, GitHub, filesystem and Supervisor interactions can be mocked or simulated during automated testing.

Critical recovery logic must have explicit tests for failure paths, not only successful execution.

---

# Development Best Practices

Development should follow current software engineering best practices, including:

* Small, reviewable changes.
* Clear separation of concerns.
* Dependency inversion around external systems.
* Explicit interfaces between components.
* Strong error handling.
* Structured logging.
* Idempotent operations.
* Deterministic tests.
* Automated formatting.
* Static analysis.
* Type checking where supported.
* Dependency vulnerability scanning.
* Automated CI.
* Mandatory test execution for pull requests.
* Code review before integration.
* No credentials committed to Repo A.
* Versioned migrations for persistent SyncApp state.
* Backwards-compatible state handling where practical.
* Documentation updated as behavior changes.

---

# Pull Request Workflow

Development should use short-lived branches and Pull Requests.

A Pull Request should not be considered complete until:

1. Linked Issue/User Story/Task exists.
2. Acceptance criteria are satisfied.
3. Automated tests pass.
4. New behavior has appropriate tests.
5. Static analysis passes.
6. Relevant integration tests pass.
7. No unresolved review comments remain.
8. Documentation is updated where necessary.
9. CI is green.

Commits and Pull Requests should link back to their relevant GitHub Issues.

---

# Definition of Done

An Issue is Done only when:

* Acceptance criteria are satisfied.
* Required code has been implemented.
* TDD tests exist.
* Existing tests remain green.
* Failure paths are tested where relevant.
* Logging/observability is implemented.
* Documentation is updated.
* Security implications have been considered.
* Pull Request review is complete.
* CI validation succeeds.
* The change is merged into the appropriate development branch.

---

# Initial Epics

The initial project backlog should be organized around the following Epics:

## EPIC 1 — Home Assistant App Foundation

Create the installable Home Assistant App, configuration model, permissions, lifecycle and persistent application state.

## EPIC 2 — Git Synchronization Engine

Implement repository authentication, staging, hashing, change detection, local-to-remote synchronization and synchronization state tracking.

## EPIC 3 — Repository Branch and Data Management

Implement `main`, `candidate`, `database`, `runtime` and `logs` branch responsibilities and retention policies.

## EPIC 4 — Runtime Inventory and AI Context

Collect entities, devices, integrations, areas, floors, services, current states and system information and expose normalized AI-readable representations.

## EPIC 5 — Dependency and Topology Analysis

Build relationships between entities, devices, integrations, automations, scripts, scenes and areas.

## EPIC 6 — Candidate Validation and Deployment

Implement candidate detection, change analysis, validation, risk classification and controlled deployment.

## EPIC 7 — Backup and Rollback

Implement pre-deployment backup, fast rollback, full recovery and known-good version tracking.

## EPIC 8 — Deployment Observation and Health Analysis

Observe Home Assistant after changes and determine whether configuration, integrations and affected resources remain healthy.

## EPIC 9 — Database Snapshot Management

Create consistent Recorder database snapshots and implement configurable retention and large-file handling.

## EPIC 10 — Logging and 30-Day Retention

Collect Home Assistant, Supervisor, application and deployment logs while enforcing the required rolling retention policy.

## EPIC 11 — Retrigger Work and Resilience

Implement idempotent recovery, transient retry handling, stale job recovery and protection against retry loops.

## EPIC 12 — Security and Hardening

Implement credential isolation, permission minimization, input validation, auditability and protection of sensitive information.

## EPIC 13 — Automated Testing and CI/CD

Build the TDD infrastructure, unit/integration/end-to-end test suites and automated quality gates.

## EPIC 14 — Documentation and Operations

Provide installation, configuration, troubleshooting, recovery, architecture and contributor documentation.

---

# Success Criteria

The project will be considered successful when:

* Home Assistant configuration can be reliably synchronized to GitHub.
* Direct local Home Assistant changes are reflected in `main`.
* AI/user changes can be proposed through `candidate`.
* Candidates cannot bypass validation and backup.
* Successful deployments automatically become the new known-good `main`.
* Failed deployments automatically return Home Assistant to a known-good state.
* Known-bad commits cannot enter an infinite deployment loop.
* Home Assistant runtime information is available in a structured AI-readable format.
* Dependencies between Home Assistant resources can be analyzed.
* Historical database information is available for analysis.
* Operational logs are available with 30-day retention.
* Interrupted operations recover automatically.
* Every deployment is traceable to a Git commit and recovery point.
* Core functionality is covered by automated tests and continuously validated by CI.
* Development work is traceable through Epics, User Stories, Tasks, Defects and Bugfixes in GitHub.
