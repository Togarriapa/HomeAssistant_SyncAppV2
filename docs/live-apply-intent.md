# Durable live Apply intent recovery boundary

This document describes the non-authoritative persistence boundary introduced by Task #283. The sole product specification remains the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Purpose

A verified deployment may eventually need to mutate the live Home Assistant configuration. Before any later writer is allowed to do that, SyncApp persists enough exact identity metadata to recognize the interrupted deployment after a restart and to route it back through the controlled deployment pipeline.

Persisting an intent is **not** permission to write `/homeassistant`. A durable record is recovery metadata only.

## Required evidence before persistence

`record_live_apply_intent()` accepts the producer-issued chain that already exists in the controlled deployment path:

1. `ApplyAuthorization`, derived from the persisted prepared deployment, verified backup and freshly re-proven Repo B heads.
2. `StagePrewriteEvidence`, derived from a fresh integrity verification of the isolated candidate Stage.
3. `LiveApplyPlan`, derived from the deterministic candidate change evidence after another Stage integrity proof.
4. `LiveApplyPreconditionEvidence`, derived by read-only verification of the affected live paths against the trusted baseline.

The persistence adapter re-derives `LiveApplyIntent` from those objects immediately before opening the StateStore transaction. It then re-reads the prepared deployment and repository pin from protected state inside the transaction and requires exact target, repository ID, baseline SHA, candidate SHA, Stage manifest SHA-256 and backup slug bindings.

## Durable record

Schema v8 stores only recovery metadata:

- deployment ID;
- Repo B target and immutable repository ID;
- baseline and candidate commit IDs;
- Stage manifest SHA-256;
- verified backup slug;
- verified Home Assistant root;
- SHA-256 of the canonical deterministic ordered Apply operations;
- UTC recording timestamp;
- SHA-256 integrity digest over the canonical record values.

Candidate file contents, Home Assistant secrets, GitHub credentials and API tokens are not persisted in this record.

The table is created transactionally for new stores and by an explicit v7-to-v8 migration for existing stores. Existing work rows are preserved.

## Idempotence and conflicts

The first valid record for a deployment/candidate identity is immutable. Repeating the exact intent returns the already-persisted record, including its original timestamp. A later attempt that changes the root or any other intent binding for the same identity is rejected rather than replacing the row. The repository/candidate uniqueness constraint also prevents alias deployment IDs from rebinding the same candidate.

## Restart and Retrigger Work discovery

`discover_live_apply_intents()` provides bounded, deterministic discovery of persisted intents after a restart. Every discovered row is passed through the same integrity and identity validation as a direct load.

Discovery does not reconstruct or grant Apply authorization. A later retrigger-aware deployment worker must obtain fresh upstream evidence again, including current repository/Stage/live-path proofs, before any mutation can occur. The persisted record exists only to identify what interrupted work must be reconsidered.

## Fail-closed read semantics

A loaded record is rejected if its canonical integrity hash fails, its fields are malformed, its repository pin no longer matches, or its associated prepared deployment no longer matches the target/repository/baseline/candidate/Stage/backup identity. Diagnostics are intentionally sanitized and do not echo persisted path or secret-like values.

## Explicit non-authority

Task #283 performs no live file write, delete, rename, chmod or directory creation; no Supervisor mutation; no Home Assistant reload/restart; no backup restore; no Git promotion; no observation acceptance; and no rollback action.

The eventual writer must consume the durable intent together with fresh producer-issued evidence, re-check every path immediately before mutation, preserve the backup/rollback relationship, and durably record progress so an interrupted Apply is never blindly restarted.
