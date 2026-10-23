# Design

## Context

See `proposal.md` for motivation. Evidence on upstream `main` at `190ae622201f13749454dd4daffed1e6c3fc4ad3`:

- Root README sections “Remote -> Home Assistant”, “Retrigger and Recovery”, “Test Driven Development” and “Success Criteria” require controlled candidate processing, independent recovery, tests and exact auditability.
- `__main__.py` constructs/ticks normal Local, Recorder, runtime, logs and candidate detection services. Detection only enqueues immutable candidate work.
- `retrigger_cycle.py` is currently the production caller of the six candidate preflight recovery lanes. Its ordered early-exit behavior limits a cycle to one candidate action.
- `candidate_*_execution.py` already implement evidence-bound action execution, journal-before-I/O, typed transient errors and checkpoint successors. Static and semantic deterministic rejection update orchestration and work atomically.
- `StateStore.defer_work` returns a successful phased action to `pending` with attempts zero. `fail_work` uses `retry` plus bounded backoff or `blocked`. This existing distinction supports strict routine/recovery separation without a schema migration.
- `candidate_detection_service.py` and its activation tests establish cadence, coalescing, monotonic-clock validation, trust ordering and shutdown conventions.
- `syncapp/config.yaml` maps `/homeassistant` read-only, uses the backup Supervisor role and remains experimental. Issues #212 and #251 require physical HAOS evidence, which this increment cannot produce.

## Goals / Non-Goals

The design adds one complete, normal preflight route using existing typed execution authority. It keeps routine scheduling, recovery and deployment mutation separate. No schema or option migration is needed.

No live deployment enablement, permission changes, deployment-gate changes, broad orchestration refactor, transport expansion, new API client or new dependency is part of this increment. The root README remains unchanged.

## Decisions

### 1. Dedicated normal processor claims pending work only

Add `candidate_preflight_process.py` with `run_candidate_preflight_process(...)`, a sanitized result and fixed error type. Use one atomic deterministic claim for due `candidate` work whose status is exactly `pending`, whose attempt count is below `StateStore.MAX_WORK_ATTEMPTS`, and whose absent orchestration or permitted `next_action` identifies one of the six actions below. Existing orchestrations must belong to the configured target and pinned numeric repository identity. Filter out other targets/identities, terminal state and `prepare_backup`; phase and next-action must match the permitted transition. Use compare-and-update checks when claiming, validate the loaded orchestration and register a previously unregistered claimed SHA through `register_claimed_candidate`.

Order by `next_attempt_at`, `created_at`, `work_key`; execute only the selected candidate's one action. Do not use the generic claim API that can select other work kinds. Do not call any recovery helper, independently reset attempts, invoke a `*_retrigger_pass`, or duplicate executor evidence validation. The canonical successful `defer_work` transition intentionally resets attempts for the next phase; no other routine attempt reset is authorized. Use existing typed execution functions directly:

| Next action | Executor | Explicit inputs |
| --- | --- | --- |
| no orchestration / `fetch_stage` | `execute_candidate_fetch_stage_once` | selected token XOR deploy-key ingress; fetch workspace, stage root, live read-only root |
| `analyze` | `execute_candidate_integrity_once` | GitHub token; analysis workspace, same stage root, live read-only root |
| `analyze_dependencies` | `execute_candidate_dependency_once` | Core token; same stage root, live read-only root |
| `classify_risk` | `execute_candidate_risk_once` | persisted dependency/runtime evidence |
| `validate` | `execute_candidate_static_once` | same stage root and live read-only root |
| `validate_semantics` | `execute_candidate_semantic_once` | same stage root and live read-only root |

On successful nonterminal execution, call the existing `defer_work` only while the exact claimed work remains running. Static/semantic rejection already changes work to blocked; inspect the typed result and durable work, and do not defer blocked work. Semantic success also defers, preserving `prepare_backup` for its existing owner. The routine processor never marks the whole candidate succeeded at this handoff.

Expected typed executor failures are recorded with `fail_work(..., transient=error.transient)`, then returned as fixed `retry`/`blocked` outcomes. The owner loop continues once that failure is durably recorded. A state/claim/authority failure that cannot be recorded safely raises the sanitized processor error and fails closed. Do not catch arbitrary exceptions to fabricate success or silently lose work.

Alternative: call existing Retrigger passes from normal ticks. Rejected because those passes recover running work and consume retries. Alternative: change all existing recovery wrappers to support a mode flag. Rejected for this increment because it widens the recovery regression surface. One small pending-only claim/dispatch module reuses the security-critical executors without changing them.

### 2. Small cadence service stays inside the state owner

