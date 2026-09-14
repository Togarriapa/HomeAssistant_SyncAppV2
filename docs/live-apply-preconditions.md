# Live Apply precondition proof boundary

This document describes the final read-only live-filesystem proof before a future controlled Home Assistant candidate writer. The sole product specification remains the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Purpose

`prove_live_apply_preconditions()` consumes one exact immutable `LiveApplyPlan` and an absolute Home Assistant root path. It verifies that every affected live path still satisfies the baseline assumptions carried by that plan without mutating the live tree.

For an `added` operation, the destination must be absent. For `deleted`, `modified`, `mode_changed`, and `modified_and_mode_changed` operations, the existing live path must be a regular non-symlink file whose Git blob object ID and Git mode exactly match the plan baseline. SHA-1 and SHA-256 Git object IDs are supported according to the expected object-ID width.

The proof rejects unsafe or traversal paths, duplicate paths, non-deterministic plan ordering, symlinked affected paths, unsafe parent components, non-regular files, malformed baseline metadata, live content/mode drift, and plan drift observed across filesystem inspection. Errors are deterministic and sanitized; live file contents and nested filesystem exception text are never returned.

## Evidence and authority

Successful verification emits immutable ephemeral `LiveApplyPreconditionEvidence` containing the exact deployment, Repo B identity, baseline SHA, candidate SHA, Stage manifest digest, Home Assistant root, and verified affected paths.

This evidence is not a durable deployment result and does not grant standalone mutation authority. A future writer must consume the exact upstream candidate/Stage/plan chain and must re-check each individual live path precondition immediately before mutating that path. This proof narrows the TOCTOU window; it does not eliminate the need for write-time preconditions.

## Explicit non-authority

This boundary performs no `/homeassistant` write, delete, rename, chmod, directory creation, or other filesystem mutation. It performs no Supervisor mutation, Home Assistant reload/restart, backup creation/restoration, Git network/ref mutation, candidate promotion, observation, health acceptance, rejection-state mutation, or rollback.

The pre-deployment backup, controlled Apply, reload/restart, observation, promotion/rejection, and rollback semantics from the initial V2 README remain mandatory. Deterministic precondition mismatches must be blocked rather than continuously retriggered.

The recurring Retrigger Work mechanism remains enabled and unchanged.