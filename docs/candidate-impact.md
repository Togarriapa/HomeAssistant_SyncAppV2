# Candidate runtime impact evidence

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, especially **Dependency and Topology Model**, **Remote → Home Assistant Deployment**, and the `runtime/analysis` model.

Candidate dependency evidence is bound to a canonical SHA-256 fingerprint of the exact `RuntimeInventoryInput` used during reference classification. The dependency gate fingerprints the runtime evidence before analysis and proves the same fingerprint again before returning. Downstream consumers can call `verify_candidate_dependency_analysis()` to reject structurally tampered evidence or a different runtime snapshot.

`ha_syncapp.candidate_impact.expand_candidate_impact()` consumes that verified candidate dependency evidence and the exact bound runtime snapshot. It rebuilds the deterministic registry topology and expands each known entity reference into the currently observable device, integration/config-entry, area, floor and label relationships.

Before any later stage, including Risk Classification, trusts candidate impact evidence, it must call `verify_candidate_impact_analysis()` with the exact `CandidateDependencyAnalysis` and `RuntimeInventoryInput` that the impact claims to represent. Verification validates the supplied result, re-verifies the dependency/runtime binding, deterministically rebuilds the impact evidence, and requires exact equality. A changed runtime snapshot or any removed, added, reordered, or altered relationship therefore fails closed and requires fresh dependency/impact analysis.

## Authority boundaries

The runtime topology remains the authority source for relationship provenance. Direct registry relationships remain `registry`; an entity area inherited through its device remains `derived`. Candidate impact preserves derived area identifiers separately so downstream Risk Classification cannot confuse inheritance with a direct entity registry assignment.

References whose registry targets are missing are retained as unresolved impact evidence. No missing node is invented. The candidate dependency analyzer remains `best_effort_lexical`; impact expansion does not upgrade lexical candidate parsing into semantic certainty.

Entity domains are retained. References in the `automation`, `script`, and `scene` domains are explicitly classified as those Home Assistant object kinds; all other entity domains are classified as ordinary entities.

## Safety boundary

This stage is read-only. It does not call Home Assistant services, execute candidate automations/scripts/templates, modify the live configuration, classify deployment risk, validate Home Assistant configuration, take a backup, apply a candidate, reload/restart Home Assistant, observe deployment health, promote a candidate or roll back.

Runtime evidence can legitimately change while Home Assistant is running. A runtime-fingerprint mismatch therefore fails closed and requires fresh dependency analysis rather than silently combining evidence captured from different system states.
