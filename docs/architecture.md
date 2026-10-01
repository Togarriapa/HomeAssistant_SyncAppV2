# Current architecture

> **Specification authority:** this document describes the implementation on the
> current `main` branch. The sole product specification remains the initial root
> `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.
> Increment-specific documents retain the narrower authority boundaries that were
> true when each component was introduced; this document is the current system map.

## System boundary

Home Assistant Supervisor starts one protected SyncApp service through the container
launcher. Strict option validation occurs before durable state ownership. The app
receives `/homeassistant` read-only, uses the Home Assistant Core and Supervisor APIs
only through bounded purpose-specific clients, and stores app-owned state, credentials,
snapshots, Git workspaces and journals below `/data/syncapp`.

The launcher owns process supervision and the interval timer, but never opens the
state database. The service owns the stable process lock, schema 38 SQLite store and
all durable state transitions. A same-owner Unix socket carries only bounded Retrigger
requests from the launcher to that state-owning process.

Repo B must be an explicitly configured private GitHub repository. Before repository
work, SyncApp verifies the canonical target through GitHub REST, requires a positive
numeric repository identity, and pins that identity durably. A repository recreated at
the same path fails closed. Git operations always use isolated app-owned workspaces;
the live Home Assistant configuration directory is never a Git working tree.

## Protected state and durable identities

`/data/syncapp` and its protected children reject symlinks, unsafe ownership or mode,
unexpected file types and ambiguous hard links. The stable `instance.lock` inode is
held with nonblocking `flock` for the service lifetime and is never age-stolen or
unlinked. SQLite uses transactions and `synchronous=FULL`; unknown schemas, integrity
failures and unsafe recovery files fail closed rather than triggering a destructive
reset.

The schema 38 store includes:

- installation/run identity and clean/interrupted lifecycle evidence;
- pinned repository identities and exact branch synchronization baselines;
- unique durable work identities, attempts, status and bounded retry timestamps;
- candidate orchestration checkpoints and producer-issued evidence bindings;
- backup, Apply, restart, observation, finalization, promotion and rollback journals;
- log/database retention evidence and replacement intent;
- Repo B initialization, deploy-key generation/rotation and one-shot administrative
  action receipts; and
- identity-free runtime/deployment projections for troubleshooting.

Re-enqueueing the same work identity is idempotent. Interrupted `running` work can be
reconciled by its owning lane. Transient failures use controlled exponential backoff;
deterministic failures block without looping. Blocked work is eligible again only when
its source identity changes or an operator supplies the exact separately authorized
administrative retry request.

## Routine producers

Routine production is deliberately separate from recovery. Startup bootstraps and
bounded event/cadence services schedule or process Local configuration, Recorder
database, generated runtime inventory, sanitized logs and candidate-head observations.
Repeated signals coalesce against the lane's deterministic durable identity. A routine
producer never rearms blocked work and never imports interrupted-work recovery
semantics.

Local configuration is snapshotted byte-for-byte into protected staging before any
publication. Recorder uses a consistent staged snapshot. Runtime inventory normalizes
Home Assistant, Supervisor and analysis data for AI consumption. Log collection and
database/log retention remain bounded and use dedicated branches. Branch routing keeps
`main`, `candidate`, `database`, `runtime` and `logs` responsibilities separate.

## Retrigger recovery

The launcher sends the first Retrigger request one full configured interval after
startup. Missed intervals coalesce; a failed dispatch advances the deadline instead of
forming a tight loop. The service then runs one deterministic bounded cycle across the
implemented Local, Recorder, retention, runtime, log, rollback and candidate lanes.

Each lane recovers only its own interrupted or eligible retry work and performs at most
its documented bounded action. Candidate lanes are ordered so one cycle cannot skip
ahead through multiple deployment phases. Retrigger cannot create administrative
authority, unblock deterministic failures, bypass validation, repeat an uncertain
mutation blindly, or become the routine periodic producer.

See [Bounded Retrigger cycle](retrigger-cycle.md),
[Retrigger schedule](retrigger-schedule.md) and
[Retrigger runtime status](retrigger-runtime-status.md).

## Candidate deployment

An exact trusted `candidate` commit advances through durable, evidence-bound gates:

1. identity-bound detection and isolated Fetch/Stage;
2. integrity/static analysis, dependency analysis and risk classification;
3. exact-version isolated Home Assistant semantic validation;
4. journaled Supervisor backup creation or exact interruption reconciliation;
5. fresh repository, backup, Stage, plan and live-path Apply admission proof;
6. journal-before-mutation controlled Apply with per-operation verification;
7. authorized Core restart and staged Core, Supervisor, integration, startup-error,
   resource, entity, automation/script and assertion observations;
8. finalization and atomic non-force promotion plus known-good tagging on success; or
9. durable rejection and separately authorized exact-backup rollback on failure.

Every successor is created from persisted producer-issued evidence. A failed candidate
SHA remains blocked; retry/backoff is limited to classified transient failure. Backup,
observation, promotion and rollback cannot be skipped by startup, routine work,
Retrigger or administrative controls.

## Deploy-key administration and transport

GitHub REST still uses the configured token for private/numeric repository identity and
metadata operations. Git/SSH lanes can independently use the protected repository-scoped
Ed25519 deploy key. No credential is embedded in repository URLs, logs, durable work
identities or generated artifacts.

One-shot UUIDv4-bound operator actions expose protected generation, read-only access
testing, staged prepare/verify/activate rotation and explicit empty-repository
initialization. Completed or deterministically blocked requests replay their durable
receipt without repeating side effects; transient failures retain the shared bounded
retry policy. GitHub enrollment and old-key removal remain manual, and the previous key
is retained through activation.

Candidate fetch, ordinary publication, promotion, rollback repository proof and
generated-history retention have separate opt-in deploy-key selectors. Each revalidates
the exact protected generation and repository proof immediately before use, passes the
private key only through an inherited descriptor, pins GitHub's host identity and has no
silent token-Git fallback. See the consolidated
[Repo B deploy-key operations](repo-b-deploy-key-operations.md) runbook.

## Runtime and deployment observability

Generated runtime artifacts and fixed structured log events expose bounded state for
routine synchronization, retry/backoff, blocked work, initialization, deploy-key
administration and deployment progress. Projections omit tokens, private keys, private
paths, repository targets, branch/commit identities, request UUIDs, raw API/command
responses and exception text where those values are not required for operator action.

Runtime visibility is read-only and grants no retry, Apply, promotion or rollback
authority. Detailed evidence remains in the protected state store; public diagnostics
use counts, fixed outcome categories, timestamps and phase/action summaries.

## Safety invariants

- `/homeassistant` remains read-only to ordinary snapshot and validation paths; only
  the reviewed Apply writer receives narrowly scoped mutation authority after every
  upstream gate passes.
- Git never runs in the live configuration tree.
- Repo B identity pinning precedes repository work, and mutation uses non-force or
  exact-lease semantics appropriate to the lane.
- Configuration validation, backup, deployment observation, promotion and rollback
  remain independent required safeguards.
- Locking, unique work/commit identities, journaling, bounded retry and deterministic
  blocking protect every recovery path.
- Routine scheduling, Retrigger recovery and explicit administrative retry remain
  separate authority domains.

## Physical HAOS release gates

Native amd64/aarch64 CI proves the reproducible container, AppArmor and semantic
validator contracts, but it cannot prove the exact Supervisor/AppArmor behavior of an
installed app on physical Home Assistant OS hardware. The following release gates
therefore remain open and must not be inferred complete from source or CI:

- [issue #212](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/212) —
  semantic-validator confinement on physical Home Assistant OS;
- [issue #251](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/251) —
  application lifecycle, persistence and reboot behavior on physical Home Assistant OS.

Use [Physical HAOS release evidence](haos-release-evidence.md) on a dedicated test
installation. Its checker validates the evidence document's shape and integrity; it
cannot establish that the physical observations occurred or were truthful. Neither
gate may be closed without reviewed evidence from the designated HAOS system.
