# Runtime dependency and topology evidence

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, especially the `runtime` and **Dependency and Topology Model** sections.

`ha_syncapp.runtime_topology.build_runtime_topology()` consumes only the already-collected `RuntimeInventoryInput`. It performs no Home Assistant API calls, writes, service calls, reloads, restarts or candidate execution. The result is another `RuntimeInventoryInput` containing `analysis/topology` and `analysis/dependencies`, so it can be merged into the existing runtime artifact pipeline without treating analysis as a deployment authority.

## Authority model

Relationships read directly from Home Assistant registries are marked `registry` and described as authoritative registry evidence for the exact collected runtime snapshot. Examples include entity-to-device, entity-to-config-entry, device-to-config-entry, device-to-area, area-to-floor and label assignments.

An entity area inherited through its registered device is marked `derived` on the topology edge and `derived_via_device` in the entity dependency summary. Derived relationships are intentionally described as best-effort rather than registry-authoritative direct assignments.

Entity state is observational runtime evidence. `available` is derived only from the collected state value: a missing state, `unknown` or `unavailable` is not treated as available. Runtime state is never treated as configuration authority.

## Unresolved references

A relationship that names an unknown device, integration/config entry, area, floor or label is retained in the deterministic `unresolved` list. SyncApp does not invent a missing node or silently discard the reference. Malformed datasets, duplicate identities and malformed relationship fields fail closed instead of producing potentially misleading topology.

## Current scope

This slice establishes the registry/runtime substrate required by parent task #133. It does **not** claim complete automation/script/scene dependency coverage and does not parse or execute staged candidate configuration. Candidate-specific dependency analysis, including explicit static references and unresolved dynamic/template references, remains a separate guarded increment before Risk Classification.
