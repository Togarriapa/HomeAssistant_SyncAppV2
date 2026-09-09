# Combined Home Assistant Core runtime bundle

This increment is derived only from the initial V2 `README.md` at root commit
`71d284ce447d79b044e332c9bc01ae801dc91947`, specifically the generated `runtime` branch and its
AI-readable Home Assistant inventory requirement.

## Publication boundary

A claimed `runtime` Retrigger item now collects both existing bounded Core sources before entering
the existing isolated runtime staging and Repo B publication transaction:

1. REST Core data: configuration summary, states and services.
2. WebSocket registry data: entity registry, device registry and area registry.
3. Fail-closed composition into one `RuntimeInventoryInput`.
4. Existing deterministic runtime artifact staging and verification.
5. Existing guarded non-force Repo B `runtime` publication and durable work transition.

No partial artifact is published. If either collector fails, the claimed runtime item returns to
transient retry state. If the two collectors unexpectedly produce the same manifest or dataset key,
composition fails rather than choosing one value and silently overwriting the other.

Target identity is checked before either collector runs, and an idle Retrigger pass performs no Core
collection at all.

## Current AI-visible Core datasets

The guarded runtime publication path can now carry these Home Assistant Core datasets together:

- states;
- services;
- entity registry records;
- device registry records;
- area registry records;
- the REST Core configuration summary and collector counts in the runtime manifest.

This does not claim that the README runtime inventory is complete. Integrations/config entries,
floors, labels, Supervisor data, hardware data, topology/dependency analysis and deployment
observation remain separate future increments.

## Unchanged safety properties

This composition layer adds no Home Assistant write command, WebSocket subscription, Supervisor
mutation permission, candidate deployment behavior, backup/restart/rollback behavior or new Git
transport. It only ensures already bounded read-only evidence reaches the already guarded runtime
publication lane as one coherent artifact.
