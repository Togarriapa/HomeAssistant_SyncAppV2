## Purpose

Advance freshly queued trusted candidates through read-only validation during normal service operation while preserving independent recovery and deployment safeguards.

## ADDED Requirements

### Requirement: Routine candidate preflight advances bounded pending work

The service SHALL advance at most one due pending candidate action per routine preflight tick, in deterministic due-time order, through exact Fetch/Stage, integrity analysis, dependency analysis, risk classification, static validation and exact-version isolated semantic validation. Each action MUST consume the exact persisted predecessor evidence. Successful semantic validation MUST leave the candidate at the existing backup-preparation handoff.

#### Scenario: Fresh candidate completes preflight without recovery dispatch

- **WHEN** a trusted candidate is detected and successive normal ticks run with valid synthetic repository, runtime and validation evidence
- **THEN** the candidate advances through all six preflight actions using its exact SHA and bound evidence
- **AND** each tick executes at most one action and semantic success leaves backup preparation pending
- **AND** no recovery dispatch is required for those successful preflight actions

#### Scenario: Multiple candidates or elapsed intervals remain bounded

- **WHEN** multiple due pending candidates exist or several routine intervals elapsed
- **THEN** one tick processes at most one action selected by due time, creation time and candidate identity
- **AND** the service coalesces missed intervals without replaying a catch-up queue

### Requirement: Routine execution cannot acquire recovery authority

Routine preflight SHALL claim only due pending work within the existing attempt budget. It MUST leave running, retry, blocked, terminal and exhausted-attempt work unchanged and MUST NOT recover interrupted work, independently reset attempts, remove backoff or rearm known-bad candidates. The canonical successful phase handoff SHALL retain its existing attempt reset. Existing Retrigger scheduling and recovery SHALL remain available with their current policy.

#### Scenario: Interrupted and retry work await Retrigger

- **WHEN** a candidate is running or has retry status, whether its retry deadline is future or already due
- **THEN** routine ticks leave its durable work and checkpoints unchanged and perform no external operation for it
- **AND** a later authorized Retrigger pass can recover or retry it under the existing rules

#### Scenario: Repeated detection cannot rearm rejection

- **WHEN** a blocked candidate SHA is detected repeatedly and routine ticks run
- **THEN** its work remains blocked and no preflight action executes for it

#### Scenario: Exhausted pending work cannot gain a new attempt

- **WHEN** pending candidate work already has an attempt count at or above the existing maximum
- **THEN** routine ticks leave its durable rows unchanged and perform no external operation for it

### Requirement: Existing candidate evidence and transport safeguards apply

Routine preflight MUST reuse the established exact-candidate execution and checkpoint safeguards. It SHALL preserve private numeric repository binding, isolated workspaces, staging integrity, dependency/risk checks, static validation and exact-version semantic confinement. Candidate observation and Fetch/Stage SHALL honor the configured token or deploy-key selector without credential fallback; integrity/change analysis SHALL retain its existing separately token-backed transport boundary.

#### Scenario: Deploy-key Fetch/Stage receives no token

- **WHEN** deploy-key candidate ingress is explicitly configured and its protected proof is valid
- **THEN** normal Fetch/Stage uses that ingress without passing a token to its reference or fetch operations
- **AND** the later integrity/change-analysis operation uses its existing token-backed executor explicitly

#### Scenario: Stale or tampered authority fails closed

- **WHEN** the exact candidate, repository binding, checkpoint, staged bytes, baseline or required version/confinement evidence does not satisfy an existing executor's checks
- **THEN** the action cannot advance to a successful successor or invoke any downstream deployment mutation
- **AND** deterministic failure blocks the exact work under the existing failure policy

### Requirement: Routine failures preserve durable retry and rejection policy

An expected action failure SHALL be recorded using the existing transient or deterministic classification. A recorded transient failure MUST retain the existing bounded attempts and backoff; a recorded deterministic failure MUST remain blocked. Failure of durable state or authority handling MUST fail the service closed with sanitized diagnostics.

#### Scenario: Transient action failure hands off to recovery

- **WHEN** a preflight executor reports a transient failure
- **THEN** the claimed action becomes retry work with the existing backoff and attempt budget
- **AND** subsequent routine ticks do not consume that retry

#### Scenario: Invalid configuration stops at the validation gate

- **WHEN** static or semantic validation deterministically rejects the candidate
- **THEN** the existing rejection checkpoint and blocked candidate state are retained
- **AND** normal operation performs no backup, Apply, restart, promotion or rollback

### Requirement: Routine preflight grants no deployment mutation authority

The normal preflight path MUST stop before backup preparation and MUST NOT invoke backup creation, Apply admission/execution, restart, post-deployment observation, finalization, promotion or rollback. It MUST preserve the default read-only Home Assistant mount and current App permissions. Physical HAOS gates MUST NOT be represented as complete by this increment.

#### Scenario: Semantically validated candidate remains at the handoff

- **WHEN** a candidate reaches the backup-preparation handoff and further normal ticks run
- **THEN** routine preflight does not claim it or change its work/checkpoint state
- **AND** no backup, live-file write, Core mutation or repository publication occurs through routine preflight

### Requirement: Preflight follows protected service lifecycle and diagnostics

Routine preflight SHALL run only after configured Repo B identity is pinned, inside the existing state-owning service loop. It MUST respect shutdown before construction and between actions, release its scheduling state before the store closes, and report only fixed action/outcome categories. Credentials, raw responses, exception text, private paths and repository/candidate identities MUST NOT enter its diagnostic events.

#### Scenario: Unconfigured or stopping service performs no preflight

- **WHEN** Repo B is unconfigured or shutdown is requested before preflight construction or a tick
- **THEN** the service performs no normal candidate preflight action

#### Scenario: Failure diagnostics omit sensitive details

- **WHEN** a preflight action receives credentials or an external failure containing sensitive sentinel text
- **THEN** emitted normal-preflight diagnostics contain only fixed action/outcome categories
- **AND** the sensitive text, repository target, candidate SHA and protected paths are absent
