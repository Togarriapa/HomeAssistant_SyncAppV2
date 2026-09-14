# Deterministic live Apply planning boundary

This document describes the side-effect-free planning gate for one exact future controlled Home Assistant candidate deployment. The sole product specification remains the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Purpose

`build_live_apply_plan()` converts freshly re-proven Stage evidence plus the exact candidate change evidence into immutable, deterministic path-level operations. It does not apply those operations.

The planner accepts only the exact `StagePrewriteEvidence`, `CandidateStage`, and `CandidateChanges` types. Before producing a plan it binds the repository target, immutable repository ID, candidate SHA, `candidate` branch, Stage manifest SHA-256, and trusted baseline SHA. It then re-runs `verify_candidate_stage()` and rejects any evidence drift observed across that external filesystem verification boundary.

Operations are sorted by UTF-8 path bytes. Added, modified, mode-changed, and modified-and-mode-changed entries must match the exact staged Git mode and object ID. Deleted paths are represented only as deletion intent and must be absent from the candidate Stage. Every operation retains the baseline and candidate Git mode/object metadata required for a later writer to perform precondition checks instead of blind overwrites or deletes. For staged files the plan also retains staged size and SHA-256 integrity metadata.

## Security and failure semantics

The planner snapshots evidence into detached immutable values before Stage verification and compares the live evidence against those snapshots afterward. Stage verification failures and malformed or inconsistent evidence fail closed using deterministic sanitized diagnostics; nested exception details, candidate bytes, repository credentials, tokens, and private configuration are not emitted.

The plan is ephemeral authorization data only. It is not a durable deployment result and does not grant permission to skip any subsequent safety gate.

## Explicit non-authority

This boundary performs no `/homeassistant` write, delete, rename, chmod, or other live filesystem mutation. It performs no Supervisor mutation, Home Assistant reload/restart, backup creation/restoration, Git network/ref mutation, candidate promotion, observation, health acceptance, or rollback.

A future live writer must consume the exact plan together with the exact freshly re-proven Stage, must precondition every live path operation against the expected baseline state, and must preserve the existing pre-deployment backup, controlled deployment, observation, promotion, rejection, and rollback semantics from the initial V2 README.

The recurring Retrigger Work mechanism remains enabled and unchanged. Retry orchestration must never treat deterministic plan/evidence failures as indefinitely retryable work.
