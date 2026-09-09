# Synchronization baseline state

This document describes the protected synchronization-baseline state introduced by issues #27 and #28. Its sole product requirement source is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Purpose

The initial V2 specification requires each Local → GitHub cycle to compare a verified staging snapshot with the last synchronized state, record the resulting Git commit after a successful synchronization, remain restart-safe, and keep internal application state outside Repo B.

`StateStore` therefore records one last-successful baseline per configured repository target and branch. A baseline binds:

- the Repo B target;
- the branch;
- the deterministic staged snapshot ID;
- the resulting Git commit SHA;
- the timezone-aware successful-synchronization timestamp.

The baseline does not claim a synchronization happened until orchestration explicitly records the successful result. Replacing a baseline is an atomic SQLite transaction. Reads validate persisted values again and fail closed if state has been corrupted.

## Schema evolution

Protected state schema version 4 adds `synchronization_baseline`. Existing schema versions migrate forward transactionally: v1 adds recoverable work, v2 adds repository identity binding, and v3 adds synchronization baselines. Existing installation identity, work records and repository bindings are preserved.

## Boundary

This state primitive performs no Git command, network request, Home Assistant configuration mount, live filesystem write, candidate deployment, validation, backup, observation or rollback. A future local synchronization orchestrator may only record a baseline after its isolated Git publication succeeds. Remote candidate work remains subject to the controlled validation, backup, deployment, observation and rollback process defined by the initial V2 README.
