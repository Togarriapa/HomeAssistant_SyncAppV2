# Retrigger Log Collection

The sole product specification for this behavior is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, especially the `logs`, Retrigger and Recovery, Security, and observability requirements.

Each scheduler-neutral Retrigger service invocation preserves recovery priority. After Repo B trust is reverified, the existing bounded recovery cycle runs first in deterministic order: local configuration synchronization, database synchronization, runtime synchronization, and at most one already-pending logs synchronization item. Only after those lanes complete does SyncApp collect a new bounded Home Assistant Core and Supervisor log snapshot.

The fresh collection is all-or-nothing. Its exact immutable artifact is staged below the app-owned `/data/syncapp/work/log-artifacts` boundary, which is created or reused as a same-owner private directory. Symlink, non-directory, ownership, containment, or filesystem ambiguity fails closed. The artifact is then enqueued as durable `logs` work using its deterministic artifact identity.

The newly enqueued logs item is **not** published in the same Retrigger invocation. The next scheduled Retrigger pass may claim it. This deliberate one-interval delay ensures that a collection failure cannot starve older recovery work and that one invocation cannot consume two log publication items.

`SUPERVISOR_TOKEN` is passed only to the existing bounded Core/Supervisor collector. It is not added to Repo B URLs, durable work identity, artifact metadata, or public failure diagnostics. Collection errors are translated into the sanitized Retrigger cycle failure so the scheduled recovery mechanism can retry later without exposing nested transport details.

This slice does not change Retrigger scheduling, candidate deployment, Home Assistant validation, backup, Apply, observation, rollback, container privileges, or the live Home Assistant configuration tree.
