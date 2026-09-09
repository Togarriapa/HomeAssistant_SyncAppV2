# Guarded Local → Repo B synchronization cycle

This increment implements one bounded synchronization cycle from the live Home
Assistant configuration source to Repo B `main`. It is derived from the initial
V2 README and does not implement candidate deployment.

The live Home Assistant tree is consumed only through the read-only
`/homeassistant` App mount. A cycle copies a stable snapshot into protected
staging, copies that verified snapshot again into a separate mutable Git
workspace, and only then initializes or runs Git. Git is never initialized or
executed in `/homeassistant`.

## Safety sequence

For each cycle the coordinator:

1. requires an already pinned private Repo B identity;
2. captures and verifies a stable local snapshot;
3. prepares a separate isolated Git workspace;
4. initializes machine-owned Git metadata only in that workspace;
5. re-verifies Repo B identity and reads exact `main` presence/absence;
6. reads the durable last-synchronized baseline;
7. refuses automatic publication when the remote branch diverged from that
   baseline, disappeared after a baseline existed, or already exists without a
   local baseline;
8. when an existing remote exactly matches the baseline, anchors the isolated
   workspace to that exact commit before creating a new snapshot commit;
9. creates a commit only when Git sees meaningful content differences;
10. delegates all publication authorization and transport to the existing
    preflight → immutable intent → verified publication workflow; and
11. removes temporary snapshot/workspace data whether the cycle succeeds or
    fails.

A first publication is allowed only when the trusted private repository proves
that `main` is absent and there is no prior synchronization baseline. The normal
Git push remains non-force and is followed by fresh trusted remote verification
before durable state advances.

## Fail-closed outcomes

`baseline_required` means Repo B already has a `main` branch but this SyncApp
installation has no durable evidence establishing that branch as its prior
synchronization baseline. It will not adopt or overwrite the branch
implicitly.

`diverged` means Repo B `main` no longer equals the durable baseline. The cycle
preserves both states and performs no automatic merge or push.

`remote_missing` means a previously synchronized `main` branch disappeared. The
cycle does not recreate it automatically because doing so could conceal remote
administrative changes.

`no_change` means the isolated workspace, after anchoring to the exact trusted
baseline, contains no Git content change to publish. No push occurs.

## Deliberate boundaries

This coordinator is not yet invoked by the long-running service. Event-driven
change detection, debounce scheduling and Retrigger Work Cron Job integration are
separate increments. Remote `candidate` processing is also separate and remains
subject to integrity/dependency/risk validation, Home Assistant validation,
pre-deployment backup, apply, reload/restart, observation and automatic rollback.
Nothing in this local synchronization cycle grants write access to the live Home
Assistant configuration tree.
