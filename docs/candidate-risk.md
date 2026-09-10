# Candidate deployment risk classification

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, specifically **Remote → Home Assistant** and **Deployment Risk Classification**.

Risk classification is the pipeline gate after dependency analysis. It is deliberately conservative and read-only: classification never validates Home Assistant configuration, creates a backup, changes live files, reloads/restarts Home Assistant, observes a deployment, promotes a candidate, or rolls back.

`ha_syncapp.candidate_risk.classify_candidate_risk()` first calls `verify_candidate_impact_analysis()`. The classifier therefore refuses to use impact evidence that cannot be deterministically reconstructed from the same dependency analysis and runtime snapshot.

## Classification rules

The README's four levels are preserved: `low`, `medium`, `high`, and `critical`. Each changed path establishes a minimum level, and independent uncertainty signals may only raise that level.

Examples implemented from the README include:

- automation, script, scene, blueprint, and otherwise non-critical YAML paths: **low**;
- dashboard/package multi-resource configuration: **medium**;
- `.storage`, core `configuration.yaml`, authentication/secret configuration, and custom components: **high**;
- Recorder/database files and sidecars: **critical**.

A path that cannot be recognized safely is classified **high**, not low. Dynamic candidate references, unanalyzed candidate bytes, unknown object references, and unresolved runtime relationships also establish a **high** minimum because incomplete dependency evidence must not be mistaken for proof of safety.

When several rules apply, the highest risk wins. Risk reasons, changed paths, affected entities, unresolved references, dynamic paths, and unanalyzed paths are emitted deterministically alongside the repository ID, baseline SHA, candidate SHA, Stage-manifest SHA-256, and runtime SHA-256 bindings.

Downstream consumers must call `verify_candidate_risk_classification()` before trusting stored or transported risk evidence. Verification re-runs the trusted impact verification and recomputes the classification exactly.

## Safety boundary

This classification is evidence only. It does not authorize deployment. The next README gate remains Home Assistant static/configuration validation, followed by a recoverable pre-deployment backup before any candidate can be applied.