Add `candidate_preflight_service.py` following the existing detection-service lifecycle: `start(now)`, `tick(now)`, `stop()`, finite nondecreasing monotonic clock validation and missed-interval coalescing. A fixed one-second cadence starts one full interval after `start`; it needs no user option. A due tick invokes the normal processor once, using an aware UTC reference time for durable timestamps. No thread, socket, second database owner, immediate startup action or catch-up loop is added.

Integrate `_candidate_preflight_service_if_configured(...)` into `__main__.py` after trusted repository setup and alongside candidate detection. Reuse the same protected roots as Retrigger (`main-workspaces/candidate-fetch`, `main-workspaces/candidate-analysis`, `main-snapshots/candidate-stage`) and the existing safe-root preparation. The normal entry point must preserve private-root ownership, directory type, symlink rejection, 0700 mode and non-overlap checks equivalent to Fetch/Stage recovery without importing or invoking that recovery module. Supply only the selected ingress authority to Fetch/Stage, keep the GitHub token separately available for integrity analysis, and pass the Core token explicitly for read-only runtime collection. Tick after candidate detection, checking shutdown before each service action. Stop on clean shutdown and exception cleanup before StateStore releases ownership. Include the new service in active-mode detection and expose a fixed startup failure category/exit code consistently with adjacent services.

The one-second cadence is a scheduling bound, not a promise that external I/O finishes in one second. Existing client timeouts and semantic subprocess bounds continue to apply. The single owner serializes normal work and Retrigger requests; the new code must not introduce concurrent state mutation.

### 3. Preserve transport and deployment boundaries explicitly

`repo_b_candidate_transport` currently selects observation and Fetch/Stage. Its SSH authority must receive no token and must not fall back. The existing integrity executor independently re-observes/refetches and compares trusted `main` using the GitHub token; retain this documented boundary without pretending SSH covers all preflight I/O.

Normal preflight dispatch has exactly six allowed actions. It imports no backup, Apply, restart, observation, finalization, promotion or rollback executor. No live mount write, backup API call or Git publication belongs to the normal path. Existing Retrigger deployment behavior and timers remain unchanged; this increment adds no pause/disable control and does not claim live deployment readiness.

### 4. Use current projections and fixed diagnostics

Existing candidate orchestration/checkpoint runtime projections already expose the new route's persisted phases. Avoid another schema or durable status collection. Emit normal-preflight events only for work actually processed, with fixed action and outcome categories (`advanced`, `retry`, `blocked`), and add a fixed fatal-service reason. Never log the result's internal WorkItem or arbitrary exception representation.

### 5. Verify behavior through the actual route

Use synthetic Git/runtime/validator boundaries. Add a component test starting with detector-enqueued pending work and drive all six real executors through successive normal ticks, checking exact checkpoints and SHA binding at each step, byte-identical unchanged live files and the final backup handoff. Reuse existing synthetic fixture helpers where useful; do not replace the entire chain with fabricated generic success objects. Negative tests must cover invalid configuration, stale/tampered authority, selected SSH Fetch/Stage with zero token calls, deterministic rejection and transient backoff.

Separately prove that running, due/future retry, blocked, terminal and backup-ready work are untouched; snapshot the full relevant durable rows and forbid external calls. Run an existing Retrigger pass afterward to demonstrate that recovery still owns interrupted/eligible retry work. Service activation tests prove trust ordering, shutdown/cleanup, sanitized failure and cadence behavior. Packaging regression tests keep the read-only mount and App permissions intact.

## Risks / Trade-offs

- Normal phases are synchronous and can delay the owner loop → perform one bounded action per tick, retain existing timeouts and check shutdown between actions.
- Successful work and failed retries share one work table → use exact `pending` claims; never reclassify `running` or `retry` in normal code.
- Earlier code records candidate work by SHA before orchestration registration → register only through the existing claimed-work authority and re-prove exact repository/candidate evidence in the executor; do not create another identity scheme in this increment.
- New normal SQL might drift from recovery semantics → keep the pending-only query local and small, reuse typed executors unchanged, test exact status preservation and existing Retrigger integration.
- SSH selection covers only current ingress adoption → document the independent token integrity operation and test the explicit credential boundary.
- Synthetic/container tests cannot establish physical HAOS confinement or lifecycle behavior → retain #212/#251 and the experimental read-only package; report those limitations in review.

## Migration Plan

No persistent-state migration, dependency or option change. Existing configured services gain normal pending-candidate preflight on upgrade; existing retry/interrupted work remains owned by Retrigger. Reverting the new service/processor and lifecycle wiring returns successful pending work to the pre-existing Retrigger route with compatible checkpoints. Archive this OpenSpec change only after implementation, review and required CI satisfy its tasks; do not close physical evidence gates.
