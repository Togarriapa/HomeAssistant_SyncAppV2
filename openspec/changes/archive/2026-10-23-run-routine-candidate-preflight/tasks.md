# Tasks

Implementation work is tracked by [task #470](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/470). This checklist is the OpenSpec implementation record for that issue; the root README remains the product specification. Use RED/GREEN/REFACTOR in each group and record the initial focused failure before implementation.

## 1. Pending-only normal preflight processor

- [x] 1.1 Add failing `tests/test_candidate_preflight_process.py` cases for deterministic pending-only claims, one-action bounds and no-op running/due retry/future retry/blocked/terminal/backup-ready/exhausted-attempt work; verify the focused tests fail before adding the processor and compare unchanged durable rows plus zero external calls for excluded work.
- [x] 1.2 Implement `candidate_preflight_process.py` with an atomic due-pending claim, exact repository/orchestration checks and dispatch to the six existing typed executors; verify claim tests pass, stale authority fails closed and no recovery or mutation executor is imported/called.
- [x] 1.3 Implement existing-policy defer/retry/block outcomes without deferring atomically rejected work; verify tests for successful pending successor, transient retry/backoff, deterministic block, and fatal sanitized state failure.
- [x] 1.4 Add a synthetic component test that starts with detector-enqueued work and drives all six real executors through normal processor calls, preserving exact checkpoint/SHA bindings, unchanged live bytes and the final backup handoff; verify it passes without a Retrigger dispatch or downstream mutation call.
- [x] 1.5 Cover token Fetch/Stage and deploy-key Fetch/Stage plus separately token-backed integrity analysis in processor tests; verify zero token calls/fallback in selected SSH ingress and deterministic rejection on changed proof/evidence.
- [x] 1.6 Document pending-only processing, recovery handoff, transport boundary and no live deployment authority in `docs/candidate-preflight.md`; verify every described phase and status rule maps to a passing processor test.

## 2. Cadence service and state-owner lifecycle

- [x] 2.1 Add failing `tests/test_candidate_preflight_service.py` coverage for first-full-interval start, one-second cadence, at-most-one action, missed-interval coalescing, valid monotonic clocks, sanitized errors and stop behavior; verify RED before service implementation.
- [x] 2.2 Implement `candidate_preflight_service.py` around the normal processor with `start/tick/stop`, no thread or recovery API and fixed action/outcome results; verify the focused service tests pass and a recorded action failure leaves the service available for unrelated future pending work.
- [x] 2.3 Add failing `tests/test_candidate_preflight_service_activation.py` cases, then wire trusted construction, protected roots/credentials, active mode, owner-loop tick after candidate detection, shutdown guards, exception cleanup and fixed fatal exit category in `__main__.py`; verify configured/unconfigured and interrupted/clean lifecycle tests pass without leaking sentinels.
- [x] 2.4 Update `docs/architecture.md` and `syncapp/DOCS.md` with the routine preflight path, pending/retry distinction, backup boundary and link to the runbook; verify the docs preserve initial README authority, read-only permissions, unchanged Retrigger scheduling and open hardware gates #212/#251.

## 3. Cross-boundary verification and delivery

- [x] 3.1 Verify normal ticks leave interrupted/eligible retry work untouched and the existing authorized Retrigger pass subsequently recovers/processes it; run focused processor/service tests together with `tests/test_candidate_*_retrigger.py`, `tests/test_retrigger_cycle.py`, `tests/test_retrigger_schedule.py` and `tests/test_packaging.py` without weakening any existing assertion.
- [x] 3.2 Run the full Python 3.12 test suite using a short temporary root where needed, Ruff check/format, mypy and Bandit through the repository's required CI; verify green results and separately report any unavailable local tool/platform checks without treating physical HAOS gates as satisfied.
- [x] 3.3 Run `openspec validate run-routine-candidate-preflight --strict` and inspect the final diff for exact task #470 scope, sanitized diagnostics, no README/permission/recovery-policy changes and no live Home Assistant action; verify an independent planning/review model finds no unresolved correctness or security issues.
- [x] 3.4 Prepare the linked PR for merge after required CI and independent review pass, resolve findings and verify issue #470 acceptance criteria; update this checklist with actual pre-merge evidence and archive the completed OpenSpec change in the delivery PR. This checkbox records pre-merge readiness only: PR #471 remains unmerged, and root will record the eventual merge commit and post-merge verification on issue #470 when those events occur.

## Verification evidence

Evidence captured 2026-10-23. Pull request [#471](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/pull/471) is ready for merge but is not yet merged. The native CI run at [run 39961610314](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/actions/runs/39961610314) passed on head `cb33291c42c6bb976f48e39c02ef8cbcd4140377` (tree `d79a4daa903464b9405cad87264f9c1857b6f969`): Ruff format and lint, strict mypy across 210 source files, Bandit, all 2,449 Python tests in 36.80 seconds, and both amd64 and aarch64 container builds.

Independent Sol review approved the implementation and static claim SQL, with 156 focused tests and mutation checks. The checks confirmed that widening the pending-only claim to retries breaks the exclusion test and removing the blocked-result path breaks both deterministic-rejection tests. Local verification also passed the focused suite, strict OpenSpec validation, `compileall` and `git diff --check`. Ruff, mypy and Bandit were unavailable locally; the macOS full-suite baseline had platform-specific failures, so native CI is the full-suite evidence. No physical HAOS evidence is claimed: gates #212 and #251 remain open, the default Home Assistant mount remains read-only, and existing Retrigger scheduling and recovery policy are unchanged. Root will record the eventual merge commit and post-merge verification on issue #470.
